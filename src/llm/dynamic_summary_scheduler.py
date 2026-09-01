"""动态（信号驱动）摘要调度器：取代固定 1 小时周期轮询（原 H2-B RollingSummaryScheduler）。

设计要点：
- 移除「每小时遍历所有活跃会话、盲取最近 N 分钟消息」的定时轰炸；
- 改为信号驱动：轻量心跳（check_interval_seconds）周期性评估哪些会话「需要」摘要，
  仅对真正满足条件的会话调 LLM：
    1) 静默触发：会话静默 ≥ quiet_minutes 且自上次摘要以来有 ≥ min_messages 条新消息；
    2) 体量触发：自上次摘要以来未摘要消息数 ≥ max_messages_per_chat（防无限增长）；
    3) 陈旧触发：距上次摘要 ≥ max_age_hours 且有新内容。
- 收集逻辑（重构）：自上次摘要时间点 conversations.last_summary_at 起收集未归档消息，
  而非固定时间窗，做到「只摘要真正新增的内容」。
- 复用 SummaryScheduler 的异步队列 + per-chat 去重 + CAS 写回骨架，主回复链路不阻塞。
- 失败兜底：LLM 未产出 / DB 写失败均仅记日志；成功后 mark_conversation_summarized
  更新 last_summary_at 防重摘要。
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from src.memory.sqlite_store import SQLiteStore

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环导入
    from src.llm.agent import LLMAgent

logger = logging.getLogger(__name__)


@dataclass
class DynamicSummaryJob:
    """队列元素：单个 chat 的一次动态摘要任务。消息在 Worker 侧按需收集，避免快照过期。"""

    chat_id: str
    generation: int = 0
    created_at: str = ""


class DynamicSummaryScheduler:
    """信号驱动的动态摘要调度器（单 daemon 线程 + 队列 + per-chat 去重 + CAS 写回）。"""

    def __init__(self, agent: "LLMAgent", store: "SQLiteStore", platform: str = "dingtalk",
                 check_interval_seconds: int = 60,
                 quiet_minutes: int = 10,
                 min_messages: int = 3,
                 max_messages_per_chat: int = 100,
                 max_age_hours: int = 24,
                 scan_days: int = 7) -> None:
        self._agent = agent
        self._store = store
        self._platform = platform
        self._check_interval = max(5, int(check_interval_seconds))
        self._quiet_minutes = max(1, int(quiet_minutes))
        self._min_messages = max(1, int(min_messages))
        self._max_messages = max(self._min_messages, int(max_messages_per_chat))
        self._max_age_hours = max(1, int(max_age_hours))
        self._scan_days = max(1, int(scan_days))
        self._queue: "queue.Queue[DynamicSummaryJob | None]" = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._evaluator: "threading.Thread | None" = None
        self._worker: "threading.Thread | None" = None

    # ----------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._evaluator is not None and self._evaluator.is_alive():
            logger.debug("[动态摘要] 已运行，忽略重复 start()")
            return
        self._stop_event.clear()
        self._evaluator = threading.Thread(target=self._loop, name="dynamic-summary", daemon=True)
        self._worker = threading.Thread(
            target=self._worker_loop, name="dynamic-summary-worker", daemon=True,
        )
        self._evaluator.start()
        self._worker.start()
        logger.info(
            "[动态摘要] 已启动（平台=%s，评估间隔=%ds，静默阈值=%dmin，最小消息=%d，上限=%d，陈旧=%dh）",
            self._platform, self._check_interval, self._quiet_minutes,
            self._min_messages, self._max_messages, self._max_age_hours,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        try:
            self._queue.put(None)  # 毒丸唤醒 worker
        except Exception as _exc:  # noqa: BLE001
            logger.debug("stop: 毒丸入队失败（队列可能已关闭）: %s", _exc)
        for th in (self._evaluator, self._worker):
            if th is not None and th.is_alive():
                th.join(timeout=timeout)
                if th.is_alive():
                    logger.warning("[动态摘要] 停止超时（%.1fs）", timeout)
        self._evaluator = None
        self._worker = None

    # ----------------------------------------------------------- 外部触发入口
    def request_summary(self, chat_id: str, trigger: str = "event") -> None:
        """非阻塞：供消息落库后等场景按需触发（带 pending 去重）。"""
        self._enqueue(chat_id, trigger)

    # ----------------------------------------------------------- 内部入队
    def _enqueue(self, chat_id: str, trigger: str) -> None:
        if not chat_id:
            return
        if self._stop_event.is_set():
            return
        with self._pending_lock:
            if chat_id in self._pending:
                return
            self._pending.add(chat_id)
        try:
            job = DynamicSummaryJob(chat_id=chat_id, created_at=datetime.now().isoformat())
            self._queue.put(job)
            logger.debug("[动态摘要] 入队 chat_id=%s trigger=%s", chat_id, trigger)
        except Exception as e:  # noqa: BLE001
            with self._pending_lock:
                self._pending.discard(chat_id)
            logger.warning("[动态摘要] 入队失败 chat_id=%s: %s", chat_id, e)

    # ----------------------------------------------------------- 评估循环
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("[动态摘要] 评估异常: %s", e)
            self._stop_event.wait(timeout=self._check_interval)

    def _run_once(self) -> None:
        """轻量评估：找出满足信号条件的会话并入队（不在此调 LLM）。"""
        if getattr(self._store, "_closed", False):
            logger.debug("[动态摘要] store 已关闭，跳过本轮")
            return
        try:
            chats = self._store._message_repo.get_chats_needing_dynamic_summary(
                quiet_minutes=self._quiet_minutes,
                min_messages=self._min_messages,
                max_messages_per_chat=self._max_messages,
                max_age_hours=self._max_age_hours,
                scan_days=self._scan_days,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[动态摘要] 查询需摘要会话失败: %s", e)
            return
        if not chats:
            return
        for chat in chats:
            if self._stop_event.is_set():
                break
            self._enqueue(chat["chat_id"], "signal")
        logger.info(
            "[动态摘要] 本轮评估发现 %d 个待摘要会话（平台=%s）",
            len(chats), self._platform,
        )

    # ----------------------------------------------------------- 工作循环
    def _worker_loop(self) -> None:
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
                logger.warning("[动态摘要] 处理异常 chat_id=%s: %s", getattr(job, "chat_id", "?"), e)
            finally:
                with self._pending_lock:
                    self._pending.discard(job.chat_id)
        logger.debug("[动态摘要] worker 线程退出")

    def _process_job(self, job: DynamicSummaryJob) -> None:
        from src.memory.platform_context import with_platform
        with with_platform(self._platform):
            self._process_job_inner(job)

    def _process_job_inner(self, job: DynamicSummaryJob) -> None:
        chat_id = job.chat_id
        try:
            messages = self._store._message_repo.collect_dynamic_summary_messages(
                chat_id, max_messages=self._max_messages, platform=self._platform,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[动态摘要] 收集消息失败 chat_id=%s: %s", chat_id[:20], e)
            return
        if len(messages) < self._min_messages:
            return
        try:
            summary = self._agent.summarize_conversation(messages)
        except Exception as e:  # noqa: BLE001
            logger.warning("[动态摘要] LLM 调用失败 chat_id=%s: %s", chat_id[:20], e)
            return
        if not summary:
            return
        older_boundary = messages[-1].msg_id if messages else ""
        try:
            self._store._conversation_repo.upsert_conversation_summary(
                chat_id=chat_id, summary=summary,
                older_boundary_msg_id=older_boundary, covered_count=len(messages),
            )
            # 更新 last_summary_at，避免下一轮重复摘要同一批内容
            self._store._message_repo.mark_conversation_summarized(chat_id)
            logger.debug("[动态摘要] 写回成功 chat_id=%s 覆盖 %d 条", chat_id, len(messages))
        except Exception as e:  # noqa: BLE001
            logger.warning("[动态摘要] 写回失败 chat_id=%s: %s", chat_id[:20], e)
