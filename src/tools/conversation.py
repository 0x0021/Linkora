from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError
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
            },
            "include_members": {
                "type": "boolean",
                "description": "是否列出群成员（默认 true；仅群聊有效，成员过多时只返回前 50 名）",
            },
        },
        "required": ["chat_id"],
    }

    # 成员列表最大返回条数（避免 1000+ 人的大群把上下文撑爆）
    _MAX_MEMBERS = 50

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        chat_id = (args.get("chat_id") or "").strip()
        if not chat_id:
            return {"error": "chat_id 不能为空"}
        include_members = args.get("include_members", True)
        if isinstance(include_members, str):
            include_members = include_members.strip().lower() not in ("false", "0", "no")

        try:
            info = self.dws.chat_conversation_info(chat_id)
        except Exception as e:
            logger.exception("获取会话信息失败: %s", e)
            return {"error": f"获取会话信息失败: {e}"}
        if not isinstance(info, dict):
            return {"error": "未获取到会话信息"}

        is_single = bool(info.get("singleChat"))
        # 成员明细：优先用会话信息里**内嵌**的成员列表（飞书 chat_conversation_info
        # 会直接返回 members / memberList / member_list）；钉钉 dws 的
        # conversation-info 只给 memberCount、不含明细，此时再走成员枚举通道。
        inline_members = (
            info.get("members")
            or info.get("memberList")
            or info.get("member_list")
            or []
        )
        member_list = [
            {
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
                    or m.get("openDingtalkId")
                    or m.get("userid")
                    or ""
                ),
                "role": m.get("role") or m.get("memberRoleDesc") or "",
            }
            for m in (inline_members or [])[: self._MAX_MEMBERS]
            if isinstance(m, dict)
        ]
        result = {
            "title": info.get("title", "") or info.get("name", ""),
            "chat_id": chat_id,
            "type": "single" if is_single else "group",
            "owner": (
                info.get("owner") or info.get("ownerName") or info.get("owner_name")
                or info.get("ownerNick") or ""
            ),
            # 人数优先取接口给的 memberCount；没有则用内嵌名单长度。
            "member_count": int(info.get("memberCount") or 0) or len(inline_members or []),
            "members": member_list,
            "bots": [],
            # 内嵌名单由会话信息接口直接给出，视为已完整；枚举通道会按实际完整性覆盖。
            "members_complete": bool(member_list),
        }

        # 内嵌名单为空且是群聊 → 走成员枚举（``chat +chat-members-list``）。
        # 单聊无成员列表概念；显式关闭或枚举失败都不影响基础信息返回。
        if include_members and not is_single and not member_list:
            result.update(self._fetch_members(chat_id))
        return result

    def _fetch_members(self, chat_id: str) -> dict:
        """枚举群成员（真人 / 机器人分桶），失败时降级为「人数已知、名单未知」。"""
        try:
            data = self.dws.chat_group_members(chat_id)
        except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
            # 成员枚举是增强信息，失败不应让整个工具失败——降级返回已知的 memberCount。
            # 精确捕获（不新增宽异常）：IMAdapterError=CLI 失败、ValueError=参数校验、
            # OSError=文件 IO、RuntimeError=适配器内部错误；其余异常属 Bug，上抛。
            logger.warning("[get_conversation_info] 群成员枚举失败（不影响基础信息）: %s", e)
            return {"members": [], "members_complete": False, "bots": []}
        if not isinstance(data, dict):
            return {"members": [], "members_complete": False, "bots": []}

        users = data.get("users") or []
        bots = data.get("bots") or []
        member_list = [
            {
                "name": m.get("name", ""),
                "userid": m.get("open_dingtalk_id", ""),
                "role": m.get("role", ""),
            }
            for m in users[: self._MAX_MEMBERS]
            if isinstance(m, dict)
        ]
        bot_list = [
            {
                "name": m.get("name", ""),
                "userid": m.get("open_dingtalk_id", ""),
            }
            for m in bots[: self._MAX_MEMBERS]
            if isinstance(m, dict)
        ]
        return {
            "members": member_list,
            "bots": bot_list,
            # complete=False 表示名单被截断或有分页失败，不能当「群里就这些人」。
            "members_complete": bool(data.get("complete", False)),
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
            logger.warning("broad except swallowed in execute() @ src/tools/conversation.py:199, see exc_info", exc_info=True)
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
