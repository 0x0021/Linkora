from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class SearchDocTool(BaseTool):
    name = "search_doc"
    display_name = "搜索钉钉文档"
    short_description = "按关键词搜索钉钉文档库，返回匹配文档的名称、类型与访问链接 URL"
    description = "搜索钉钉文档，返回匹配的文档列表（名称、nodeId、类型、URL）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.doc（单一真源）
    intent_categories = ["domain.doc"]
    # 钉钉文档专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词"
            },
            "page_size": {
                "type": "integer",
                "description": "每页数量，默认 10",
                "default": 10
            },
        },
        "required": ["query"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        query = (args.get("query") or "").strip()
        page_size = args.get("page_size", 10)
        if not query:
            return {"error": "query is required"}

        try:
            docs = self.dws.doc_search(query, page_size=page_size)
        except Exception as e:
            logger.exception("搜索文档失败: %s", e)
            return {"error": f"搜索文档失败: {e}"}
        results = []
        for doc in (docs or []):
            if not isinstance(doc, dict):
                continue
            results.append({
                "name": doc.get("name", ""),
                "node_id": doc.get("nodeId", ""),
                "type": doc.get("nodeType", ""),
                "extension": doc.get("extension", ""),
                "url": doc.get("docUrl", ""),
                "creator_uid": doc.get("creatorUid", ""),
            })
        return {"count": len(results), "documents": results}


class GetDocContentTool(BaseTool):
    name = "get_doc_content"
    display_name = "读取文档内容"
    short_description = "通过 nodeId 读取钉钉文档的完整正文，以 Markdown 格式返回"
    description = "读取钉钉文档的内容（通过 nodeId），返回 Markdown 格式内容"
    # 场景关键词统一维护在 IntentRegistry 的 domain.doc（单一真源）
    intent_categories = ["domain.doc"]
    # 钉钉文档专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "文档的 nodeId（可从 search_doc 获取）"
            },
        },
        "required": ["node_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        node_id = args.get("node_id", "")
        if not node_id:
            return {"error": "node_id is required"}

        data = self.dws.doc_read(node_id, content_format="markdown")
        content = ""
        if isinstance(data, dict):
            result = data.get("result", data)
            if isinstance(result, dict):
                content = result.get("content", "") or result.get("markdown", "")
            elif isinstance(result, str):
                content = result
        if not content and isinstance(data, dict):
            content = str(data.get("result", ""))

        max_len = 8000
        if len(content) > max_len:
            content = content[:max_len] + "\n\n... (内容已截断)"

        return {"node_id": node_id, "content": content, "length": len(content)}
