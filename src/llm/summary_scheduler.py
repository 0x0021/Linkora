"""H2-A 后台异步摘要调度器（单 daemon 线程 + 队列 + per-chat 去重 + CAS 写回）。

设计要点（详见 docs/system_design.md §1、§7）：
- 与 DatabaseBackupCoordinator 完全同构：单 daemon 线程串行消费 queue.Queue，
  同一 chat 天然串行，写回无并发竞态。
- 主回复链路绝不阻塞：schedule() 仅做 per-chat pending 去重后 queue.put（非阻塞、不调 LLM）。
- 跨线程约定：Worker 只调 agent.summarize_conversation（已确认不读 agent._tl /
  _cache_*），并经 store 方法写回（per-thread 连接），不持有主线程 cursor。
- 写回三件套：① 单 daemon 线程串行 ② per-chat pending 去重（锁保护）③ generation CAS。
- 失败兜底：summarize_conversation 返回空 → 不写库（缓存保持旧值/空），主回复早已发出。
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from src.memory.sqlite_store import SQLiteStore

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环导入
    from src.llm.agent import LLMAgent
    from src.models import Message

logger = logging.getLogger(__name__)


@dataclass
class SummaryJob:
    """队列元素：单个 chat 的一次异步摘要任务。

    generation：调度时读取的当前库代际；Worker 写回时作为 CAS 期望值，
    保证即便（理论上不会发生的）并发双写，也只采纳代际匹配的写回。
    """

    chat_id: str
    older: list["Message"] = field(default_factory=list)
    generation: int = 0
    created_at: str = ""


class SummaryScheduler:
    """单 daemon 线程 + 队列 + per-chat 去重的后台摘要调度器。

    用法（对齐 DatabaseBackupCoordinator / _start_backup_scheduler 风格）：
        scheduler = SummaryScheduler(agent, store)
        scheduler.start()          # 立即返回，起后台线程
        agent._summary_scheduler = scheduler
        ...
        scheduler.stop()           # 退出时优雅停止（join）
    """

    def __init__(self, agent: "LLMAgent", store: "SQLiteStore") -> None:
        self._agent = agent
        self._store = store
        self._queue: "queue.Queue[SummaryJob]" = queue.Queue()
        self._pending: set[str] = set()  # 在途 chat_id（in-flight），用于去重
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        """起 daemon 线程消费队列，立即返回（不阻塞启动）。"""
        if self._thread is not None and self._thread.is_alive():
            logger.debug("[摘要调度] 已运行，忽略重复 start()")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="summary-scheduler", daemon=True,
        )
        self._thread.start()
        logger.info("[摘要调度] 后台异步摘要调度器已启动（daemon 线程）")

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止：置停止标志、入队毒丸唤醒 worker、join。

        pending 中的 chat 即便未处理也不阻塞退出（守护线程，且主回复已发出）。
        """
        self._stop_event.set()
        # 入队一个 None 作为毒丸，确保 worker 即便队列空也能从 get() 唤醒退出
        try:
            self._queue.put(None)  # type: ignore[arg-type]
        except Exception as _exc:  # noqa: BLE001
            logger.warning(f"stop: swallowed exception: {_exc}")
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("[摘要调度] 停止超时（%.1fs），守护线程将在进程退出时终止", timeout)
            else:
                logger.info("[摘要调度] 已停止")
        self._thread = None

    # ------------------------------------------------------------------ 调度入口
    def schedule(self, chat_id: str, older: list["Message"]) -> None:
        """非阻塞：pending 去重后把任务入队。

        - 同一 chat 在 in-flight 期间只入队一次（pending 去重）。
        - 调度时读取当前库代际作为 CAS 期望值，供 Worker 写回。
        - 任何异常都不应影响主回复链路（仅记日志）。
        """
        if not chat_id or not older:
            return
        if self._stop_event.is_set():
            return
        with self._pending_lock:
            if chat_id in self._pending:
                # 同 chat 已在途：跳过入队（单 worker 串行 + pending 去重，物理不可能双写）
                return
            self._pending.add(chat_id)
        try:
            # 读取当前代际（CAS 期望值）；无缓存行时为 0（首次写入）
            row = self._store._conversation_repo.get_conversation_summary(chat_id)
            generation = row.generation if row is not None else 0
            job = SummaryJob(
                chat_id=chat_id,
                older=list(older),
                generation=generation,
                created_at=datetime.now().isoformat(),
            )
            self._queue.put(job)
        except Exception as e:  # noqa: BLE001
            # 调度失败不能拖垮主回复：回收 pending 并记日志
            with self._pending_lock:
                self._pending.discard(chat_id)
            logger.warning("[摘要调度] 入队失败 chat_id=%s: %s", chat_id, e)

    # ------------------------------------------------------------------ 工作循环
    def _worker_loop(self) -> None:
        """daemon 线程主循环：取任务→处理，直到收到停止信号或毒丸。"""
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:  # 毒丸
                break
            try:
                self._process_job(job)
            except Exception as e:  # noqa: BLE001
                logger.warning("[摘要调度] 处理任务异常 chat_id=%s: %s", getattr(job, "chat_id", "?"), e)
            finally:
                # 无论成功/失败，都释放该 chat 的 pending，允许下一轮重新调度
                with self._pending_lock:
                    self._pending.discard(job.chat_id)
        logger.debug("[摘要调度] worker 线程退出")

    def _process_job(self, job: SummaryJob) -> None:
        """处理单个摘要任务：算摘要 → CAS 写回（失败兜底不写脏）。

        仅当 summarize_conversation 返回非空才执行 UPSERT；空则返回、不写库，
        缓存保持旧值（或仍为空），主回复不受影响、无脏数据。
        """
        try:
            summary = self._agent.summarize_conversation(
                job.older, max_messages=getattr(self._agent, "_summary_max_messages", 0),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[摘要调度] 摘要计算异常 chat_id=%s: %s", job.chat_id, e)
            return
        if not summary:
            # 失败兜底：返回空则不写库（不脏写），缓存保持旧值/空
            logger.debug("[摘要调度] 摘要为空，跳过写回 chat_id=%s", job.chat_id)
            return
        # older 段「最新一条」msg_id 作为边界标记（调试/未来精确校验用）
        older_boundary_msg_id = job.older[-1].msg_id if job.older else ""
        try:
            ok = self._store._conversation_repo.upsert_conversation_summary(
                chat_id=job.chat_id,
                summary=summary,
                older_boundary_msg_id=older_boundary_msg_id,
                covered_count=len(job.older),
                expected_generation=job.generation,
            )
            if ok:
                logger.debug(
                    "[摘要调度] 写回成功 chat_id=%s，覆盖 %d 条，代际=%d→%d",
                    job.chat_id, len(job.older), job.generation, job.generation + 1,
                )
            else:
                logger.debug("[摘要调度] 写回被 CAS 跳过 chat_id=%s", job.chat_id)
        except Exception as e:  # noqa: BLE001
            # DB 写异常仅记日志，不影响主线程（主回复早已发出）
            logger.warning("[摘要调度] 写回失败 chat_id=%s: %s", job.chat_id, e)
