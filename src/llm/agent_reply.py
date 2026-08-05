"""AgentReply 数据类。

独立模块，避免 src.llm.agent 与 src.llm.agent_steps 之间的循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.llm.style import Citation


@dataclass
class AgentReply:
    """process_message 的返回契约。

    - text: 需要由 poller(main.py) 调用 _send_reply 发送的回复文本。为空表示无需发送。
    - already_sent: 是否已通过 send_message 工具直接发送给当前会话。
      为 True 时必须跳过 poller 的二次发送，否则会向同一会话发两条消息（双重回复）。

    注：流式内容不经本契约传递——agent 内部在流式模式下直接经 IM 适配器逐段下发，
    process_message 仍只返回终态 AgentReply。（曾有 stream_chunks 字段，全仓无任何
    读写点，已移除；勿再新增"返回迭代器"的旁路契约。）
    """
    text: str = ""
    already_sent: bool = False
    routing_mode: str | None = None
    routed_tools: list[str] | None = None
    skill_name: str | None = None
    skill_source: str | None = None
    confidence: float | None = None
    evidence_source: str | None = None
    citations: list[Citation] = field(default_factory=list)
    best_chunk: str | None = None
