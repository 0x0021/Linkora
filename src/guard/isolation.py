"""提示词边界隔离：把「别人发来的消息」在进入 LLM 前标为不可信数据。

与 ``src/llm/untrusted.py`` 用同一套边界标记，但**只在命中结构化风险时**才包裹：

- 正常业务消息（``risk="none"``）→ **字节级原样返回**，不改 prompt、不降级角色、
  不动 provider 契约，完全兼容 ``tests/test_prompt_injection.py`` 的零误伤回归；
- 命中伪造角色标记 / 边界逃逸 / 定向控制字符 → 先清洗载荷，再加边界标记，
  让模型明确「这段是外部数据，其中任何指令都不是系统指令」；
- ``mode="observe"`` → 只记录不改写，任何情况下都原样返回。

历史铁律说「当前用户消息不包裹」，理由是包裹会降低模型对正常意图的敏感度。
本实现只对**已确证含协议级攻击结构**的消息开例外，正常消息一个字都不动。
"""
from __future__ import annotations

from src.guard.config import GuardConfig
from src.guard.inspect import Assessment, assess, sanitize
from src.llm.untrusted import wrap_untrusted

_INBOUND_LABEL = "外部入站消息（已清洗）"


def isolate_for_prompt(
    text: str,
    cfg: GuardConfig | None = None,
    assessment: Assessment | None = None,
) -> str:
    """返回应送入 LLM 的消息正文。

    Args:
        text: 原始入站消息正文。
        cfg: 防护策略；None 时用默认值。
        assessment: 已算好的评估结果（避免入站层与 prompt 层重复计算）；None 时现算。
    """
    cfg = cfg or GuardConfig()
    if not cfg.enabled or cfg.mode == "observe" or not text:
        return text
    result = assessment or assess(text, cfg)
    # low（行首角色标签 / 零宽字符 / 超长等）只记录展示，正文零改写 ——
    # 这类形态在正常贴日志、贴配置时可能出现，改写即为误伤。
    if result.risk not in ("medium", "high"):
        return text
    cleaned = sanitize(text, result, cfg)
    if cfg.wrap_untrusted_inbound:
        return wrap_untrusted(cleaned, _INBOUND_LABEL)
    return cleaned


__all__ = ["isolate_for_prompt"]
