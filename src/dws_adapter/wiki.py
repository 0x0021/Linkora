"""DwsAdapter 知识库 mixin。拆分自 dws_adapter.py。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DwsAdapterWikiMixin:
    def wiki_space_list(self, space_type: str = "orgWikiSpace",
                        limit: int = 20, cursor: str = "") -> list:
        """列出知识库空间。type: orgWikiSpace(组织,默认) / myWikiSpace / orgSpace / mySpace。"""
        args = ["wiki", "space", "list", "--type", space_type, "--limit", str(limit)]
        if cursor:
            args += ["--cursor", cursor]
        try:
            data = self.run(args, operation="wiki_space_list", force_no_dry_run=True)
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 列出知识库空间失败: %s", e)
            return []

    def wiki_space_search(self, query: str, space_type: str | None = None,
                          limit: int = 10) -> list:
        """搜索知识库（组织知识库下 query 必填）。"""
        args = ["wiki", "space", "search", "--query", query, "--limit", str(limit)]
        if space_type:
            args += ["--type", space_type]
        try:
            data = self.run(args, operation="wiki_space_search", force_no_dry_run=True)
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 搜索知识库失败: %s", e)
            return []

    def wiki_node_list(self, workspace_id: str, folder: str | None = None,
                      limit: int = 50, cursor: str = "") -> list:
        """列出知识库下节点（文档/文件夹/表格）。workspace 必填。"""
        args = ["wiki", "node", "list", "--workspace", workspace_id, "--limit", str(limit)]
        if folder:
            args += ["--folder", folder]
        if cursor:
            args += ["--cursor", cursor]
        try:
            data = self.run(args, operation="wiki_node_list", force_no_dry_run=True)
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 列出知识库节点失败: %s", e)
            return []

    def wiki_node_search(self, workspace_id: str, query: str,
                         extensions: str | None = None, limit: int = 20,
                         cursor: str = "") -> list:
        """在知识库内搜索节点。workspace + query 必填。"""
        args = ["wiki", "node", "search", "--workspace", workspace_id,
                "--query", query, "--limit", str(limit)]
        if extensions:
            args += ["--extensions", extensions]
        if cursor:
            args += ["--cursor", cursor]
        try:
            data = self.run(args, operation="wiki_node_search", force_no_dry_run=True)
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 搜索知识库节点失败: %s", e)
            return []
