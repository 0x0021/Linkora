"""企业内容语义检索工具（domain.aisearch）。

把 `src/dws_adapter/aisearch.py` 的 ``aisearch_*`` 方法接成 Agent 工具，让数字分身能
跨类型语义检索企业内部内容（文档/消息/日程/待办/听记/日志/邮件等），并在被追问时
回答「我发给谁了什么 / 谁发给我的」这类行为问题。

⚠️ 与既有工具的分工（勿混用）：
- 要**精确**的消息检索（按发送人/会话/时间结构化筛选、可枚举完整消息集合）→ 用
  ``search_messages``（底层 chat +search-msg）。
- 要**具体**文档正文 / 听记摘要 / 审批详情 → 用 ``search_doc`` / ``get_doc_content`` /
  ``list_minutes`` / ``get_minutes`` 等专用工具。
- 本工具的价值在于**跨类型 + 语义**：用户问「618大促复盘相关的文档和消息有哪些」这类
  问题时，一次检索覆盖多个来源。

⚠️ 返回的是**摘要片段**（``original_content_read`` 通常为 false），不是原文全文。
该标记会随结果返回给 LLM，避免它据此断言原文细节。
"""
from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)

# 单条 snippet 长度上限（dws 返回的片段本身已截断，这里再兜一层防上下文膨胀）
MAX_SNIPPET_CHARS = 1200
# 返回条数上限
MAX_ITEMS = 20

# dws v1.0.63 合法内容类型
CONTENT_TYPES = [
    "all", "document", "im", "calendar", "todo", "minute", "report",
    "image", "link", "notable", "baike", "mail",
]
BEHAVIOR_TYPES = ["all", "send", "create", "share", "edit", "receive"]


def _normalize_evidence(item: dict) -> dict:
    """把完整性证据规整成「人话」标记，便于 LLM 判断这条结果有多可信。"""
    evidence = item.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {}
    query_match = evidence.get("query_match") or ""
    snippet_only = not evidence.get("original_content_read", False)
    # queryMatch: no_text_evidence / partial_text_evidence = 语义候选（非确认的主题匹配）
    is_candidate = query_match in ("no_text_evidence", "partial_text_evidence")
    return {
        "is_snippet_only": snippet_only,
        "query_match": query_match,
        "is_semantic_candidate": is_candidate,
        "within_requested_range": evidence.get("within_requested_range"),
    }


class SearchEnterpriseContentTool(BaseTool):
    name = "search_enterprise_content"
    display_name = "跨类型检索企业内容"
    short_description = "语义检索企业内部内容（文档/消息/日程/待办/听记/日志/邮件等），支持按类型与时间范围过滤，一次覆盖多个来源"
    description = "语义检索企业内部内容"
    intent_categories = ["domain.aisearch"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "string",
                "description": "内容关键词，多个用逗号分隔。只填内容词，时间词放 time_range、类型词放 types",
            },
            "types": {
                "type": "string",
                "description": "内容类型，多个用逗号分隔：" + "/".join(CONTENT_TYPES),
            },
            "time_range": {
                "type": "string",
                "description": "时间范围，如 今天/本周/9月/过去一周。用户明确给出时间词时才填",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        queries = (args.get("queries") or "").strip()
        types = (args.get("types") or "all").strip() or "all"
        time_range = (args.get("time_range") or "").strip()
        try:
            raw = self.dws.aisearch_enterprise(
                queries=queries, types=types, time_range=time_range)
        except (IMAdapterError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("企业内容语义检索失败: %s", exc)
            return {"error": f"企业内容语义检索失败: {exc}"}

        items: list[dict] = []
        for it in (raw if isinstance(raw, list) else []):
            if not isinstance(it, dict):
                continue
            snippet = (it.get("snippet") or "")[:MAX_SNIPPET_CHARS]
            item = {
                "title": it.get("title") or "",
                "snippet": snippet,
                "source_type": it.get("source_type") or "",
                "source_name": it.get("source_name") or "",
                "date": it.get("date") or "",
                "has_url": bool(it.get("url")),
                "evidence": _normalize_evidence(it),
            }
            items.append(item)

        hint = ""
        if not items:
            hint = ("没有检索到内容。可换用更贴近原文的关键词、放开 types=all，"
                    "或确认该时间窗内确有相关内容。")
        elif all(i["evidence"]["is_snippet_only"] for i in items):
            hint = ("返回的是**摘要片段**（未读原文）。需要原文时用对应产品的专用工具"
                    "（如 search_doc / get_minutes）再取一次。")
        return {"queries": queries, "types": types, "time_range": time_range,
                "count": len(items), "items": items[:MAX_ITEMS], "hint": hint}


class SearchEnterpriseBehaviorTool(BaseTool):
    name = "search_enterprise_behavior"
    display_name = "追溯企业行为记录"
    short_description = "按行为类型追溯（我发给谁/谁发给我/我创建过什么），可指定内容类型与时间范围"
    description = "追溯企业行为记录"
    intent_categories = ["domain.aisearch"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "queries": {
                "type": "string",
                "description": "内容关键词，多个用逗号分隔。只填内容词",
            },
            "types": {
                "type": "string",
                "description": "内容类型，多个用逗号分隔：" + "/".join(CONTENT_TYPES),
            },
            "time_range": {
                "type": "string",
                "description": "时间范围，如 今天/本周/过去一周",
            },
            "behavior_type": {
                "type": "string",
                "description": "行为类型：" + "/".join(BEHAVIOR_TYPES),
            },
            "direction": {
                "type": "string",
                "description": "交互方向，如 我->某人、某人->我",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        queries = (args.get("queries") or "").strip()
        types = (args.get("types") or "all").strip() or "all"
        time_range = (args.get("time_range") or "").strip()
        behavior_type = (args.get("behavior_type") or "all").strip() or "all"
        direction = (args.get("direction") or "").strip()
        try:
            raw = self.dws.aisearch_behavior(
                queries=queries, types=types, time_range=time_range,
                behavior_type=behavior_type, direction=direction,
            )
        except (IMAdapterError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("企业行为检索失败: %s", exc)
            return {"error": f"企业行为检索失败: {exc}"}

        items: list[dict] = []
        for it in (raw if isinstance(raw, list) else []):
            if not isinstance(it, dict):
                continue
            items.append({
                "title": it.get("title") or "",
                "snippet": (it.get("snippet") or "")[:MAX_SNIPPET_CHARS],
                "source_type": it.get("source_type") or "",
                "date": it.get("date") or "",
                "has_url": bool(it.get("url")),
                "evidence": _normalize_evidence(it),
            })
        hint = ""
        if not items:
            hint = ("没有匹配的行为记录。行为检索依赖后端索引，可能存在延迟；"
                    "若要完整消息集合，请改用 search_messages。")
        return {"queries": queries, "behavior_type": behavior_type,
                "direction": direction, "count": len(items),
                "items": items[:MAX_ITEMS], "hint": hint}
