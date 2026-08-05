"""DwsAdapter 文档/联系人/日历/待办 mixin。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging

from src.dws_adapter.core import DwsError

logger = logging.getLogger(__name__)


class DwsAdapterDocMixin(DwsAdapterBase):
    def doc_search(self, query: str, page_size: int = 10) -> list[dict]:
        data = self.run([
            "doc", "search",
            "--query", query,
            "--page-size", str(page_size),
        ], force_no_dry_run=True)
        if isinstance(data, dict):
            return data.get("documents", [])
        return []

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
