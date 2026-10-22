from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError
from src.tools.base import BaseTool
from src.tools.utils import _coerce_int_in_range

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


class ListTodosTool(BaseTool):
    name = "list_todos"
    display_name = "列出待办事项"
    short_description = (
        "查询与当前用户相关的待办清单（可按完成状态、角色、优先级、截止时间过滤），"
        "用于「我有哪些待办 / 哪些逾期待办 / 今天到期的待办」这类场景"
    )
    description = "列出待办事项"
    # 场景关键词统一维护在 IntentRegistry 的 domain.todo（单一真源）
    intent_categories = ["domain.todo"]
    # 仅钉钉可用：飞书/企微适配器无 todo_list_mine
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["true", "false"],
                "description": "完成状态：true=已完成，false=未完成（可选，默认全部）",
            },
            "role": {
                "type": "string",
                "enum": ["executor", "creator", "participant"],
                "description": "角色：executor=执行人（默认）/ creator=创建人 / participant=参与者",
            },
            "priority": {
                "type": "string",
                "enum": ["10", "20", "30", "40"],
                "description": "优先级过滤：10=低/20=普通/30=高/40=紧急",
            },
            "size": {
                "type": "integer",
                "description": "每页条数，1..20，默认 20",
            },
            "page": {
                "type": "integer",
                "description": "页码，默认 1",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        status = (args.get("status") or "").strip()
        role = (args.get("role") or "").strip()
        priority = (args.get("priority") or "").strip()
        size = _coerce_int_in_range(args.get("size"), 20, 1, 20)
        page = _coerce_int_in_range(args.get("page"), 1, 1, 999)

        try:
            payload = self.dws.todo_list_mine(
                size=size, page=page, status=status,
                role_types=role, priority=priority,
            )
        except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
            logger.exception("查询待办失败: %s", e)
            return {"error": f"查询待办失败: {e}"}

        todos = payload.get("todos") or []
        result = {
            "count": len(todos),
            "items": todos,
            "has_more": bool(payload.get("has_more")),
            "page": payload.get("page") or page,
        }
        if status:
            result["status_filter"] = status
        if not todos:
            result["hint"] = (
                "没有匹配的待办"
                + ("（未完成）" if status == "false" else "")
            )
        return result


class CompleteTodoTool(BaseTool):
    name = "complete_todo"
    display_name = "完成待办事项"
    short_description = "把一条待办标记为已完成（需提供 taskId）"
    description = "完成待办事项"
    # 场景关键词统一维护在 IntentRegistry 的 domain.todo（单一真源）
    intent_categories = ["domain.todo"]
    # 仅钉钉可用：飞书/企微适配器无 todo_complete
    platforms = ["dingtalk"]
    # 写操作：完成待办会改变他人可见的任务状态，需二次确认
    require_confirm = True
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "待办 taskId（必填，可从 list_todos 结果取得）",
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return {"error": "task_id 不能为空"}

        try:
            result = self.dws.todo_complete(task_id)
        except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
            logger.exception("完成待办失败: %s", e)
            return {"error": f"完成待办失败: {e}"}

        success = bool(result.get("success", False)) if isinstance(result, dict) else False
        return {"success": success, "task_id": task_id}


class ScheduleMeetingTool(BaseTool):
    name = "schedule_meeting"
    display_name = "会议协调"
    short_description = (
        "查日程冲突、找空闲时段、为多人推荐共同开会时间（按姓名自动解析参会人），"
        "用于「我明天有冲突吗 / 我还有什么时候有空 / 找大家都有空的时间」"
    )
    description = "会议协调（冲突检测 / 空闲时段 / 建议时间）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.calendar（单一真源）
    intent_categories = ["domain.calendar"]
    # 仅钉钉可用：飞书/企微适配器无 calendar_conflicts 等方法
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["conflicts", "free_slots", "suggest"],
                "description": (
                    "conflicts=检测当天日程冲突（默认）；"
                    "free_slots=查空闲时段；"
                    "suggest=为多人推荐共同开会时间"
                ),
            },
            "in_days": {
                "type": "integer",
                "description": "几天后（0=今天，默认；1=明天），conflicts/free_slots 模式用",
            },
            "participants": {
                "type": "string",
                "description": "suggest 模式必填：参会人姓名，逗号分隔（会逐个解析为唯一 userId，匹配不到会明确报错）",
            },
            "start": {
                "type": "string",
                "description": "suggest 模式必填：时间范围开始 ISO-8601，如 2026-10-23T09:00:00+08:00",
            },
            "end": {
                "type": "string",
                "description": "suggest 模式必填：时间范围结束 ISO-8601",
            },
            "duration": {
                "type": "integer",
                "description": "suggest 模式：会议时长（分钟），可选",
            },
            "from_hour": {
                "type": "integer",
                "description": "free_slots 模式：工作时段起始小时，默认 9",
            },
            "to_hour": {
                "type": "integer",
                "description": "free_slots 模式：工作时段结束小时，默认 18",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        mode = (args.get("mode") or "conflicts").strip() or "conflicts"
        in_days = _coerce_int_in_range(args.get("in_days"), 0, 0, 365)

        if mode == "conflicts":
            try:
                res = self.dws.calendar_conflicts(in_days=in_days)
            except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
                logger.exception("检测日程冲突失败: %s", e)
                return {"error": f"检测日程冲突失败: {e}"}
            res["mode"] = "conflicts"
            if not res.get("has_conflict"):
                res["hint"] = (
                    f"{res.get('date') or '当天'}无日程冲突（共 {res.get('event_count') or 0} 个日程）"
                )
            return res

        if mode == "free_slots":
            from_hour = _coerce_int_in_range(args.get("from_hour"), 9, 0, 23)
            to_hour = _coerce_int_in_range(args.get("to_hour"), 18, 1, 24)
            try:
                res = self.dws.calendar_free_slots(
                    in_days=in_days, from_hour=from_hour, to_hour=to_hour)
            except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
                logger.exception("查询空闲时段失败: %s", e)
                return {"error": f"查询空闲时段失败: {e}"}
            res["mode"] = "free_slots"
            if res.get("all_busy"):
                res["hint"] = f"{res.get('date') or '当天'}工作时段已排满"
            elif not res.get("free_slots"):
                res["hint"] = f"{res.get('date') or '当天'}没有空闲时段"
            return res

        if mode == "suggest":
            raw_participants = (args.get("participants") or "").strip()
            if not raw_participants:
                return {"error": "suggest 模式需要 participants（参会人姓名，逗号分隔）"}
            participants = [p.strip() for p in raw_participants.split(",") if p.strip()]
            start = (args.get("start") or "").strip()
            end = (args.get("end") or "").strip()
            if not start or not end:
                return {"error": "suggest 模式需要 start 与 end 时间范围（ISO-8601）"}
            duration = _coerce_int_in_range(args.get("duration"), 0, 0, 1440)
            try:
                res = self.dws.calendar_suggest_time(
                    participants=participants, start=start, end=end,
                    duration=duration or 0,
                )
            except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
                # CLI 对姓名解析失败会明确报错，这里原样透传给调用方
                logger.warning("推荐会议时间失败: %s", e)
                return {"error": f"推荐会议时间失败: {self.dws._readable_dws_error(e)}"}
            res["mode"] = "suggest"
            res["participants"] = participants
            if not res.get("suggestions"):
                res["hint"] = "该时间范围内没有找到共同空闲时段"
            return res

        return {"error": f"未知 mode: {mode}（可用：conflicts / free_slots / suggest）"}
