"""执行层平台门控兜底（T-A1 补强）。

验证 ToolOrchestrator.execute_tool_calls 在工具真正执行前按当前平台拦截被门控的工具：
schema 层（router.filter_schemas_by_platform）已把不可用工具从 LLM 视野中剔除，但若 LLM
凭历史上下文或幻觉直接吐出被过滤的工具名，执行链不能静默放行——基类适配器（如企微
mark_read / chat_conversation_info）是空实现返回 {}，一旦执行就是「成功但空结果」且无
任何错误痕迹。本门控将此类调用转为显式失败并返回给 LLM，语义与 router.is_tool_for_platform
完全一致：platform_id 为假值或工具 platforms 为空列表（全平台）时放行。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.llm.router import is_tool_for_platform
from src.llm.tool_orchestrator import ToolOrchestrator
from src.tools.calendar import GetCalendarEventsTool
from src.tools.conversation import GetConversationInfoTool, GetUnreadTool


def _make_agent(platform_id: str, tools: dict, blocked: dict[str, bool]) -> MagicMock:
    """构造 orchestrator 所需的最小 agent mock。

    _is_tool_for_platform 接到真实的 router.is_tool_for_platform（判据与生产同源），
    tool_router.execute 用 spy，便于断言「是否真的执行到工具」。
    """
    router = MagicMock()
    router._tools = tools
    agent = MagicMock()
    agent.platform_id = platform_id
    agent.tool_router = router
    agent._is_tool_for_platform.side_effect = lambda name: is_tool_for_platform(agent, name)
    return agent


def _build_tools() -> dict:
    dws = MagicMock()
    return {
        "get_calendar_events": GetCalendarEventsTool(dws),
        "get_conversation_info": GetConversationInfoTool(dws),
        "get_unread": GetUnreadTool(dws),
    }


def _msg() -> SimpleNamespace:
    return SimpleNamespace(chat_id="chat-1", sender_id="user-1", sender_name="某人")


def _call(tool_name: str, cid: str = "call-1") -> dict:
    return {"id": cid, "name": tool_name, "args": {}}


def test_wecom_blocks_conversation_info_without_executing():
    """企微上调 get_conversation_info（platforms=[dingtalk,feishu]）→ 显式失败且不执行。"""
    agent = _make_agent("wecom", _build_tools(), {})
    orch = ToolOrchestrator(agent)
    results, _ = orch.execute_tool_calls([_call("get_conversation_info")], _msg())

    assert len(results) == 1
    assert results[0]["role"] == "tool"
    assert results[0]["tool_call_id"] == "call-1"
    payload = results[0]["content"]
    assert '"success": false' in payload
    assert "wecom" in payload
    assert "get_conversation_info" in payload
    # 关键：工具没有真的执行到
    agent.tool_router.execute.assert_not_called()


def test_dingtalk_allows_conversation_info():
    """钉钉上同一工具正常放行并执行。"""
    agent = _make_agent("dingtalk", _build_tools(), {})
    orch = ToolOrchestrator(agent)
    results, _ = orch.execute_tool_calls([_call("get_conversation_info")], _msg())

    assert len(results) == 1
    agent.tool_router.execute.assert_called_once()
    assert agent.tool_router.execute.call_args.args[0] == "get_conversation_info"


def test_empty_platforms_tool_allowed_everywhere():
    """platforms 为空列表（全平台）的工具在任意平台放行。"""
    for pid in ("dingtalk", "feishu", "wecom"):
        agent = _make_agent(pid, _build_tools(), {})
        orch = ToolOrchestrator(agent)
        results, _ = orch.execute_tool_calls([_call("get_unread")], _msg())
        assert len(results) == 1
        agent.tool_router.execute.assert_called_once_with(
            "get_unread", {}, session_key="chat-1")


def test_falsy_platform_id_skips_gate():
    """platform_id 为假值（None / 空串）时不门控，保持现有路径与后台任务不受影响。"""
    for pid in (None, ""):
        agent = _make_agent(pid, _build_tools(), {})
        orch = ToolOrchestrator(agent)
        results, _ = orch.execute_tool_calls([_call("get_conversation_info")], _msg())
        assert len(results) == 1
        agent.tool_router.execute.assert_called_once()


def test_mixed_batch_gates_only_blocked_tool_and_keeps_call_ids():
    """混合批次：被门控的工具返回错误、其余照常执行，tool_call_id 一一对应不错位。"""
    agent = _make_agent("wecom", _build_tools(), {})
    orch = ToolOrchestrator(agent)
    calls = [
        _call("get_conversation_info", "call-a"),  # 企微被门控
        _call("get_calendar_events", "call-b"),    # 企微可用
        _call("get_unread", "call-c"),             # 全平台
    ]
    results, _ = orch.execute_tool_calls(calls, _msg())

    assert len(results) == 3
    by_id = {r["tool_call_id"]: r for r in results}
    assert set(by_id) == {"call-a", "call-b", "call-c"}
    # call-a 被门控：失败且未执行
    assert '"success": false' in by_id["call-a"]["content"]
    # call-b / call-c 真实执行（router.execute 收到两次调用）
    assert agent.tool_router.execute.call_count == 2
    executed = [c.args[0] for c in agent.tool_router.execute.call_args_list]
    assert set(executed) == {"get_calendar_events", "get_unread"}
