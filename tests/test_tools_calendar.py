"""日历 & 待办工具测试。"""
from unittest.mock import MagicMock


from src.tools.calendar import (
    CreateTodoTool,
    GetCalendarEventsTool,
    ListTodosTool,
    CompleteTodoTool,
    ScheduleMeetingTool,
)


class TestGetCalendarEvents:
    def test_with_range(self):
        dws = MagicMock()
        dws.calendar_event_list.return_value = [
            {"title": "周会", "start": "2026-07-11T09:00:00+08:00",
             "end": "2026-07-11T10:00:00+08:00",
             "location": {"address": "3楼会议室"}, "status": "confirmed"},
            {"title": "评审", "start": "2026-07-11T14:00:00+08:00",
             "end": "2026-07-11T15:00:00+08:00",
             "location": {}, "status": "tentative"},
        ]
        tool = GetCalendarEventsTool(dws)
        res = tool.execute({"start": "2026-07-11T00:00:00+08:00",
                            "end": "2026-07-11T23:59:59+08:00"})
        assert res["count"] == 2
        assert res["events"][0]["title"] == "周会"
        assert res["events"][0]["location"] == "3楼会议室"
        assert res["events"][1]["location"] == ""

    def test_empty_params(self):
        dws = MagicMock()
        dws.calendar_event_list.return_value = []
        tool = GetCalendarEventsTool(dws)
        res = tool.execute({})
        assert res["count"] == 0
        assert res["events"] == []

    def test_no_location(self):
        dws = MagicMock()
        dws.calendar_event_list.return_value = [
            {"title": "晨会", "start": "", "end": "", "status": "accepted"}
        ]
        tool = GetCalendarEventsTool(dws)
        res = tool.execute({})
        assert res["count"] == 1
        assert res["events"][0]["location"] == ""


class TestCreateTodo:
    def test_success(self):
        dws = MagicMock()
        dws.todo_task_create.return_value = {"success": True}
        tool = CreateTodoTool(dws)
        res = tool.execute({"title": "写周报", "executors": "user001"})
        assert res["success"] is True
        assert res["title"] == "写周报"

    def test_missing_title(self):
        dws = MagicMock()
        tool = CreateTodoTool(dws)
        res = tool.execute({"executors": "user001"})
        assert "error" in res

    def test_missing_executors(self):
        dws = MagicMock()
        tool = CreateTodoTool(dws)
        res = tool.execute({"title": "写报告"})
        assert "error" in res

    def test_with_due_and_priority(self):
        dws = MagicMock()
        dws.todo_task_create.return_value = {"success": True}
        tool = CreateTodoTool(dws)
        res = tool.execute({"title": "紧急",
                            "executors": "u1,u2",
                            "due": "2026-07-11T18:00:00+08:00",
                            "priority": "40"})
        assert res["success"] is True
        dws.todo_task_create.assert_called_once_with(
            "紧急", "u1,u2", "2026-07-11T18:00:00+08:00", "40")

    def test_failure(self):
        dws = MagicMock()
        dws.todo_task_create.return_value = {"success": False}
        tool = CreateTodoTool(dws)
        res = tool.execute({"title": "todo", "executors": "u1"})
        assert res["success"] is False

    def test_non_dict_result(self):
        dws = MagicMock()
        dws.todo_task_create.return_value = "ok"
        tool = CreateTodoTool(dws)
        res = tool.execute({"title": "x", "executors": "u1"})
        assert res["success"] is False
class TestListTodos:
    def test_returns_normalized_todos(self):
        dws = MagicMock()
        dws.todo_list_mine.return_value = {
            "todos": [{
                "task_id": "T1", "subject": "写周报", "due_time": 1792581972967,
                "priority": 20, "status_stage": 2, "done": True,
            }],
            "count": 1, "has_more": True, "page": 1, "size": 20,
        }
        res = ListTodosTool(dws).execute({})
        assert res["count"] == 1
        assert res["items"][0]["task_id"] == "T1"
        assert res["items"][0]["done"] is True
        assert res["has_more"] is True

    def test_passes_filters(self):
        dws = MagicMock()
        dws.todo_list_mine.return_value = {
            "todos": [], "count": 0, "has_more": False, "page": 1, "size": 20,
        }
        ListTodosTool(dws).execute({
            "status": "false", "role": "creator", "priority": "40", "size": 5, "page": 2,
        })
        dws.todo_list_mine.assert_called_once_with(
            size=5, page=2, status="false", role_types="creator", priority="40")

    def test_size_clamped_to_cli_range(self):
        dws = MagicMock()
        dws.todo_list_mine.return_value = {
            "todos": [], "count": 0, "has_more": False, "page": 1, "size": 20,
        }
        # dws todo +get-my-tasks 的 --size 只接受 1..20，越界会被 CLI 拒绝
        ListTodosTool(dws).execute({"size": 999})
        assert dws.todo_list_mine.call_args.kwargs["size"] == 20
        ListTodosTool(dws).execute({"size": 0})
        assert dws.todo_list_mine.call_args.kwargs["size"] == 20
        ListTodosTool(dws).execute({"size": "abc"})
        assert dws.todo_list_mine.call_args.kwargs["size"] == 20

    def test_empty_result_gives_hint(self):
        dws = MagicMock()
        dws.todo_list_mine.return_value = {
            "todos": [], "count": 0, "has_more": False, "page": 1, "size": 20,
        }
        res = ListTodosTool(dws).execute({"status": "false"})
        assert res["count"] == 0
        assert "没有匹配的待办" in res["hint"]
        assert res["status_filter"] == "false"

    def test_error_degrades(self):
        from src.im_adapter.errors import IMAdapterError
        dws = MagicMock()
        dws.todo_list_mine.side_effect = IMAdapterError("boom")
        res = ListTodosTool(dws).execute({})
        assert "error" in res


class TestCompleteTodo:
    def test_success(self):
        dws = MagicMock()
        dws.todo_complete.return_value = {"success": True}
        res = CompleteTodoTool(dws).execute({"task_id": "T1"})
        assert res["success"] is True
        assert res["task_id"] == "T1"
        dws.todo_complete.assert_called_once_with("T1")

    def test_requires_task_id(self):
        dws = MagicMock()
        res = CompleteTodoTool(dws).execute({})
        assert "error" in res
        dws.todo_complete.assert_not_called()

    def test_write_op_requires_confirm(self):
        dws = MagicMock()
        assert CompleteTodoTool(dws).require_confirm is True

    def test_error_degrades(self):
        from src.im_adapter.errors import IMAdapterError
        dws = MagicMock()
        dws.todo_complete.side_effect = IMAdapterError("boom")
        res = CompleteTodoTool(dws).execute({"task_id": "T1"})
        assert "error" in res
class TestScheduleMeeting:
    def test_conflicts_mode(self):
        dws = MagicMock()
        dws.calendar_conflicts.return_value = {
            "date": "2026-10-23", "event_count": 1, "conflict_count": 0,
            "conflicts": [], "has_conflict": False, "complete": True,
        }
        res = ScheduleMeetingTool(dws).execute({"mode": "conflicts", "in_days": 1})
        assert res["mode"] == "conflicts"
        assert res["has_conflict"] is False
        assert "无日程冲突" in res["hint"]
        dws.calendar_conflicts.assert_called_once_with(in_days=1)

    def test_conflicts_mode_reports_actual_conflict(self):
        dws = MagicMock()
        dws.calendar_conflicts.return_value = {
            "date": "2026-10-23", "event_count": 2, "conflict_count": 1,
            "conflicts": [{"a": "周会", "b": "评审"}],
            "has_conflict": True, "complete": True,
        }
        res = ScheduleMeetingTool(dws).execute({})
        assert res["has_conflict"] is True
        assert res["conflict_count"] == 1
        assert "hint" not in res

    def test_free_slots_mode(self):
        dws = MagicMock()
        dws.calendar_free_slots.return_value = {
            "date": "2026-10-23", "window": "09:00-18:00", "slot_count": 1,
            "all_busy": False,
            "free_slots": [{"start": "09:00", "end": "14:00", "duration_min": 300}],
        }
        res = ScheduleMeetingTool(dws).execute(
            {"mode": "free_slots", "in_days": 1, "from_hour": 8, "to_hour": 20})
        assert res["mode"] == "free_slots"
        assert res["free_slots"][0]["duration_min"] == 300
        dws.calendar_free_slots.assert_called_once_with(
            in_days=1, from_hour=8, to_hour=20)

    def test_free_slots_all_busy_hint(self):
        dws = MagicMock()
        dws.calendar_free_slots.return_value = {
            "date": "2026-10-23", "window": "09:00-18:00", "slot_count": 0,
            "all_busy": True, "free_slots": [],
        }
        res = ScheduleMeetingTool(dws).execute({"mode": "free_slots"})
        assert "排满" in res["hint"]

    def test_suggest_mode(self):
        dws = MagicMock()
        dws.calendar_suggest_time.return_value = {
            "suggestions": [{"start": "2026-10-23T09:00:00+08:00",
                             "end": "2026-10-23T09:30:00+08:00"}],
            "count": 1, "complete": True,
        }
        res = ScheduleMeetingTool(dws).execute({
            "mode": "suggest", "participants": "张三, 李四",
            "start": "2026-10-23T09:00:00+08:00",
            "end": "2026-10-23T18:00:00+08:00", "duration": 30,
        })
        assert res["mode"] == "suggest"
        assert res["count"] == 1
        assert res["participants"] == ["张三", "李四"]
        dws.calendar_suggest_time.assert_called_once_with(
            participants=["张三", "李四"],
            start="2026-10-23T09:00:00+08:00",
            end="2026-10-23T18:00:00+08:00", duration=30)

    def test_suggest_requires_participants(self):
        dws = MagicMock()
        res = ScheduleMeetingTool(dws).execute(
            {"mode": "suggest", "start": "s", "end": "e"})
        assert "error" in res
        dws.calendar_suggest_time.assert_not_called()

    def test_suggest_requires_time_range(self):
        dws = MagicMock()
        res = ScheduleMeetingTool(dws).execute(
            {"mode": "suggest", "participants": "张三"})
        assert "error" in res
        dws.calendar_suggest_time.assert_not_called()

    def test_suggest_no_result_hint(self):
        dws = MagicMock()
        dws.calendar_suggest_time.return_value = {
            "suggestions": [], "count": 0, "complete": True}
        res = ScheduleMeetingTool(dws).execute({
            "mode": "suggest", "participants": "张三",
            "start": "2026-10-23T09:00:00+08:00", "end": "2026-10-23T18:00:00+08:00"})
        assert "没有找到共同空闲" in res["hint"]

    def test_unknown_mode(self):
        dws = MagicMock()
        res = ScheduleMeetingTool(dws).execute({"mode": "bogus"})
        assert "error" in res

    def test_metadata(self):
        dws = MagicMock()
        t = ScheduleMeetingTool(dws)
        assert t.platforms == ["dingtalk"]
        assert t.intent_categories == ["domain.calendar"]
        assert t.require_confirm is False
        assert t.name == "schedule_meeting"
