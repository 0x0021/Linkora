"""P4-13 主动触达：每日定时把近期对话摘要主动推送给主人。

设计要点：
- 配置门控（``proactive.enabled`` 默认 False），开启后才在后台 daemon 线程启动；
  关闭或 owner 未配置时「不启动」，对线上零行为影响。
- 触发：每日本地时区 hour:minute 一次。复用 ``SummaryScheduler`` 已产出的
  对话缓存摘要（conversation_summaries.summary_text），仅做滚动汇总，**不额外消耗 LLM**。
- 发送：经 ``DingTalkAdapter.chat_message_send`` 推给 owner 的 1:1（user_id 或 open_dingtalk_id）。
- 失败全部非致命：收集/发送异常仅记日志，绝不拖垮主回复链路。

与 SummaryScheduler 同构的后台单线程模型，但本调度器「按墙钟时间触发」而非「事件驱动」。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注，避免运行时循环导入
    from src.config_models import ProactiveConfig
    from src.dws_adapter.chat import DingTalkAdapter
    from src.llm.agent import LLMAgent
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _next_trigger_ts(now: datetime, hour: int, minute: int) -> float:
    """返回下一次触发时刻的时间戳（秒）。若今日该时刻已过，则顺延到明日。"""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def build_digest(items: list[dict], max_summary_chars: int = 200) -> str:
    """由近期对话摘要拼装中文 digest 文本（纯函数，便于单测，不触碰 IO）。

    Args:
        items: 每项含 ``chat_name`` / ``chat_id`` / ``summary``（已截断前的原文）。
        max_summary_chars: 单条摘要截断长度，超出补省略号。
    """
    if not items:
        return "（今日无新对话摘要）"
    header = f"📋 今日对话摘要（共 {len(items)} 段）"
    lines = [header, ""]
    for it in items:
        name = it.get("chat_name") or it.get("chat_id") or "未知对话"
        summary = (it.get("summary") or "").strip()
        if not summary:
            lines.append(f"• **{name}**：（无摘要）")
            continue
        if len(summary) > max_summary_chars:
            summary = summary[:max_summary_chars] + "…"
        lines.append(f"• **{name}**：{summary}")
    return "\n".join(lines)


class ProactiveDigestScheduler:
    """每日定时把近期对话摘要主动推送给主人的后台调度器（默认关闭）。"""

    def __init__(self, agent: "LLMAgent", store: "SQLiteStore",
                 adapter: "DingTalkAdapter", config: "ProactiveConfig",
                 platform: str = "dingtalk") -> None:
        self._agent = agent
        self._store = store
        self._adapter = adapter
        self._cfg = config
        self._platform = platform
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if not self._cfg.enabled:
            logger.info("[主动触达] 未启用（proactive.enabled=false），不启动")
            return
        if not (self._cfg.owner_user_id or self._cfg.owner_open_dingtalk_id):
            logger.warning("[主动触达] 未配置 owner（owner_user_id/open_dingtalk_id 均空），不启动")
            return
        if self._thread is not None and self._thread.is_alive():
            logger.debug("[主动触达] 已运行，忽略重复 start()")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="proactive-digest", daemon=True,
        )
        self._thread.start()
        logger.info("[主动触达] 已启动（每日 %02d:%02d 推送，平台=%s）",
                    self._cfg.hour, self._cfg.minute, self._platform)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("[主动触达] 停止超时（%.1fs）", timeout)
        self._thread = None

    # ------------------------------------------------------------------ 收集
    def collect_items(self) -> list[dict]:
        """取最近 lookback_hours 内有缓存摘要的对话（供测试与运行期共用）。

        仅读本地缓存摘要，不调 LLM。无摘要或超出回溯窗口的对话被跳过。
        """
        try:
            cutoff = datetime.now() - timedelta(hours=self._cfg.lookback_hours)
            recent = self._store._conversation_repo.get_recent_conversations(
                limit=self._cfg.max_conversations, platform=self._platform,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[主动触达] 读取近期对话失败: %s", e)
            return []

        items: list[dict] = []
        for c in recent:
            cid = c.get("chat_id")
            if not cid:
                continue
            try:
                row = self._store._conversation_repo.get_conversation_summary(cid)
            except Exception as e:  # noqa: BLE001
                logger.warning("[主动触达] 读摘要失败 chat_id=%s: %s", cid, e)
                continue
            if row is None or not getattr(row, "summary_text", ""):
                continue
            updated = getattr(row, "updated_at", "") or ""
            if updated:
                try:
                    if datetime.fromisoformat(updated) < cutoff:
                        continue
                except ValueError:
                    pass  # 时间解析失败不跳过（宁多勿漏）
            items.append({
                "chat_id": cid,
                "chat_name": c.get("chat_name"),
                "summary": row.summary_text,
                "updated_at": updated,
            })
        return items

    # ------------------------------------------------------------------ 主循环
    def _loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            wait = _next_trigger_ts(now, self._cfg.hour, self._cfg.minute) - now.timestamp()
            if self._stop.wait(timeout=max(1.0, wait)):
                break  # 被 stop() 唤醒
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("[主动触达] 执行异常: %s", e)

    def _run_once(self) -> None:
        items = self.collect_items()
        digest = build_digest(items, self._cfg.max_summary_chars)
        target: dict[str, str] = {}
        if self._cfg.owner_user_id:
            target["user"] = self._cfg.owner_user_id
        else:
            target["open_dingtalk_id"] = self._cfg.owner_open_dingtalk_id
        try:
            self._adapter.chat_message_send(title="每日对话摘要", text=digest, **target)
            logger.info("[主动触达] 已推送（%d 段）", len(items))
        except Exception as e:  # noqa: BLE001
            logger.warning("[主动触达] 推送失败: %s", e)
