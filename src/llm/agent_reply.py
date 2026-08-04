"""AgentReply 数据类。

独立模块，避免 src.llm.agent 与 src.llm.agent_steps 之间的循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from src.llm.style import Citation


@dataclass
class AgentReply:
    """process_message 的返回契约。

    - text: 需要由 poller(main.py) 调用 _send_reply 发送的回复文本。为空表示无需发送。
    - already_sent: 是否已通过 send_message 工具直接发送给当前会话。
      为 True 时必须跳过 poller 的二次发送，否则会向同一会话发两条消息（双重回复）。
    - stream_chunks: 流式输出的内容块迭代器（仅在启用流式时有效）。
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
    stream_chunks: Iterator[str] | None = None
