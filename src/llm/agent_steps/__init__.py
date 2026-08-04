"""Agent 子步骤模块 — 从 src.llm.agent 拆出的各阶段实现。

各子模块：
- rag_fallback:   _apply_rag_empty_fallback  (三级递进 RAG 空结果处理)
- skill:          _activate_skills / _apply_skill_round_limit
- routing_trace:  _record_routing_trace_pre / _finalize_trace
                  _maybe_converge_tools / _build_tool_call_assistant_message
                  _handle_discarded_tool_calls
- stream:         _detect_stream_support / _handle_stream_response
- reply:          _make_reply / _finish_reply
                  process_message (主编排器)
                  extract_memories_from_conversation / summarize_conversation
"""
from __future__ import annotations

from .rag_fallback import apply_rag_empty_fallback
from .skill import activate_skills, apply_skill_round_limit
from .routing_trace import (
    build_tool_call_assistant_message,
    finalize_trace,
    handle_discarded_tool_calls,
    maybe_converge_tools,
    record_routing_trace_pre,
)
from .stream import detect_stream_support, handle_stream_response
from .reply import (
    finish_reply,
    make_reply,
    process_message,
    summarize_conversation,
    extract_memories_from_conversation,
)

__all__ = [
    "apply_rag_empty_fallback",
    "activate_skills",
    "apply_skill_round_limit",
    "build_tool_call_assistant_message",
    "finalize_trace",
    "handle_discarded_tool_calls",
    "maybe_converge_tools",
    "record_routing_trace_pre",
    "detect_stream_support",
    "handle_stream_response",
    "make_reply",
    "finish_reply",
    "process_message",
    "summarize_conversation",
    "extract_memories_from_conversation",
]
