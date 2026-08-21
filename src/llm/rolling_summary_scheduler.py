"""H2-B 滚动摘要调度器：每小时提取最近 N 分钟消息生成摘要。

设计要点：
- 与 SummaryScheduler（事件驱动、压缩归档）互补：本调度器按时间窗口滚动生成摘要，
  不压缩历史消息，仅负责把近期对话片段聚合为可展示摘要。
- 触发：daemon 线程每 interval_minutes 轮询一次；每次遍历近期活跃会话，
  取最近 lookback_minutes 分钟内的消息调 LLM 生成摘要，写回 conversation_summaries。
- 失败兜底：LLM 未产出摘要 / DB 写失败均仅记日志，不阻塞其他会话。
- 配置门控：memory.conversation_summary.rolling_enabled=false 时不启动。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环导入
    from src.llm.agent import LLMAgent
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class RollingSummaryScheduler:
    """每小时滚动提取近期消息生成摘要的后台调度器。"""

    def __init__(self, agent: "LLMAgent", store: "SQLiteStore",
                 platform: str = "dingtalk",
                 lookback_minutes: int = 60,
                 interval_minutes: int = 60,
                 max_messages_per_chat: int = 100,
                 min_messages: int = 3) -> None:
        self._agent = agent
        self._store = store
        self._platform = platform
        self._lookback_minutes = lookback_minutes
        self._interval_minutes = interval_minutes
        self._max_messages = max_messages_per_chat
        self._min_messages = min_messages
        self._stop_event = threading.Event()
        self._thread: "threading.Thread | None" = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.debug("[滚动摘要] 已运行，忽略重复 start()")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="rolling-summary", daemon=True,
        )
        self._thread.start()
        logger.info(
            "[滚动摘要] 已启动（平台=%s，间隔=%dmin，回溯=%dmin）",
            self._platform, self._interval_minutes, self._lookback_minutes,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("[滚动摘要] 停止超时（%.1fs）", timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("[滚动摘要] 执行异常: %s", e, exc_info=True)
            self._stop_event.wait(timeout=self._interval_minutes * 60)

    def _run_once(self) -> None:
        """扫描所有活跃会话，每个会话取最近 lookback_minutes 分钟消息生成摘要。"""
        # 检查 store 是否已关闭（避免 shutdown 后访问导致 SQLiteStore is closed 异常）
        if getattr(self._store, '_closed', False):
            logger.debug("[滚动摘要] store 已关闭，跳过本轮")
            return

        cutoff_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=self._lookback_minutes)
        ).isoformat()

        try:
            repo = self._store._conversation_repo
            conn = repo._cc(self._platform)
            cur = conn.cursor()

            # 取最近 7 天内有消息的会话（避免全表扫描无意义）
            cur.execute(
                """SELECT chat_id, chat_name FROM conversations
                   WHERE updated_at >= datetime('now', '-7 days')
                   ORDER BY updated_at DESC
                   LIMIT 50""",
            )
            convs = [dict(r) for r in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            logger.warning("[滚动摘要] 查询活跃会话失败: %s", e)
            return

        processed = 0
        for conv in convs:
            if self._stop_event.is_set():
                break
            chat_id = conv["chat_id"]
            chat_name = conv.get("chat_name", "") or chat_id[:20]
            try:
                self._process_chat(chat_id, chat_name, cutoff_ts)
                processed += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[滚动摘要] 处理会话 %s 失败: %s", chat_id[:20], e)

        logger.info("[滚动摘要] 本轮处理 %d 个会话（平台=%s）", processed, self._platform)

    def _process_chat(self, chat_id: str, chat_name: str, cutoff_ts: str) -> None:
        """提取最近 N 分钟消息，调 LLM 生成摘要，写回 conversation_summaries。"""
        from src.models import Message
        try:
            repo = self._store._conversation_repo
            conn = repo._cc(self._platform)
            cur = conn.cursor()
            cur.execute(
                """SELECT msg_id, chat_id, chat_type, sender_id, sender_name,
                          content, msg_type, timestamp
                   FROM messages
                   WHERE chat_id = ? AND is_archived = 0 AND timestamp >= ?
                   ORDER BY timestamp ASC
                   LIMIT ?""",
                (str(chat_id), cutoff_ts, self._max_messages),
            )
            rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("[滚动摘要] 查询消息失败 chat_id=%s: %s", chat_id, e)
            return

        if len(rows) < self._min_messages:
            return  # 消息不足，跳过

        messages = []
        for row in rows:
            try:
                ts = row["timestamp"]
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                dt = datetime.now(timezone.utc)
            messages.append(Message(
                msg_id=row["msg_id"] or "",
                chat_id=row["chat_id"],
                chat_type=row["chat_type"] or "",
                chat_name=chat_name,
                sender_id=row["sender_id"] or "",
                sender_name=row["sender_name"] or "",
                content=row["content"] or "",
                msg_type=row["msg_type"] or "text",
                timestamp=dt,
            ))

        # 调 LLM 生成摘要
        try:
            summary = self._agent.summarize_conversation(messages)
        except Exception as e:  # noqa: BLE001
            logger.warning("[滚动摘要] LLM 调用失败 chat_id=%s: %s", chat_id, e)
            return

        if not summary:
            return

        # 写回 conversation_summaries（upsert）
        try:
            older_boundary = messages[-1].msg_id if messages else ""
            repo.upsert_conversation_summary(
                chat_id=chat_id, summary=summary,
                older_boundary_msg_id=older_boundary,
                covered_count=len(messages),
            )
            logger.debug(
                "[滚动摘要] 摘要已写回 chat_id=%s 覆盖 %d 条消息",
                chat_id, len(messages),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[滚动摘要] 写回失败 chat_id=%s: %s", chat_id, e)
