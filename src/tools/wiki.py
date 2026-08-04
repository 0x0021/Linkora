"""钉钉知识库（wiki）工具（domain.wiki）。

把 `src/dws_adapter.py` 里已实现但 0 引用的 `wiki_*` 方法接成 Agent 工具，
让数字分身在对话中能直接「列出知识库空间 / 在空间内搜索 / 列出节点 /
在节点内搜索」，支撑「对方问知识库里有什么、帮我找某份文档」这类自动回复场景。

全部为只读操作，无需二次确认（require_confirm=False）。
"""
from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool
from src.tools.utils import _coerce_limit, list_result

logger = logging.getLogger(__name__)


class WikiSpaceListTool(BaseTool):
    name = "wiki_space_list"
    display_name = "列出知识库空间"
    short_description = "列出我可访问的钉钉知识库空间（组织库/我的库等），返回空间名与 ID"
    description = "列出钉钉知识库空间列表"
    # 场景关键词统一维护在 IntentRegistry 的 domain.wiki（单一真源）
    intent_categories = ["domain.wiki"]
    # 钉钉知识库专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "space_type": {
                "type": "string",
                "enum": ["orgWikiSpace", "myWikiSpace", "orgSpace", "mySpace"],
                "description": "空间类型：orgWikiSpace=组织知识库(默认)，myWikiSpace=我的知识库，orgSpace=组织空间，mySpace=我的空间",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 20",
            },
            "cursor": {
                "type": "string",
                "description": "分页游标（上一页返回的 cursor），可选",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        space_type = (args.get("space_type") or "orgWikiSpace").strip() or "orgWikiSpace"
        if space_type not in ("orgWikiSpace", "myWikiSpace", "orgSpace", "mySpace"):
            space_type = "orgWikiSpace"
        limit = _coerce_limit(args.get("limit"), 20)
        cursor = (args.get("cursor") or "").strip()

        try:
            raw = self.dws.wiki_space_list(
                space_type=space_type, limit=limit, cursor=cursor
            )
        except Exception as e:
            logger.exception("列出知识库空间失败: %s", e)
            return {"error": f"列出知识库空间失败: {e}"}

        return list_result(raw, limit, space_type=space_type)


class WikiSpaceSearchTool(BaseTool):
    name = "wiki_space_search"
    display_name = "搜索知识库"
    short_description = "在组织/我的知识库中按关键词搜索知识库空间，返回匹配的空间"
    description = "搜索钉钉知识库空间"
    intent_categories = ["domain.wiki"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词（组织知识库下必填）",
            },
            "space_type": {
                "type": "string",
                "enum": ["orgWikiSpace", "myWikiSpace", "orgSpace", "mySpace"],
                "description": "限定空间类型，可选",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 10",
            },
        },
        "required": ["query"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        space_type = (args.get("space_type") or "").strip() or None
        if space_type and space_type not in ("orgWikiSpace", "myWikiSpace", "orgSpace", "mySpace"):
            space_type = None
        limit = _coerce_limit(args.get("limit"), 10)

        try:
            raw = self.dws.wiki_space_search(
                query=query, space_type=space_type, limit=limit
            )
        except Exception as e:
            logger.exception("搜索知识库失败: %s", e)
            return {"error": f"搜索知识库失败: {e}"}

        return list_result(raw, limit, query=query)


class WikiNodeListTool(BaseTool):
    name = "wiki_node_list"
    display_name = "列出知识库节点"
    short_description = "列出某知识库空间下的节点（文档/文件夹/表格），需提供空间 ID"
    description = "列出知识库空间下的节点"
    intent_categories = ["domain.wiki"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "知识库空间 ID（通常从「列出知识库空间」结果取得）",
            },
            "folder": {
                "type": "string",
                "description": "文件夹/目录节点 ID，限定只列出该目录下的子节点，可选",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 50",
            },
            "cursor": {
                "type": "string",
                "description": "分页游标，可选",
            },
        },
        "required": ["workspace_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        workspace_id = (args.get("workspace_id") or "").strip()
        if not workspace_id:
            return {"error": "workspace_id 不能为空"}
        folder = (args.get("folder") or "").strip() or None
        limit = _coerce_limit(args.get("limit"), 50)
        cursor = (args.get("cursor") or "").strip()

        try:
            raw = self.dws.wiki_node_list(
                workspace_id=workspace_id, folder=folder, limit=limit, cursor=cursor
            )
        except Exception as e:
            logger.exception("列出知识库节点失败: %s", e)
            return {"error": f"列出知识库节点失败: {e}"}

        return list_result(raw, limit, workspace_id=workspace_id)


class WikiNodeSearchTool(BaseTool):
    name = "wiki_node_search"
    display_name = "在知识库内搜索节点"
    short_description = "在某知识库空间内按关键词搜索文档/表格等节点，需提供空间 ID 与关键词"
    description = "在知识库空间内搜索节点"
    intent_categories = ["domain.wiki"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "知识库空间 ID（通常从「列出知识库空间」结果取得）",
            },
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "extensions": {
                "type": "string",
                "description": "限定文件扩展名过滤，如 'docx,pdf'，可选",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 20",
            },
            "cursor": {
                "type": "string",
                "description": "分页游标，可选",
            },
        },
        "required": ["workspace_id", "query"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        workspace_id = (args.get("workspace_id") or "").strip()
        if not workspace_id:
            return {"error": "workspace_id 不能为空"}
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query 不能为空"}
        extensions = (args.get("extensions") or "").strip() or None
        limit = _coerce_limit(args.get("limit"), 20)
        cursor = (args.get("cursor") or "").strip()

        try:
            raw = self.dws.wiki_node_search(
                workspace_id=workspace_id, query=query,
                extensions=extensions, limit=limit, cursor=cursor,
            )
        except Exception as e:
            logger.exception("搜索知识库节点失败: %s", e)
            return {"error": f"搜索知识库节点失败: {e}"}

        return list_result(raw, limit, workspace_id=workspace_id, query=query)
