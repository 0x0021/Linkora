"""T-A1: 工具平台门控（platforms）声明验证。

验证 GetCalendarEventsTool / CreateTodoTool / GetConversationInfoTool 已声明正确的
platforms，且 is_tool_for_platform 在对应平台上正确暴露/隐藏，避免把底层适配器缺方法的
工具暴露给 LLM 导致运行时报错。

钉钉/飞书/企微适配器方法支持矩阵（已实测）：
- calendar_event_list  : 钉钉 ✅ / 飞书 ❌ / 企微 ✅
- todo_task_create     : 钉钉 ✅ / 飞书 ❌ / 企微 ✅
- chat_conversation_info: 钉钉 ✅ / 飞书 ✅ / 企微 ❌
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.llm.router import is_tool_for_platform
from src.tools.calendar import CreateTodoTool, GetCalendarEventsTool
from src.tools.conversation import (
    GetConversationInfoTool,
    GetUnreadTool,
    SearchMessagesTool,
)


def _make_agent(platform_id: str, tools: dict) -> SimpleNamespace:
    """造最小 agent stub：platform_id + tool_router._tools（参考 test_rag_state_reset 写法）。"""
    router = SimpleNamespace(_tools=tools, config=None)
    return SimpleNamespace(platform_id=platform_id, tool_router=router)


def _build_tools() -> dict:
    """构造全部相关工具实例（dws 用 MagicMock，is_tool_for_platform 不触发 execute）。"""
    dws = MagicMock()
    return {
        "get_calendar_events": GetCalendarEventsTool(dws),
        "create_todo": CreateTodoTool(dws),
        "get_conversation_info": GetConversationInfoTool(dws),
        "get_unread": GetUnreadTool(dws),
        "search_messages": SearchMessagesTool(dws),
    }


def test_tool_platforms_declared():
    """三个工具已声明正确的 platforms 列表。"""
    assert GetCalendarEventsTool.platforms == ["dingtalk", "wecom"]
    assert CreateTodoTool.platforms == ["dingtalk", "wecom"]
    assert GetConversationInfoTool.platforms == ["dingtalk", "feishu"]


def test_feishu_cannot_see_dingtalk_wecom_only_tools():
    """飞书 agent 拿不到 get_calendar_events / create_todo，但能拿到 get_conversation_info。"""
    agent = _make_agent("feishu", _build_tools())
    assert is_tool_for_platform(agent, "get_calendar_events") is False
    assert is_tool_for_platform(agent, "create_todo") is False
    assert is_tool_for_platform(agent, "get_conversation_info") is True
    # 三平台齐备的工具不受影响
    assert is_tool_for_platform(agent, "get_unread") is True
    assert is_tool_for_platform(agent, "search_messages") is True


def test_wecom_cannot_see_dingtalk_feishu_only_tool():
    """企微 agent 拿不到 get_conversation_info，但能拿到 get_calendar_events / create_todo。"""
    agent = _make_agent("wecom", _build_tools())
    assert is_tool_for_platform(agent, "get_calendar_events") is True
    assert is_tool_for_platform(agent, "create_todo") is True
    assert is_tool_for_platform(agent, "get_conversation_info") is False


def test_dingtalk_sees_all_three():
    """钉钉 agent 三个工具都拿得到。"""
    agent = _make_agent("dingtalk", _build_tools())
    assert is_tool_for_platform(agent, "get_calendar_events") is True
    assert is_tool_for_platform(agent, "create_todo") is True
    assert is_tool_for_platform(agent, "get_conversation_info") is True
