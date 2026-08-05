"""DwsAdapter AI 听记 mixin。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging

logger = logging.getLogger(__name__)


class DwsAdapterMinutesMixin(DwsAdapterBase):
    def minutes_list(self, scope: str = "mine", query: str | None = None,
                     start: str | None = None, end: str | None = None,
                     limit: int = 10, cursor: str = "") -> list:
        """列出听记。scope: all(我有权限) / mine(我创建) / shared(他人共享)。"""
        if scope not in ("all", "mine", "shared"):
            scope = "mine"
        args = ["minutes", "list", scope, "--limit", str(limit)]
        if cursor:
            args += ["--cursor", cursor]
        if query:
            args += ["--query", query]
        if start:
            args += ["--start", start]
        if end:
            args += ["--end", end]
        try:
            data = self.run(args, operation="minutes_list", force_no_dry_run=True)
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 列出听记失败: %s", e)
            return []

    def _minutes_get(self, sub: str, minutes_id: str):
        """minutes get <sub> --id 的统一封装，返回解析结果或 None。"""
        try:
            data = self.run(
                ["minutes", "get", sub, "--id", minutes_id],
                operation=f"minutes_get_{sub}", force_no_dry_run=True,
            )
            return self._get_result(data)
        except Exception as e:
            logger.warning("[DWS] 获取听记(%s)失败: %s", sub, e)
            return None

    def minutes_get_summary(self, minutes_id: str):
        """获取听记 AI 摘要（Markdown）。"""
        return self._minutes_get("summary", minutes_id)

    def minutes_get_todos(self, minutes_id: str):
        """获取听记中提取的待办事项。"""
        return self._minutes_get("todos", minutes_id)

    def minutes_get_transcription(self, minutes_id: str):
        """获取听记语音转写原文。"""
        return self._minutes_get("transcription", minutes_id)

    def minutes_get_info(self, minutes_id: str):
        """获取听记基础信息。"""
        return self._minutes_get("info", minutes_id)
