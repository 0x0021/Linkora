"""入站消息防护统一入口：检测 → 频率控制 → 处置决策 → 事件留存。

被 ``src/platform/runtime_inbound.py::_handle_message_with_rid`` 在入站最前端调用；
Web 管理台经 ``web/routers/guard.py`` 读取 ``src.guard.events`` 展示结果。

**不误伤正常消息**；命中结构标记的消息默认只清洗 + 边界隔离，照常进入 LLM。
只有「多部件共现的越权指令编排」（见 ``src/guard/commands.py``）默认整条拦截，
``mode=observe`` 时任何情况都不拦截。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.guard.config import DEFAULT_GUARD_CONFIG, GuardConfig
from src.guard.events import append_event, note_inspected
from src.guard.inspect import Assessment, assess, preview

logger = logging.getLogger("linkora.guard")

_SIGNAL_LABELS: dict[str, str] = {
    "boundary_escape": "伪造不可信数据边界标记",
    "role_spoofing": "伪造对话角色 / 模板标记",
    "role_label": "行首角色标签（仅记录）",
    "control_chars": "不可见控制字符",
    "oversize": "超长消息",
    "repeat_flood": "病态字符填充",
    "rate_limited": "触发频率控制",
    "injection_command": "越权指令编排（提示词注入）",
}

_ACTION_LABELS: dict[str, str] = {
    "allow": "正常放行",
    "observed": "仅记录（观测模式）",
    "detected": "已记录，正文零改写",
    "sanitized": "已清洗载荷",
    "isolated": "已清洗并隔离",
    "blocked": "已拦截，未进入 LLM",
    "rate_limited": "频率控制拦截",
}


class _ConfigState:
    """运行期策略覆盖状态（Web 管理台调参用，不写 config.yaml）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.override: GuardConfig | None = None


_config_state = _ConfigState()


def set_guard_config_override(cfg: GuardConfig | None) -> None:
    """设置 / 清除运行期策略覆盖（None 恢复为 config.yaml 的 safety.guard）。

    刻意**不落盘**：live ``config.yaml`` 是配置红线，绝不自动改写。
    管理台调整的策略仅在当前进程生效，重启后回到 yaml 配置。
    """
    with _config_state.lock:
        _config_state.override = cfg


def get_guard_config_override() -> GuardConfig | None:
    with _config_state.lock:
        return _config_state.override


def get_guard_config() -> GuardConfig:
    """读取运行期策略：Web 覆盖 > ``shared_state`` 配置单例 > 默认策略。

    主进程未启动（单测 / 独立 Web）或配置模型较旧时回退默认策略，
    保证防护能力在任何进程形态下都可用、且绝不因读配置失败而抛异常。
    """
    override = get_guard_config_override()
    if override is not None:
        return override
    from src.shared_state import get_config  # 函数级 import：避免包初始化期形成循环

    guard = getattr(getattr(get_config(), "safety", None), "guard", None)
    return guard if isinstance(guard, GuardConfig) else DEFAULT_GUARD_CONFIG


class SlidingWindowLimiter:
    """按 key 的滑动窗口计数（进程内，线程安全）。

    仅用于抑制「同一发送者短时间刷大量注入载荷」，阈值默认宽松，
    正常聊天不会触发；不替代网关级限流。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, *, window_seconds: int, max_count: int) -> bool:
        """记录一次命中；返回 True 表示**已超限**。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            cutoff = now - window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            bucket.append(now)
            over = len(bucket) > max_count
            if len(self._hits) > 2000:
                self._hits = {
                    k: v for k, v in self._hits.items() if v and v[-1] >= cutoff
                }
            return over

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = SlidingWindowLimiter()


def reset_rate_limiter() -> None:
    """清空频率控制计数（测试与策略重置时使用）。"""
    _limiter.reset()


@dataclass(frozen=True)
class GuardDecision:
    """一条入站消息的防护决策。"""

    risk: str
    signals: tuple[str, ...]
    signal_labels: tuple[str, ...]
    action: str
    action_label: str
    blocked: bool
    char_count: int

    @property
    def is_clean(self) -> bool:
        return self.risk == "none" and not self.blocked


_ALLOW_DECISION = GuardDecision(
    risk="none",
    signals=(),
    signal_labels=(),
    action="allow",
    action_label=_ACTION_LABELS["allow"],
    blocked=False,
    char_count=0,
)


def would_block_inbound(result: Assessment, cfg: GuardConfig) -> bool:
    """判定该检测结果是否会被整条拦截（入站链路与 Web 预览共用同一套规则）。

    抽成函数是为了避免「预览显示不拦、实际链路却拦了」这类判定漂移。
    频率控制是独立的有状态分支，不在此函数内（预览不消耗计数）。
    """
    if cfg.mode == "observe" or not cfg.enabled:
        return False
    # 确证的多部件越权指令编排 —— 默认拦截，可用 block_injection_commands 关闭。
    if "injection_command" in result.signals and cfg.block_injection_commands:
        return True
    # 结构标记类高风险 —— 默认仍只隔离，由 block_high_risk 显式开启拦截。
    return result.risk == "high" and cfg.block_high_risk


def decide_action(
    result: Assessment,
    cfg: GuardConfig,
    *,
    over_limit: bool = False,
) -> tuple[str, bool]:
    """返回 ``(action, blocked)`` —— 入站链路与 Web 预览共用的**唯一判定真源**。

    抽成纯函数是为了避免「预览显示不拦、实际链路却拦了」这类判定漂移。
    频率控制的计数是有状态的，由调用方算出 ``over_limit`` 后传入；预览恒传 False。
    """
    if not cfg.enabled:
        return "allow", False
    if over_limit and cfg.rate_limit_block:
        return "rate_limited", True
    if cfg.mode == "observe":
        return ("observed" if result.risk != "none" else "allow"), False
    if would_block_inbound(result, cfg):
        return "blocked", True
    if result.risk == "none":
        return "allow", False
    if result.risk == "low":
        return "detected", False
    if cfg.wrap_untrusted_inbound:
        return "isolated", False
    return "sanitized", False


def inspect_inbound(
    content: str,
    *,
    platform: str = "",
    chat_id: str = "",
    chat_name: str = "",
    sender_id: str = "",
    sender_name: str = "",
    msg_id: str = "",
    cfg: GuardConfig | None = None,
) -> GuardDecision:
    """评估一条「别人发过来的消息」并留存事件，返回处置决策。

    纯业务无 IO 失败风险：事件写入是 best-effort，任何异常都在此层内消化，
    绝不向消息处理链路抛出。
    """
    cfg = cfg or get_guard_config()
    note_inspected()
    if not cfg.enabled:
        return _ALLOW_DECISION

    result = assess(content, cfg)
    signals = list(result.signals)
    over_limit = False
    if cfg.rate_limit_enabled:
        key = f"{platform}:{sender_id or chat_id or 'unknown'}"
        over_limit = _limiter.hit(
            key,
            window_seconds=cfg.rate_limit_window_seconds,
            max_count=cfg.rate_limit_max_messages,
        )
        if over_limit and "rate_limited" not in signals:
            signals.append("rate_limited")

    action, blocked = decide_action(result, cfg, over_limit=over_limit)

    decision = GuardDecision(
        risk=result.risk,
        signals=tuple(signals),
        signal_labels=tuple(_SIGNAL_LABELS.get(s, s) for s in signals),
        action=action,
        action_label=_ACTION_LABELS.get(action, action),
        blocked=blocked,
        char_count=result.char_count,
    )

    should_log = (
        cfg.audit_enabled
        and (result.risk != "none" or over_limit or cfg.log_all_messages)
    )
    if should_log:
        now = datetime.now().astimezone()
        record: dict[str, Any] = {
            "ts": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "event_id": uuid.uuid4().hex[:12],
            "platform": platform,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "msg_id": msg_id,
            "risk": result.risk,
            "signals": decision.signals,
            "signal_labels": decision.signal_labels,
            "categories": list(result.categories),
            "action": decision.action,
            "action_label": decision.action_label,
            "blocked": decision.blocked,
            "char_count": result.char_count,
            "content_preview": preview(content, cfg.content_preview_chars),
            "detail": "、".join(decision.signal_labels) or "无风险",
        }
        append_event(record, max_events=cfg.max_events)
        try:
            from src.audit import audit

            audit(
                "prompt_guard",
                action,
                "blocked" if blocked else "detected",
                actor=sender_name or "unknown",
                session_key=chat_id or None,
                target=platform or "unknown",
                detail=f"risk={result.risk} signals={','.join(decision.signals)}",
            )
        except (ImportError, OSError, ValueError) as e:
            logger.debug("[guard] 结构化审计写入跳过: %s", e)

    if result.risk != "none" or blocked:
        logger.warning(
            "[提示词防护] risk=%s action=%s signals=%s 来自=%s 会话=%s 长度=%d",
            result.risk, action, ",".join(decision.signals) or "-",
            sender_name or "(未知)", chat_name or chat_id[:16] or "-", result.char_count,
        )
    return decision


__all__ = [
    "GuardDecision",
    "SlidingWindowLimiter",
    "decide_action",
    "get_guard_config",
    "get_guard_config_override",
    "inspect_inbound",
    "reset_rate_limiter",
    "set_guard_config_override",
    "would_block_inbound",
]
