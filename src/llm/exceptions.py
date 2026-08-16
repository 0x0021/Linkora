"""LLM 处理层异常（P0-2）。

- LLMProcessingError：LLM 调用（含主模型 + 备用模型）彻底失败，且无法就地恢复。
  由 agent.process_message 在 chat 抛 RuntimeError（主+备均失败）时抛出，
  供 main 层捕获并落死信队列（DLQ），而非静默丢弃。

继承说明（2026-08-15 收敛）：
本模块原有两类异常直接继承裸 ``Exception``，与 ``src/exceptions.py`` 的统一异常
体系（``LinkoraError`` 根）脱节，导致入口无法统一 catch、日志无法按层级过滤。
现将二者收敛为继承 ``src.exceptions.LLMError``（其根为 ``LinkoraError``），
构造签名保持不变、``__str__`` 仍直接返回 ``self.message``（与裸 ``Exception`` 行为一致）。

注意：``LLMRateLimitExhaustedError`` 与 ``LLMProcessingError`` 同为 ``LLMError`` 的
**平级**子类，**不**继承 ``LLMProcessingError``——保持独立语义：限频是
「可恢复、待重放」，真崩溃是「需兜底」。若继承，会被 main 的
``isinstance(e, LLMProcessingError)`` 通用分支在 DLQ 关闭时套用
default_fallback 回复，违背「限频不回复」的诉求。
"""
from __future__ import annotations

from typing import Optional

from src.exceptions import LLMError


class LLMProcessingError(LLMError):
    """LLM 推理彻底失败，需要上层（main）决定是否落 DLQ。"""

    def __init__(self, message: str, *, original: Optional[Exception] = None,
                 stage: str = "llm_inference"):
        super().__init__(message)
        self.message = message
        self.original = original
        # 失败阶段：llm_inference（主+备 LLM 均失败）/ tool_exec（工具调用失败）等
        self.stage = stage

    def __str__(self) -> str:  # noqa: D401
        return self.message


class LLMRateLimitExhaustedError(LLMError):
    """主模型池 + 跨服务商备用池全部因限频（429 / rate_limit）耗尽。

    与 LLMProcessingError 的关键区别：这是「临时性、可恢复」的故障，
    上层（main）**不**向用户回复（避免刷屏/误导），而是打印日志并计入死信
    队列（DLQ），由管理员在管理台手动重放。

    注意：本类**不**继承 LLMProcessingError——保持独立语义：限频是
    「可恢复、待重放」，真崩溃是「需兜底」。若继承，会被 main 的
    ``isinstance(e, LLMProcessingError)`` 通用分支在 DLQ 关闭时套用
    default_fallback 回复，违背「限频不回复」的诉求。
    """

    def __init__(self, message: str, *, original: Optional[Exception] = None,
                 stage: str = "rate_limit"):
        super().__init__(message)
        self.message = message
        self.original = original
        # 失败阶段：rate_limit（主+备 LLM 均因限频失败）
        self.stage = stage

    def __str__(self) -> str:  # noqa: D401
        return self.message
