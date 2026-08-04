from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class SearchContactTool(BaseTool):
    name = "search_contact"
    display_name = "搜索联系人"
    platforms = ["dingtalk"]
    short_description = "在通讯录中按姓名、手机号或邮箱搜索联系人，返回职位和 userId"
    description = "搜索通讯录中的联系人，返回姓名、职位、userId 等"
    # 场景关键词统一维护在 IntentRegistry 的 domain.contact（单一真源）
    intent_categories = ["domain.contact"]
    parameters = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "搜索关键词（姓名或拼音）"
            },
        },
        "required": ["keyword"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        keyword = (args.get("keyword") or "").strip()
        if not keyword:
            return {"error": "keyword is required"}

        try:
            users = self.dws.contact_user_search(keyword)
        except Exception as e:
            logger.exception("搜索联系人失败: %s", e)
            return {"error": f"搜索联系人失败: {e}"}
        results = []
        for u in (users or []):
            if not isinstance(u, dict):
                continue
            results.append({
                "name": u.get("name", ""),
                "title": u.get("title", ""),
                "user_id": u.get("userId", ""),
                "nick": u.get("nick", ""),
            })
        return {"count": len(results), "contacts": results}
