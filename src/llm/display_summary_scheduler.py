"""展示用全量会话摘要调度器（信号驱动，写入独立的 conversation_display_summaries 表）。

设计要点（详见用户报告「摘要只取了一部分聊天记录」根因）：
- Web「对话摘要」页原本直接读 H2-A/动态摘要表（conversation_summaries），而那份摘要
  的定位是「LLM 上下文压缩记忆」——它只覆盖 history_window - tiering_recent 的 older 段，
  recent 段按设计永不进摘要，于是卡片看起来只覆盖聊天开头一小段。
- 本调度器与之**解耦**：独立收集每会话「近期全量消息」（summary_history_limit 窗口），
  调 LLM 生成覆盖整段对话的展示摘要，写入 conversation_display_summaries 表。
  两张表互不干扰：context 摘要继续服务 LLM，display 摘要专供 Web 展示。
- 复用动态摘要的「信号驱动 + 单 daemon worker + per-chat 去重 + CAS 写回」骨架，
  主回复链路不阻塞；失败兜底仅记日志。
- 收集用 get_recent_unarchived_messages(display_limit)（时间正序、排除摘要元消息），
  天然覆盖整个近期窗口（含 recent 段），从根本上修掉「只取一部分」的问题。
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
class DisplaySummaryJob:
    """队列元素：单个 chat 的一次展示摘要任务。消息在 Worker 侧按需收集，避免快照过期。"""

    chat_id: str
    generation: int = 0
    created_at: str = ""


class DisplaySummaryScheduler:
    """信号驱动的展示用全量摘要调度器（单 daemon 线程 + 队列 + per-chat 去重 + CAS 写回）。"""

    def __init__(self, agent: "LLMAgent", store: "SQLiteStore", platform: str = "dingtalk",
                 check_interval_seconds: int = 120,
                 min_messages: int = 4,
                 display_limit: int = 40,
                 interval_hours: int = 2,
                 freshness_seconds: int = 1800,
                 scan_days: int = 7,
                 job_interval_seconds: float = 0.5) -> None:
        self._agent = agent
        self._store = store
        self._platform = platform
        self._check_interval = max(10, int(check_interval_seconds))
        self._min_messages = max(1, int(min_messages))
        self._display_limit = max(self._min_messages, int(display_limit))
        self._interval_hours = max(1, int(interval_hours))
        self._freshness_seconds = max(60, int(freshness_seconds))
        self._scan_days = max(1, int(scan_days))
        # 任务间隔节流：首轮可能积压上百个「尚无展示摘要」的会话，若不节流会形成
        # 持续的 LLM+写库洪峰，与轮询器抢写锁。留最小间隔让写入平滑。
        self._job_interval = max(0.0, float(job_interval_seconds))
        self._queue: "queue.Queue[DisplaySummaryJob | None]" = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

    # ----------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.debug("[展示摘要] 已运行，忽略重复 start()")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="display-summary", daemon=True)
        self._worker = threading.Thread(
            target=self._worker_loop, name="display-summary-worker", daemon=True,
        )
        self._thread.start()
        self._worker.start()
        logger.info(
            "[展示摘要] 已启动（平台=%s，评估间隔=%ds，最小消息=%d，窗口=%d，再摘要间隔=%dh）",
            self._platform, self._check_interval, self._min_messages,
            self._display_limit, self._interval_hours,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        try:
            self._queue.put(None)  # 毒丸唤醒 worker
        except Exception as _exc:  # noqa: BLE001
            logger.debug("stop: 毒丸入队失败（队列可能已关闭）: %s", _exc)
        for th in (self._thread, getattr(self, "_worker", None)):
            if th is not None and th.is_alive():
                th.join(timeout=timeout)
                if th.is_alive():
                    logger.warning("[展示摘要] 停止超时（%.1fs）", timeout)
        self._thread = None
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
            job = DisplaySummaryJob(chat_id=chat_id, created_at=datetime.now().isoformat())
            self._queue.put(job)
            logger.debug("[展示摘要] 入队 chat_id=%s trigger=%s", chat_id, trigger)
        except Exception as e:  # noqa: BLE001
            with self._pending_lock:
                self._pending.discard(chat_id)
            logger.warning("[展示摘要] 入队失败 chat_id=%s: %s", chat_id, e)

    # ----------------------------------------------------------- 评估循环
    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("[展示摘要] 评估异常: %s", e)
            self._stop_event.wait(timeout=self._check_interval)

    def _run_once(self) -> None:
        """轻量评估：找出满足信号条件的会话并入队（不在此调 LLM）。"""
        if getattr(self._store, "_closed", False):
            logger.debug("[展示摘要] store 已关闭，跳过本轮")
            return
        try:
            chats = self._store._message_repo.get_conversations_needing_summary(
                max_messages=self._min_messages,
                summary_interval_hours=self._interval_hours,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[展示摘要] 查询需摘要会话失败: %s", e)
            return
        if not chats:
            return
        for chat in chats:
            if self._stop_event.is_set():
                break
            self._enqueue(chat["chat_id"], "signal")
        logger.info("[展示摘要] 本轮评估发现 %d 个待摘要会话（平台=%s）", len(chats), self._platform)

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
                logger.warning("[展示摘要] 处理异常 chat_id=%s: %s", getattr(job, "chat_id", "?"), e)
            finally:
                with self._pending_lock:
                    self._pending.discard(job.chat_id)
            if self._job_interval > 0 and not self._stop_event.is_set():
                self._stop_event.wait(timeout=self._job_interval)
        logger.debug("[展示摘要] worker 线程退出")

    def _process_job(self, job: DisplaySummaryJob) -> None:
        from src.memory.platform_context import with_platform
        with with_platform(self._platform):
            self._process_job_inner(job)

    def _process_job_inner(self, job: DisplaySummaryJob) -> None:
        chat_id = job.chat_id
        try:
            messages = self._store._message_repo.get_recent_unarchived_messages(
                chat_id, limit=self._display_limit,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[展示摘要] 收集消息失败 chat_id=%s: %s", chat_id[:20], e)
            return
        total = len(messages)
        if total < self._min_messages:
            return
        # 新鲜度护栏：已有覆盖充分且未过期则跳过，避免无谓重摘要
        try:
            cached = self._store._conversation_repo.get_display_summary(chat_id)
        except Exception as e:  # noqa: BLE001
            logger.debug("[展示摘要] 读缓存失败 chat_id=%s: %s", chat_id[:20], e)
            cached = None
        if cached is not None and cached.covered_count >= total:
            # 无新消息 → 取材窗口（最近 N 条）与已覆盖范围一致，摘要内容不会变，直接跳过。
            # 【P0】此处不可再叠加「超过 freshness_seconds 就重算」：那会让**每个空闲会话**
            # 每 30 分钟被重新摘要一次（实测 924 个会话 → 持续不断的 LLM 调用 + 写库流），
            # 无谓烧 token 并持续与轮询器抢写锁。只有「有新消息」才需要重摘要。
            logger.debug("[展示摘要] 无新消息，跳过 chat_id=%s（已覆盖 %d 条）", chat_id, total)
            return
        try:
            # max_messages=0 → 不截断，覆盖整个近期窗口（含 recent 段）
            summary = self._agent.summarize_conversation(messages)
        except Exception as e:  # noqa: BLE001
            logger.warning("[展示摘要] LLM 调用失败 chat_id=%s: %s", chat_id[:20], e)
            return
        if not summary:
            return
        boundary = messages[-1].msg_id if messages else ""
        boundary_ts = messages[-1].timestamp.isoformat() if messages else ""
        # 读取当前代际作 CAS 期望值，保证重复写回（同一 chat 多次摘要）正确递增
        try:
            cur_row = self._store._conversation_repo.get_display_summary(chat_id)
            expected_gen = cur_row.generation if cur_row is not None else 0
        except Exception:  # noqa: BLE001
            expected_gen = 0
        if self._write_with_retry(
            chat_id=chat_id, summary=summary, boundary_msg_id=boundary,
            covered_count=total, expected_generation=expected_gen,
            boundary_ts=boundary_ts,
        ):
            logger.debug("[展示摘要] 写回成功 chat_id=%s 覆盖 %d 条 边界时间=%s",
                         chat_id, total, boundary_ts)

    def _write_with_retry(self, *, chat_id: str, summary: str, boundary_msg_id: str,
                          covered_count: int, expected_generation: int,
                          boundary_ts: str, max_attempts: int = 3) -> bool:
        """写回展示摘要；遇 "database is locked" 时丢弃本线程连接自愈后重试。

        根因：worker 线程连接长期复用，一旦残留未关闭游标即持有陈旧 WAL 读快照，
        此后写库稳定失败（busy_timeout/rollback 均无效）。关掉并重建连接是唯一可靠的
        恢复手段，故此处「丢弃连接 → 重试」，避免摘要写回长期静默失败。
        """
        last_err: Exception | None = None
        for attempt in range(max_attempts):
            try:
                self._store._conversation_repo.upsert_display_summary(
                    chat_id=chat_id, summary=summary,
                    boundary_msg_id=boundary_msg_id, covered_count=covered_count,
                    expected_generation=expected_generation,
                    boundary_ts=boundary_ts,
                )
                return True
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "locked" not in str(e).lower():
                    break
                # 自愈：丢弃被污染的连接，下一轮 upsert 会新建连接
                try:
                    self._store.discard_conv_conn(self._platform)
                except Exception as _exc:  # noqa: BLE001
                    logger.debug("[展示摘要] 丢弃连接失败: %s", _exc)
                self._stop_event.wait(timeout=0.2 * (attempt + 1))
        logger.warning("[展示摘要] 写回失败 chat_id=%s: %s", chat_id[:20], last_err)
        return False
