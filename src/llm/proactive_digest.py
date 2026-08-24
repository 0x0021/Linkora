"""P4-13 主动触达：每日定时把近期对话摘要主动推送给主人。

设计要点：
- 配置门控（``proactive.enabled`` 默认 False），开启后才在后台 daemon 线程启动；
  关闭或 owner 未配置时「不启动」，对线上零行为影响。
- 触发：每日本地时区 hour:minute 一次。复用 ``SummaryScheduler`` 已产出的
  对话缓存摘要（conversation_summaries.summary_text），仅做滚动汇总，**不额外消耗 LLM**。
- 窗口：**滚动窗口**（默认过去 ``lookback_hours=24`` 小时），跨自然日生效。
  例如每日 17:30 推送即覆盖「昨日 17:30 → 今日 17:30」，不再把今日 0 点作为硬截断。
  收集直接复用 Web「对话摘要」页的 ``list_recent_summaries(since=cutoff)``，推送与页面同源。
- 发送：经 ``DwsAdapter.chat_message_send`` 推给 owner 的 1:1（user_id 或 open_dingtalk_id）。
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
    from src.dws_adapter import DwsAdapter
    from src.llm.agent import LLMAgent
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _next_trigger_ts(now: datetime, hour: int, minute: int) -> float:
    """返回下一次触发时刻的时间戳（秒）。若今日该时刻已过，则顺延到明日。"""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def build_digest(items: list[dict], max_summary_chars: int = 200,
                 title: str | None = None) -> str:
    """由近期对话摘要拼装中文 digest 文本（纯函数，便于单测，不触碰 IO）。

    输出按钉钉 markdown 渲染习惯排版：条目之间空一行（段间距），避免单换行被
    钉钉折叠成空格导致所有摘要粘成一段；自动去除摘要里冗余的「【对话摘要】」前缀。

    Args:
        items: 每项含 ``chat_name`` / ``chat_id`` / ``summary``（已截断前的原文）。
        max_summary_chars: 单条摘要截断长度，超出补省略号。
        title: 自定义标题；缺省回退「今日对话摘要」（Web 页默认口径）。
            推送场景传「近 N 小时对话摘要」以匹配滚动窗口语义。
    """
    if not items:
        return "（今日无新对话摘要）"
    header = title or f"📋 今日对话摘要（共 {len(items)} 段）"
    paragraphs: list[str] = [header, ""]
    for idx, it in enumerate(items, start=1):
        name = it.get("chat_name") or it.get("chat_id") or "未知对话"
        summary = (it.get("summary") or "").strip()
        # 摘要函数本身会以「【对话摘要】」开头，聚合推送里不需要重复前缀
        summary = summary.removeprefix("【对话摘要】").strip()
        if not summary:
            paragraphs.append(f"{idx}. **{name}**：（无摘要）")
            continue
        if len(summary) > max_summary_chars:
            summary = summary[:max_summary_chars] + "…"
        paragraphs.append(f"{idx}. **{name}**：{summary}")
    # 标题与正文、条目与条目之间用空行分隔，确保钉钉渲染出清晰段落
    return "\n\n".join(paragraphs)


class ProactiveDigestScheduler:
    """每日定时把近期对话摘要主动推送给主人的后台调度器（默认关闭）。"""

    def __init__(self, agent: "LLMAgent", store: "SQLiteStore",
                 adapter: "DwsAdapter", config: "ProactiveConfig",
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
        """取「过去 lookback_hours 小时内」有缓存摘要的对话（供测试与运行期共用）。

        直接复用 Web「对话摘要」页的 ``list_recent_summaries(since=cutoff)``，
        保证推送与页面**同源同口径**，不再依赖「前 N 个活跃会话」枚举。

        窗口为**滚动窗口** ``now - lookback_hours``（默认 24h），可跨越自然日：
        例如每日 17:30 推送即覆盖「昨日 17:30 → 今日 17:30」，不再把今日 0 点作为
        硬截断（旧实现 ``max(今日0点, now-lookback)`` 在 lookback≥24 时恒等于 0 点，
        导致参数静默失效、昨日尾巴被丢弃）。
        """
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=self._cfg.lookback_hours)
            cutoff_iso = cutoff.isoformat()
            rows = self._store._conversation_repo.list_recent_summaries(
                limit=self._cfg.max_conversations,
                platform=self._platform,
                since=cutoff_iso,
            )
        except Exception as e:
            # 读取近期对话失败不影响主回复链路，降级为空列表
            logger.warning("[主动触达] 读取近期对话失败: %s", e)
            return []

        items: list[dict] = []
        for r in rows:
            summary = (r.get("summary_text") or "").strip()
            if not summary:
                continue
            items.append({
                "chat_id": r.get("chat_id"),
                "chat_name": r.get("chat_name"),
                "summary": summary,
                "updated_at": r.get("updated_at") or "",
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
            except Exception as e:
                # 执行异常不影响主回复链路，仅记录警告
                logger.warning("[主动触达] 执行异常: %s", e)

    def _run_once(self) -> None:
        items = self.collect_items()
        title = f"📋 近 {self._cfg.lookback_hours} 小时对话摘要（共 {len(items)} 段）"
        digest = build_digest(items, self._cfg.max_summary_chars, title=title)
        try:
            if self._cfg.owner_user_id:
                self._adapter.chat_message_send(title="每日对话摘要", text=digest,
                                                user=self._cfg.owner_user_id)
            else:
                self._adapter.chat_message_send(title="每日对话摘要", text=digest,
                                                open_dingtalk_id=self._cfg.owner_open_dingtalk_id)
            logger.info("[主动触达] 已推送（%d 段）", len(items))
        except Exception as e:
            # 推送失败不影响主回复链路，仅记录警告
            logger.warning("[主动触达] 推送失败: %s", e)
