"""对话会话工具单元测试。

覆盖：
- _message_preview 辅助函数（文本/图片/URL/非dict/null）
- GetUnreadTool.execute（正常/异常/空结果/字段fallback）
- GetConversationInfoTool.execute（正常/缺chat_id/非dict返回/成员字段fallback）
- SearchMessagesTool.execute（single/group/缺chat_id/关键词过滤/异常）
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ============================================================================
# _message_preview
# ============================================================================
class TestMessagePreview:
    def test_plain_text(self):
        from src.tools.conversation import _message_preview
        assert _message_preview({"content": "hello"}) == "hello"

    def test_image_placeholder(self):
        from src.tools.conversation import _message_preview
        assert _message_preview({"content": "mediaId=abc123"}) == "[图片/文件]"

    def test_url_content(self):
        from src.tools.conversation import _message_preview
        assert _message_preview({"content": "https://example.com/img.jpg"}) == "https://example.com/img.jpg"

    def test_non_string_content(self):
        from src.tools.conversation import _message_preview
        assert _message_preview({"content": 123}) == "123"

    def test_non_dict_input(self):
        from src.tools.conversation import _message_preview
        assert _message_preview(None) == ""
        assert _message_preview([]) == ""
        assert _message_preview("string") == ""

    def test_missing_content_key(self):
        from src.tools.conversation import _message_preview
        assert _message_preview({}) == ""


# ============================================================================
# 公共 fixture
# ============================================================================
@pytest.fixture
def dws():
    return MagicMock()


# ============================================================================
# GetUnreadTool
# ============================================================================
class TestGetUnreadTool:
    @pytest.fixture
    def tool(self, dws):
        from src.tools.conversation import GetUnreadTool
        return GetUnreadTool(dws)

    def test_success(self, tool, dws):
        dws.chat_message_list_unread_conversations.return_value = [
            {
                "title": "技术交流群",
                "openConversationId": "cid1",
                "sender": "张三",
                "unreadCount": 5,
                "messages": [
                    {"content": "VPN连不上"},
                    {"content": "已修复"},
                ],
            },
            {
                "title": "李四",
                "openConversationId": "cid2",
                "senderName": "李四",
                "unread": 3,
                "latestMessage": {"content": "明天开会"},
                "messages": [],
            },
        ]
        result = tool.execute({})
        assert result["count"] == 2
        items = result["conversations"]
        assert items[0]["title"] == "技术交流群"
        assert items[0]["unread_count"] == 5
        assert items[0]["preview"] == "已修复"
        assert items[1]["preview"] == "明天开会"

    def test_count_parameter(self, tool, dws):
        dws.chat_message_list_unread_conversations.return_value = []
        tool.execute({"count": 10})
        dws.chat_message_list_unread_conversations.assert_called_with(10)

    def test_default_count(self, tool, dws):
        dws.chat_message_list_unread_conversations.return_value = []
        tool.execute({})
        dws.chat_message_list_unread_conversations.assert_called_with(20)

    def test_exception(self, tool, dws):
        dws.chat_message_list_unread_conversations.side_effect = RuntimeError("network")
        result = tool.execute({})
        assert "error" in result

    def test_empty_list(self, tool, dws):
        dws.chat_message_list_unread_conversations.return_value = []
        result = tool.execute({})
        assert result["count"] == 0
        assert result["conversations"] == []

    def test_non_dict_entries_skipped(self, tool, dws):
        dws.chat_message_list_unread_conversations.return_value = [
            "not a dict",
            {"title": "valid", "openConversationId": "c", "sender": "S", "unreadCount": 1},
        ]
        result = tool.execute({})
        assert result["count"] == 1

    def test_no_messages_fallback(self, tool, dws):
        """无 messages 字段时回退到 latestMessage。"""
        dws.chat_message_list_unread_conversations.return_value = [
            {"title": "X", "openConversationId": "c", "sender": "S",
             "unreadCount": 2, "latestMessage": None}
        ]
        result = tool.execute({})
        assert result["count"] == 1
        assert result["conversations"][0]["preview"] == ""


# ============================================================================
# GetConversationInfoTool
# ============================================================================
class TestGetConversationInfoTool:
    @pytest.fixture
    def tool(self, dws):
        from src.tools.conversation import GetConversationInfoTool
        return GetConversationInfoTool(dws)

    def test_success_group(self, tool, dws):
        dws.chat_conversation_info.return_value = {
            "title": "技术群",
            "singleChat": False,
            "ownerName": "张三",
            "members": [
                {"name": "张三", "userId": "u1"},
                {"name": "李四", "openDingTalkId": "od2"},
            ],
        }
        result = tool.execute({"chat_id": "cid"})
        assert result["title"] == "技术群"
        assert result["type"] == "group"
        assert result["owner"] == "张三"
        assert result["member_count"] == 2
        assert result["members"][0]["name"] == "张三"

    def test_success_single(self, tool, dws):
        dws.chat_conversation_info.return_value = {
            "title": "李四",
            "singleChat": True,
            "owner": "李四",
            "member_list": [{"nick_name": "李四", "userid": "u2"}],
        }
        result = tool.execute({"chat_id": "cid"})
        assert result["type"] == "single"
        assert result["members"][0]["name"] == "李四"

    def test_missing_chat_id(self, tool, dws):
        result = tool.execute({})
        assert "error" in result

    def test_empty_chat_id(self, tool, dws):
        result = tool.execute({"chat_id": "  "})
        assert "error" in result

    def test_info_not_dict(self, tool, dws):
        dws.chat_conversation_info.return_value = []
        result = tool.execute({"chat_id": "cid"})
        assert "error" in result

    def test_member_name_fallback(self, tool, dws):
        """成员名通过多种字段 fallback。"""
        dws.chat_conversation_info.return_value = {
            "title": "X", "members": [
                {"nickName": "A", "openDingTalkId": "odA"},
                {"userName": "B", "userId": "uB"},
                {"name": "C", "userid": "uC"},
                {},
            ],
        }
        result = tool.execute({"chat_id": "cid"})
        names = [m["name"] for m in result["members"]]
        assert names == ["A", "B", "C", ""]

    def test_limit_50_members(self, tool, dws):
        """超过50成员时截断。"""
        members = [{"name": f"u{i}", "userId": f"uid{i}"} for i in range(60)]
        dws.chat_conversation_info.return_value = {"title": "大群", "members": members}
        result = tool.execute({"chat_id": "cid"})
        assert len(result["members"]) == 50

    def test_member_list_fallback(self, tool, dws):
        """成员列表 fallback：memberList → member_list。"""
        dws.chat_conversation_info.return_value = {
            "title": "X",
            "memberList": [{"name": "A", "userId": "u1"}],
        }
        result = tool.execute({"chat_id": "cid"})
        assert result["member_count"] == 1
        assert result["members"][0]["name"] == "A"

    def test_non_dict_member_skipped(self, tool, dws):
        dws.chat_conversation_info.return_value = {
            "title": "X",
            "members": ["bad", {"name": "OK", "userId": "u1"}],
        }
        result = tool.execute({"chat_id": "cid"})
        assert len(result["members"]) == 1


# ============================================================================
# SearchMessagesTool
# ============================================================================
class TestSearchMessagesTool:
    @pytest.fixture
    def tool(self, dws):
        from src.tools.conversation import SearchMessagesTool
        return SearchMessagesTool(dws)

    def test_single_chat(self, tool, dws):
        dws.chat_message_list_direct.return_value = [
            {"sender": "李四", "time": "2026-07-11", "content": "明天开会"},
        ]
        result = tool.execute({"chat_id": "od1", "chat_type": "single"})
        assert result["count"] == 1
        assert result["messages"][0]["sender"] == "李四"

    def test_group_chat(self, tool, dws):
        dws.chat_message_list.return_value = [
            {"sender": "张三", "createTime": "2026-07-10", "content": "来了"},
        ]
        result = tool.execute({"chat_id": "g1", "chat_type": "group"})
        assert result["count"] == 1
        assert result["messages"][0]["content"] == "来了"

    def test_missing_chat_id(self, tool, dws):
        result = tool.execute({"chat_type": "group"})
        assert "error" in result

    def test_keyword_filter(self, tool, dws):
        dws.chat_message_list.return_value = [
            {"sender": "张工", "content": "VPN故障处理"},
            {"sender": "李工", "content": "打印机没问题"},
        ]
        result = tool.execute({"chat_id": "g", "chat_type": "group", "keyword": "vpn"})
        assert result["count"] == 1
        assert "VPN" in result["messages"][0]["content"]

    def test_keyword_in_sender(self, tool, dws):
        """关键词也检查 sender 字段。"""
        dws.chat_message_list.return_value = [
            {"sender": "VPN运维", "content": "好的"},
            {"sender": "张三", "content": "收到"},
        ]
        result = tool.execute({"chat_id": "g", "chat_type": "group", "keyword": "vpn"})
        assert result["count"] == 1
        assert result["messages"][0]["sender"] == "VPN运维"

    def test_exception(self, tool, dws):
        dws.chat_message_list.side_effect = RuntimeError("timeout")
        result = tool.execute({"chat_id": "g", "chat_type": "group"})
        assert "error" in result

    def test_non_dict_entries_skipped(self, tool, dws):
        dws.chat_message_list.return_value = [
            None,
            {"sender": "王五", "content": "有用的消息"},
        ]
        result = tool.execute({"chat_id": "g", "chat_type": "group"})
        assert result["count"] == 1

    def test_default_chat_type_group(self, tool, dws):
        """不传 chat_type 时默认 group。"""
        dws.chat_message_list.return_value = [
            {"sender": "张三", "content": "hello"},
        ]
        result = tool.execute({"chat_id": "g"})
        assert result["count"] == 1

    def test_time_and_limit_passed(self, tool, dws):
        """time 和 limit 参数透传。"""
        dws.chat_message_list.return_value = []
        tool.execute({"chat_id": "g", "chat_type": "group", "time": "2026-07-01", "limit": 20})
        dws.chat_message_list.assert_called_with(group="g", time_str="2026-07-01", limit=20)

    def test_image_preview_in_search(self, tool, dws):
        """mediaId 占位符在搜索中正确处理。"""
        dws.chat_message_list.return_value = [
            {"sender": "张三", "content": "mediaId=img001"},
        ]
        result = tool.execute({"chat_id": "g", "chat_type": "group"})
        assert result["messages"][0]["content"] == "[图片/文件]"
