"""AI 听记 / 会议纪要工具（domain.minutes）。

把 `src/dws_adapter.py` 里已实现但 0 引用的 `minutes_*` 方法接成 Agent 工具，
让数字分身在对话中能直接「列出听记 / 取摘要 / 取待办 / 取转写 / 取基础信息」，
支撑「对方问会议相关问题时自动给出有依据的内容」这一自动回复场景。

全部为只读操作，无需二次确认。
"""
from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)

# 转写原文可能长达几十万字，整段塞给 LLM 会冲爆上下文窗口并飙升 token 成本。
# 仅 transcription 维度截断（摘要/待办/基础信息本身已精简）。
MAX_TRANSCRIPT_CHARS = 6000


def _normalize_minutes(item: dict) -> dict:
    """把 dws 返回的听记条目规整为稳定字段，缺失时回退原始字典。"""
    if not isinstance(item, dict):
        return {"raw": item}
    return {
        "id": item.get("minutesId") or item.get("id") or item.get("minutes_id") or "",
        "title": item.get("title") or item.get("name") or item.get("subject") or "",
        "created_time": item.get("gmtCreate") or item.get("created") or item.get("createTime") or "",
        "creator": item.get("creator") or item.get("creatorName") or item.get("owner") or "",
        "status": item.get("status") or "",
    }


class ListMinutesTool(BaseTool):
    name = "list_minutes"
    display_name = "列出会议听记"
    short_description = "列出我有权限的 AI 听记/会议纪要（可按我创建的/他人共享的/全部筛选，支持关键词与时间范围）"
    description = "列出 AI 听记/会议纪要列表"
    # 场景关键词统一维护在 IntentRegistry 的 domain.minutes（单一真源）
    intent_categories = ["domain.minutes"]
    # 钉钉 AI 听记专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["mine", "shared", "all"],
                "description": "范围：mine=我创建的(默认)，shared=他人共享给我的，all=我有权限的全部",
            },
            "query": {
                "type": "string",
                "description": "关键词过滤（如会议主题、参会人），可选",
            },
            "start": {
                "type": "string",
                "description": "起始时间 YYYY-MM-DD 或 ISO-8601，可选",
            },
            "end": {
                "type": "string",
                "description": "结束时间 YYYY-MM-DD 或 ISO-8601，可选",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 10",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        scope = (args.get("scope") or "mine").strip() or "mine"
        if scope not in ("mine", "shared", "all"):
            scope = "mine"
        query = (args.get("query") or "").strip()
        start = (args.get("start") or "").strip()
        end = (args.get("end") or "").strip()
        limit = args.get("limit") or 10
        try:
            limit = int(limit)
        except (TypeError, ValueError) as _exc:
            logger.debug("execute: limit 解析失败，回退默认值: %s", _exc)
            limit = 10

        try:
            raw = self.dws.minutes_list(
                scope=scope, query=query or None,
                start=start or None, end=end or None, limit=limit,
            )
        except Exception as e:
            logger.exception("列出听记失败: %s", e)
            return {"error": f"列出听记失败: {e}"}

        items = [_normalize_minutes(it) for it in raw] if isinstance(raw, list) else []
        return {"count": len(items), "items": items[:limit]}


class GetMinutesTool(BaseTool):
    name = "get_minutes"
    display_name = "获取听记内容"
    short_description = "根据听记 ID 获取具体内容：AI 摘要(summary)、待办事项(todos)、语音转写原文(transcription) 或基础信息(info)"
    description = "获取某条 AI 听记的具体内容（摘要/待办/转写/信息）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.minutes（单一真源）
    intent_categories = ["domain.minutes"]
    # 钉钉 AI 听记专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "minutes_id": {
                "type": "string",
                "description": "听记 ID（通常从「列出会议听记」结果里的 id 取得）",
            },
            "aspect": {
                "type": "string",
                "enum": ["summary", "todos", "transcription", "info"],
                "description": (
                    "要取的内容：summary=AI 摘要(默认)，todos=提取的待办事项，"
                    "transcription=语音转写原文，info=基础信息"
                ),
            },
        },
        "required": ["minutes_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        mid = (args.get("minutes_id") or "").strip()
        if not mid:
            return {"error": "minutes_id 不能为空"}
        aspect = (args.get("aspect") or "summary").strip() or "summary"
        if aspect not in ("summary", "todos", "transcription", "info"):
            return {"error": f"aspect 必须是 summary/todos/transcription/info 之一，收到: {aspect}"}

        try:
            if aspect == "summary":
                data = self.dws.minutes_get_summary(mid)
            elif aspect == "todos":
                data = self.dws.minutes_get_todos(mid)
            elif aspect == "transcription":
                data = self.dws.minutes_get_transcription(mid)
            else:  # info
                data = self.dws.minutes_get_info(mid)
        except Exception as e:
            logger.exception("获取听记(%s)失败: %s", aspect, e)
            return {"error": f"获取听记{aspect}失败: {e}"}

        payload = {"minutes_id": mid, "aspect": aspect, "result": data}
        # 转写原文过长时截断，避免冲爆 LLM 上下文窗口
        if aspect == "transcription" and isinstance(data, str) and len(data) > MAX_TRANSCRIPT_CHARS:
            payload["result"] = data[:MAX_TRANSCRIPT_CHARS] + "\n\n...(转写原文过长已截断，如需完整内容请改用 aspect=summary 或缩小时间范围)"
            payload["truncated"] = True
            payload["total_chars"] = len(data)
        return payload
