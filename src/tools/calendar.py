from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


class GetCalendarEventsTool(BaseTool):
    name = "get_calendar_events"
    display_name = "查询日程事件"
    short_description = "查询用户今天或指定时间段的日程事件，支持按天、周、月等多维度查看"
    description = "查询日程事件（今天或指定时间段）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.calendar（单一真源）
    intent_categories = ["domain.calendar"]
    # 仅钉钉可用：飞书适配器无 calendar_event_list，企微 CLI 不支持日历查询
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "开始时间 ISO-8601，如 2026-07-03T00:00:00+08:00"
            },
            "end": {
                "type": "string",
                "description": "结束时间 ISO-8601"
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        start = args.get("start", "")
        end = args.get("end", "")

        try:
            events = self.dws.calendar_event_list(start, end)
        except Exception as e:
            logger.exception("查询日程失败: %s", e)
            return {"error": f"查询日程失败: {e}"}
        results = []
        for e in (events or []):
            if not isinstance(e, dict):
                continue
            results.append({
                "title": e.get("title", ""),
                "start_time": e.get("start", ""),
                "end_time": e.get("end", ""),
                "location": e.get("location", {}).get("address", "") if isinstance(e.get("location"), dict) else "",
                "status": e.get("status", ""),
            })
        return {"count": len(results), "events": results}


class CreateTodoTool(BaseTool):
    name = "create_todo"
    display_name = "创建待办"
    short_description = "创建一条新的待办任务，支持设置截止时间和提醒"
    description = "创建待办任务"
    # 场景关键词统一维护在 IntentRegistry 的 domain.todo（单一真源）
    intent_categories = ["domain.todo"]
    # 仅钉钉可用：飞书适配器无 todo_task_create，企微 CLI 不支持待办创建
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "待办标题"
            },
            "executors": {
                "type": "string",
                "description": "执行者 userId，逗号分隔"
            },
            "due": {
                "type": "string",
                "description": "截止时间 ISO-8601，如 2026-07-10T18:00:00+08:00"
            },
            "priority": {
                "type": "string",
                "enum": ["10", "20", "30", "40"],
                "description": "优先级：10=低/20=普通/30=高/40=紧急"
            },
        },
        "required": ["title", "executors"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        title = args.get("title", "")
        executors = args.get("executors", "")
        due = args.get("due", "")
        priority = args.get("priority", "")

        if not title or not executors:
            return {"error": "title and executors are required"}

        try:
            result = self.dws.todo_task_create(title, executors, due, priority)
        except Exception as e:
            logger.exception("创建待办失败: %s", e)
            return {"error": f"创建待办失败: {e}"}
        success = result.get("success", False) if isinstance(result, dict) else False
        return {"success": success, "title": title}
