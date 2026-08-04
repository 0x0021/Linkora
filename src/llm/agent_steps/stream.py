"""流式响应处理。

从 src.llm.agent._detect_stream_support / _handle_stream_response 拆出。
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

from src.llm.client import LLMStreamChunk
from src.llm.style import gate_reply, enforce_brevity

logger = logging.getLogger(__name__)


def detect_stream_support(agent, enable_stream: bool) -> bool:
    """判定本轮能否走流式：需启用流式且 IM 适配器支持增量更新。"""
    stream_supported = enable_stream and agent.im_adapter is not None
    if stream_supported:
        try:
            stream_supported = hasattr(agent.im_adapter, "chat_message_update")
        except Exception as _exc:
            logger.debug(f"_detect_stream_support: swallowed exception: {_exc}")
            stream_supported = False
    return stream_supported


def handle_stream_response(
    stream,
    message,
    agent,
) -> Iterator[str]:
    """处理流式 LLM 响应，发送占位消息并逐步更新。

    委托给 src.llm.stream_helper.handle_stream_response，
    传入 agent 上的方法引用以维持与 agent._handle_stream_response 的接口一致。
    """
    from src.llm.stream_helper import handle_stream_response as _helper
    yield from _helper(
        stream,
        message,
        agent.im_adapter,
        enforce_brevity_fn=agent._enforce_brevity,
        ensure_complete_fn=agent._ensure_complete_reply,
        gate_reply_fn=lambda reply, user_name, user_title: gate_reply(reply, user_name, user_title),
    )
