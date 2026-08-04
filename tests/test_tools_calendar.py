"""日历 & 待办工具测试。"""
from unittest.mock import MagicMock


from src.tools.calendar import CreateTodoTool, GetCalendarEventsTool


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
