"""LLMClient._do_chat 校验 tool_call.name 必须在 schema 中。

【背景】OpenAI SDK 透传 LLM 返回的 tool_call.name，不校验是否在传入的 tools schema 中。
工具收敛护栏把 web_search 从 schema 移除后，弱模型仍可能"幻觉"一个 web_search tool_call，
被透传到执行器 → 工具"复活"继续乱搜。

【覆盖】
1. 合法 + 编造混合 → 编造的丢弃、合法的保留
2. 全部编造 → tool_calls=[]、finish_reason 保留原值（不强行改成 "stop"）
3. kwargs 里无 tools（普通对话）→ 任何 tool_call 都丢弃
4. warning 日志包含模型名 + 编造的工具名
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock


from src.config import LlmConfig
from src.llm.client import LLMClient


def _make_llm_client() -> LLMClient:
    cfg = LlmConfig()
    cfg.model = "test-model"
    cfg.api_key = "test-key"
    cfg.base_url = "http://fake/v1"
    cfg.max_tokens = 100
    cfg.temperature = 0.5
    return LLMClient(cfg)


def _tc(name: str, args: str = "{}"):
    """构造 OpenAI SDK 风格的 tool_call mock。"""
    tc = MagicMock()
    tc.id = f"call_{name}"
    tc.function.name = name
    tc.function.arguments = args
    return tc


def _fake_response(tool_names: list[str], finish_reason: str = "tool_calls", content=None):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = [_tc(n) for n in tool_names] or None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def test_mixed_valid_and_fabricated_drops_only_fabricated():
    """合法 + 编造混合时：合法的保留（按 schema 顺序无关），编造的丢弃。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _fake_response(
        ["send_message", "web_search", "kb_search"]
    )
    tools = [
        {"type": "function", "function": {"name": "send_message"}},
        {"type": "function", "function": {"name": "kb_search"}},
    ]
    resp = client._do_chat(openai_client, {"model": "gpt-4", "tools": tools})
    assert [t["name"] for t in resp.tool_calls] == ["send_message", "kb_search"]
    assert resp.finish_reason == "tool_calls"


def test_all_fabricated_drops_all_preserves_finish_reason():
    """所有 tool_call 都被丢弃时：tool_calls=[]，finish_reason 保留原值（不改成 "stop"）。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _fake_response(
        ["web_search", "kb_search_old"]
    )
    tools = [
        {"type": "function", "function": {"name": "send_message"}},
    ]
    resp = client._do_chat(openai_client, {"model": "gpt-4", "tools": tools})
    assert resp.tool_calls == []
    # 关键：finish_reason 必须保留 LLM 原始值（"tool_calls"），不能改成 "stop"。
    # 否则上层会误以为"对话正常结束"而失去"LLM 试图调工具"这一信号。
    assert resp.finish_reason == "tool_calls"


def test_no_tools_kwarg_drops_all_tool_calls():
    """kwargs 里没有 tools（普通对话）→ 任何 tool_call 都丢弃。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _fake_response(["web_search"])
    resp = client._do_chat(openai_client, {"model": "gpt-4"})  # 无 tools
    assert resp.tool_calls == []
    assert resp.finish_reason == "tool_calls"


def test_empty_tools_list_drops_all_tool_calls():
    """kwargs['tools']=[] 显式空列表 → 任何 tool_call 都丢弃。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _fake_response(["send_message"])
    resp = client._do_chat(openai_client, {"model": "gpt-4", "tools": []})
    assert resp.tool_calls == []


def test_tools_none_drops_all_tool_calls():
    """kwargs['tools']=None → 任何 tool_call 都丢弃（防御 NoneType）。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _fake_response(["send_message"])
    resp = client._do_chat(openai_client, {"model": "gpt-4", "tools": None})
    assert resp.tool_calls == []


def test_warning_log_contains_model_and_fabricated_name(caplog):
    """warning 日志应同时包含模型名和编造的工具名（便于事后定位）。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _fake_response(["web_search"])
    tools = [{"type": "function", "function": {"name": "send_message"}}]
    with caplog.at_level(logging.WARNING, logger="src.llm.client"):
        client._do_chat(openai_client, {"model": "main-model", "tools": tools})
    msgs = [r.getMessage() for r in caplog.records]
    assert any("main-model" in m and "web_search" in m for m in msgs), (
        f"warning should mention model + fabricated name, got: {msgs}"
    )


def test_bug_scenario_converged_ghost_web_search():
    """Bug 原始场景：工具收敛已移除 web_search，LLM 仍幻觉 web_search tool_call → 丢弃。"""
    client = _make_llm_client()
    openai_client = MagicMock()
    # 模拟 22 工具的收敛后 schema（不含 web_search）
    converged_tools = [
        {"type": "function", "function": {"name": f"tool_{i:02d}"}} for i in range(22)
    ]
    schema_names = {t["function"]["name"] for t in converged_tools}
    assert "web_search" not in schema_names, "test pre-condition: web_search 不在 schema"

    # LLM 仍幻觉一个 web_search tool_call
    openai_client.chat.completions.create.return_value = _fake_response(
        ["web_search"], content=None
    )
    resp = client._do_chat(openai_client, {"model": "main-model", "tools": converged_tools})
    assert resp.tool_calls == [], "执行器不应看到 web_search tool_call"
    assert resp.finish_reason == "tool_calls"  # 保留原始 signal
