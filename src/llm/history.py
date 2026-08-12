"""历史消息分级注入与摘要模块（H5/H6 + H2-A）。

从 ``src.llm.agent`` 拆出——纯函数，接收 agent 实例或 context 对象即可工作，
不在此维护线程局部状态。所有原 agent 方法（``_apply_history_tiering`` 等）
作为 1 行委托保留，避免破坏既有测试与 monkey-patch 行为。

设计原则：
- **纯函数 + 数据上下文**：模块级函数接收含必需属性的对象（``store`` /
  ``_summary_*`` 配置），不依赖 ``self._tl`` 这种线程局部状态。
- **零行为改动**：拆完跑全量测试应一致通过。
- **摘要异步**：H2-A 调度仍由 ``LLMAgent._maybe_schedule_summary`` 委托，
  真实接入 ``SummaryScheduler``。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from src.models import Message
import logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.llm.summary_scheduler import SummaryScheduler

# 中文字符正则——token 估算的字符/中文区分。
_RE_CHINESE = re.compile(r'[\u4e00-\u9fff]')


# 各模型 token 单价（美元 / 百万 token）。用于 ``estimate_cost`` 离线估算。
# 这是内置价目表；用户可在 config.llm.model_pricing 中覆盖或补充
# （优先级更高，见 ``get_model_price``）。
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "kenari-free": {"input": 0, "output": 0},
    "agnes-2.0-flash": {"input": 0, "output": 0},
    "agnes-2.5-flash": {"input": 0, "output": 0},
    "gemma-4-e2b-it-4bit": {"input": 0, "output": 0},
    "gpt-4o": {"input": 5.0, "output": 15.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    "claude-3-5-sonnet": {"input": 3.0, "output": 15.0},
    "claude-3-haiku": {"input": 0.25, "output": 1.25},
    "qwen-max": {"input": 0.12, "output": 0.12},
    "qwen-plus": {"input": 0.015, "output": 0.015},
    "qwen-turbo": {"input": 0.001, "output": 0.001},
}


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数（中文按 1.5 token/字，其他按 0.4 token/字）。

    启发式估算，仅用于 chat 主循环做"是否截断 / 是否降级"决策，
    不可用于计费——计费用 LLM 响应里的真实 usage。
    """
    if not text:
        return 0
    chinese = len(_RE_CHINESE.findall(text))
    other = len(text) - chinese
    return int(chinese * 1.5 + other * 0.4)


def get_model_price(model_name: str, user_pricing: dict | None = None) -> dict[str, float]:
    """返回 ``model_name`` 的单价 {"input": float, "output": float}（USD / 百万 token）。

    匹配优先级：先查用户配置 ``user_pricing``（config.llm.model_pricing，覆盖），
    再查内置 ``_MODEL_PRICING``，均不匹配则回退 {"input": 0, "output": 0}（零元）。
    采用子串匹配：``model_name`` 包含某 key 即命中（如 "my-gpt-4o-xyz" 命中 "gpt-4o"）。
    """
    model_name_lower = (model_name or "").lower()
    tables = [user_pricing, _MODEL_PRICING] if user_pricing else [_MODEL_PRICING]
    for table in tables:
        if not table:
            continue
        for name, price in table.items():
            if name and name in model_name_lower:
                return {
                    "input": float(price.get("input", 0) or 0),
                    "output": float(price.get("output", 0) or 0),
                }
    return {"input": 0.0, "output": 0.0}


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model_name: str,
    user_pricing: dict | None = None,
) -> float:
    """按 ``model_name`` 子串匹配到 ``_MODEL_PRICING`` 估算费用（USD）。

    未匹配的模型按 "{\"input\": 0, \"output\": 0}" 计（零元）——比抛异常更友好，
    因为离线估算本就是上限参考。
    """
    pricing = get_model_price(model_name, user_pricing)
    return (
        input_tokens * pricing["input"] + output_tokens * pricing["output"]
    ) / 1_000_000


def truncate_long_message(content: str, max_chars: int = 500) -> str:
    """超长单条消息截断（保留前 ``max_chars`` + 省略号）。"""
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "…"


# ---------------------------------------------------------------------------
# 历史分级注入（H5/H6 + H2-A）
# ---------------------------------------------------------------------------
# 触发语义保持 ``len(history) > max_recent`` 不变（与「注入轮次」概念一致）。
# 新逻辑（H2-A）：热路径【只读缓存、不等 LLM】——
#   1. 若 older 段命中新鲜且覆盖充分的缓存摘要 → 直接用缓存构造占位 Message（零 LLM 阻塞）；
#   2. 否则降级为仅 recent（安全不失忆），并异步调度后台补算摘要（供下一轮命中）。
# 主回复永远先发；摘要 LLM 计算完全在后台 daemon 线程，不阻塞本方法。


def apply_history_tiering(
    agent: Any,
    history: list[Message],
    max_recent: int | None = None,
) -> list[Message]:
    """历史分级注入（H5/H6 + H2-A）。

    参数：
        agent: ``LLMAgent`` 实例，提供 ``store`` / 摘要相关缓存配置 / 调度器；
               也可是任何含 ``_history_tiering_recent`` / ``_summary_*`` /
               ``_maybe_schedule_summary`` 接口的鸭子类型对象（便于单测）。
        history: 完整会话历史（含本轮）。
        max_recent: 注入 LLM 的近期完整条数上限；``None`` 时用 agent 默认值。

    返回：实际注入 LLM 的 history 切片（已用摘要占位 Message 替换 older）。
    """
    max_recent = int(max_recent or agent._history_tiering_recent or 0)
    if len(history) <= max_recent:
        return history

    recent = history[-max_recent:]
    older = history[:-max_recent]

    if older and len(older) >= agent._summary_min_older:
        # 1) 读缓存摘要（快读，无 LLM）；命中则直接复用，主回复零阻塞
        cached = read_cached_summary(agent, history[0].chat_id, older)
        if cached is not None:
            return [cached] + recent
        # 2) 缓存未命中：降级为仅 recent（安全），并异步补算（不阻塞主回复）
        agent._maybe_schedule_summary(history[0].chat_id, older)
    return recent


def read_cached_summary(
    agent: Any,
    chat_id: str,
    older: list[Message],
) -> "Message | None":
    """读缓存摘要并做新鲜度 + 覆盖率护栏判定。

    返回可注入的摘要占位 Message，或 None（无缓存/过期/覆盖不足）。
    任何异常（DB/解析）一律返回 None，降级为 recent 仅——保证主回复不受影响。
    """
    if not chat_id or not older:
        return None
    try:
        row = agent.store._conversation_repo.get_conversation_summary(chat_id) if agent.store else None
    except Exception as e:  # noqa: BLE001
        # 故意吞掉，所有摘要失败都不该阻塞主回复——日志由 caller 记录。
        import logging
        logging.getLogger(__name__).debug(
            "[摘要] 读缓存失败 chat_id=%s: %s", chat_id, e,
        )
        return None
    if row is None or not row.summary_text:
        return None
    try:
        updated_at = datetime.fromisoformat(row.updated_at) if row.updated_at else None
    except (ValueError, TypeError) as _exc:
        logger.debug(f"read_cached_summary: swallowed exception: {_exc}")
        updated_at = None
    if updated_at is None:
        return None
    # 新鲜度：超过窗口则视为过期
    age_seconds = (datetime.now() - updated_at).total_seconds()
    if age_seconds > agent._summary_max_age_seconds:
        return None
    # 覆盖率：缓存摘要须覆盖当前 older 的 >= 阈值，避免旧摘要漏掉新 older 导致失忆
    coverage = row.covered_count / max(1, len(older))
    if coverage < agent._summary_min_coverage_ratio:
        return None
    # 构造摘要占位 Message（注意：Message 用 timestamp 而非 created_at 字段）
    summary_msg = Message(
        msg_id=f"summary_{row.covered_count}",
        chat_id=chat_id,
        chat_type=(older[0].chat_type if older else ""),
        chat_name=(older[0].chat_name if older else None),
        sender_id="system",
        sender_name="系统",
        content=f"[摘要]{row.summary_text}",
        msg_type="text",
        timestamp=(older[0].timestamp if older else datetime.now()),
        role="system",
    )
    return summary_msg


def maybe_schedule_summary(
    agent: Any,
    chat_id: str,
    older: list[Message],
) -> None:
    """异步调度后台摘要（非阻塞）。

    仅在总开关开启、scheduler 已接线、older 非空时入队。任何异常都被吞掉，
    绝不影响主回复链路。
    """
    if not getattr(agent, "_summary_async_enabled", False):
        return
    scheduler: "SummaryScheduler | None" = getattr(agent, "_summary_scheduler", None)
    if scheduler is None:
        return
    if not chat_id or not older:
        return
    try:
        scheduler.schedule(chat_id, older)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).debug(
            "[摘要] 调度后台摘要失败 chat_id=%s: %s", chat_id, e,
        )
