"""DwsAdapter 文档/联系人/日历/待办 mixin。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging

from src.dws_adapter.core import DwsError

logger = logging.getLogger(__name__)


class DwsAdapterDocMixin(DwsAdapterBase):
    @staticmethod
    def _normalize_doc_search(data: object) -> list[dict]:
        """把新旧两种文档搜索返回统一成旧契约结构。

        旧命令 ``doc search``：顶层 ``documents[]``，字段 ``nodeType`` / ``docUrl``。
        新命令 ``drive +search-docs``：``data.docs[]``，字段 ``type`` / ``url``。
        对外统一为 ``documents[]`` + 旧字段名，调用方（src/tools/doc.py）无需感知。
        """
        if not isinstance(data, dict):
            return []
        raw = data.get("documents")
        if raw is None:
            inner = data.get("data")
            if isinstance(inner, dict):
                raw = inner.get("docs")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for d in raw:
            if not isinstance(d, dict):
                continue
            item = dict(d)
            # 新命令字段 → 旧契约字段（旧命令已有同名字段时不覆盖）
            if not item.get("nodeType"):
                item["nodeType"] = d.get("type", "")
            if not item.get("docUrl"):
                item["docUrl"] = d.get("url", "")
            out.append(item)
        return out

    def doc_search(self, query: str, page_size: int = 10) -> list[dict]:
        """按关键词搜索钉钉文档，返回 ``documents[]``（名称/nodeId/类型/URL）。

        ⚠️ ``dws doc search`` 在 v1.0.62-beta.8 已 **deprecated**（实测输出 WARN：
        "'dws doc search' is deprecated, use 'dws drive search' or
        'dws wiki node search --workspace <id>' instead"）——文档类文件管理能力整体
        迁往 ``dws drive``，旧命令将来可能被移除。故优先走新命令 ``drive +search-docs``，
        仅在旧版 dws 不认识该子命令时回退旧命令，保证「升级」与「回退」两个方向都能用。
        """
        try:
            data = self.run([
                "drive", "+search-docs",
                "--query", query,
                "--limit", str(page_size),
            ], operation="doc_search", force_no_dry_run=True)
        except DwsError as e:
            msg = str(e).lower()
            # 仅当确属「命令不存在」才回退；权限/网络等真实失败必须原样抛出，
            # 否则会把故障伪装成「搜不到文档」。
            if not any(k in msg for k in (
                    "unknown command", "unknown flag", "unknown shorthand",
                    "no such", "not found")):
                raise
            logger.info("dws 不支持 drive +search-docs，回退已废弃的 doc search: %s", e)
            data = self.run([
                "doc", "search",
                "--query", query,
                "--page-size", str(page_size),
            ], operation="doc_search", force_no_dry_run=True)
        return self._normalize_doc_search(data)

    def doc_read(self, node_id: str, content_format: str = "markdown") -> dict:
        data = self.run([
            "doc", "read",
            "--node", node_id,
            "--content-format", content_format,
        ], force_no_dry_run=True)
        return data if isinstance(data, dict) else {}

    def contact_user_search(self, keyword: str) -> list[dict]:
        try:
            data = self.run([
                "contact", "user", "search",
                "--query", keyword,
            ], force_no_dry_run=True)
        except DwsError as e:
            if self._is_personal_dingtalk_error(str(e)):
                logger.debug("个人钉钉模式：contact_user_search 不可用，跳过")
                return []
            raise
        result = self._get_result(data)
        if isinstance(result, list):
            return result
        return []

    def calendar_event_list(self, start: str = "", end: str = "") -> list[dict]:
        args = ["calendar", "event", "list"]
        if start:
            args.extend(["--start", start])
        if end:
            args.extend(["--end", end])
        data = self.run(args, force_no_dry_run=True)
        result = self._get_result(data)
        if isinstance(result, dict):
            return result.get("events", [])
        return []

    def todo_task_create(self, title: str, executors: str,
                         due: str = "", priority: str = "") -> dict:
        args = ["todo", "task", "create", "--title", title, "--executors", executors]
        if due:
            args.extend(["--due", due])
        if priority:
            args.extend(["--priority", priority])
        return self.run(args)
