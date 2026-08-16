from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool
from src.tools.utils import safe_int

logger = logging.getLogger(__name__)


def _message_preview(raw: dict | None) -> str:
    """从原始消息里提取可读文本。图片/文件回退为占位，卡片 JSON 转为字符串。"""
    if not isinstance(raw, dict):
        return ""
    content = raw.get("content") or ""
    if isinstance(content, str):
        if content.startswith("mediaId="):
            return "[图片/文件]"
        if content.startswith("http://") or content.startswith("https://"):
            return content
        return content
    return str(content)


class GetUnreadTool(BaseTool):
    name = "get_unread"
    display_name = "查询未读消息"
    short_description = "汇总当前用户所有未读会话及未读消息摘要，便于快速回顾遗漏的信息"
    description = "查询当前用户的未读会话与未读消息摘要"
    # 场景关键词统一维护在 IntentRegistry 的 domain.unread（单一真源）
    intent_categories = ["domain.unread"]
    parameters = {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "返回的未读会话数量上限，默认 20",
            }
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        count = safe_int(args.get("count"), 20) or 20
        try:
            convs = self.dws.chat_message_list_unread_conversations(count)
        except Exception as e:
            logger.exception("获取未读会话失败: %s", e)
            return {"error": f"获取未读会话失败: {e}"}

        items = []
        for c in (convs or []):
            if not isinstance(c, dict):
                continue
            msgs = c.get("messages") or []
            latest = msgs[-1] if msgs else (c.get("latestMessage") or {})
            items.append({
                "title": c.get("title", ""),
                "chat_id": c.get("openConversationId", ""),
                "sender": c.get("sender") or c.get("senderName") or "",
                "unread_count": c.get("unreadCount") or c.get("unread") or (len(msgs) if msgs else 0),
                "preview": _message_preview(latest),
            })
        return {"count": len(items), "conversations": items}


class GetConversationInfoTool(BaseTool):
    name = "get_conversation_info"
    display_name = "查询会话/群信息"
    short_description = "查询指定会话（群聊或单聊）的详情，包括群主、成员列表与人数，便于了解群结构与成员"
    description = "查询指定会话（群聊或单聊）的详细信息"
    # 场景关键词统一维护在 IntentRegistry 的 domain.conversation_info（单一真源）
    intent_categories = ["domain.conversation_info"]
    # 企微适配器无 chat_conversation_info，仅钉钉/飞书可用
    platforms = ["dingtalk", "feishu"]
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "会话 ID（openConversationId）",
            }
        },
        "required": ["chat_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        chat_id = (args.get("chat_id") or "").strip()
        if not chat_id:
            return {"error": "chat_id 不能为空"}

        try:
            info = self.dws.chat_conversation_info(chat_id)
        except Exception as e:
            logger.exception("获取会话信息失败: %s", e)
            return {"error": f"获取会话信息失败: {e}"}
        if not isinstance(info, dict):
            return {"error": "未获取到会话信息"}

        members = (
            info.get("members")
            or info.get("memberList")
            or info.get("member_list")
            or []
        )
        member_list = []
        for m in (members or [])[:50]:
            if not isinstance(m, dict):
                continue
            member_list.append({
                "name": (
                    m.get("name")
                    or m.get("nickName")
                    or m.get("userName")
                    or m.get("nick_name")
                    or ""
                ),
                "userid": (
                    m.get("userId")
                    or m.get("openDingTalkId")
                    or m.get("userid")
                    or ""
                ),
            })
        return {
            "title": info.get("title", ""),
            "chat_id": chat_id,
            "type": "single" if info.get("singleChat") else "group",
            "owner": info.get("owner") or info.get("ownerName") or info.get("owner_name") or "",
            "member_count": len(members),
            "members": member_list,
        }


class SearchMessagesTool(BaseTool):
    name = "search_messages"
    display_name = "检索历史消息"
    short_description = "在指定会话（群聊或单聊）中按关键词检索历史消息，可按时间范围过滤，快速定位过往聊天内容"
    description = "在指定会话中按关键词检索历史消息"
    # 场景关键词统一维护在 IntentRegistry 的 domain.search_messages（单一真源）
    intent_categories = ["domain.search_messages"]
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "会话 ID（openConversationId）；单聊也可直接传对方 openDingTalkId",
            },
            "chat_type": {
                "type": "string",
                "enum": ["group", "single"],
                "description": "会话类型：group=群聊，single=单聊",
            },
            "keyword": {
                "type": "string",
                "description": "可选关键词，仅返回包含该词的消息（不区分大小写）",
            },
            "time": {
                "type": "string",
                "description": "可选时间锚点，如 2026-07-01；不填则取最近",
            },
            "limit": {
                "type": "integer",
                "description": "返回消息条数上限，默认 50",
            },
        },
        "required": ["chat_id", "chat_type"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        chat_id = (args.get("chat_id") or "").strip()
        chat_type = (args.get("chat_type") or "group").strip().lower()
        keyword = (args.get("keyword") or "").strip().lower()
        time_str = (args.get("time") or "").strip()
        limit = safe_int(args.get("limit"), 50) or 50

        if not chat_id:
            return {"error": "chat_id 不能为空"}

        try:
            if chat_type == "single":
                msgs = self.dws.chat_message_list_direct(
                    open_dingtalk_id=chat_id, time_str=time_str, limit=limit
                )
            else:
                msgs = self.dws.chat_message_list(
                    group=chat_id, time_str=time_str, limit=limit
                )
        except Exception as e:
            return {"error": f"获取消息失败: {e}"}

        messages = []
        for m in (msgs or []):
            if not isinstance(m, dict):
                continue
            preview = _message_preview(m)
            sender = m.get("sender") or m.get("senderName") or ""
            if keyword and keyword not in preview.lower() and keyword not in sender.lower():
                continue
            messages.append({
                "sender": sender,
                "time": m.get("time") or m.get("createTime") or m.get("timestamp") or "",
                "content": preview,
            })
        return {"count": len(messages), "messages": messages}
