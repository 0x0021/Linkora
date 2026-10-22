"""DWS Adapter · calendar 会议协调 + todo 域方法单元测试。

覆盖本轮新增的适配器方法（均为 dws v1.0.63 实测契约）：
- calendar_conflicts / calendar_free_slots / calendar_suggest_time
- todo_list_mine / todo_complete

关键实测约束：
- calendar 的 +xxx shortcut 返回体**无 result 包装**，形如
  ``{ok, outcome, data:{...}}``；原子命令才是 ``{result:{...}}``。
- todo ``+get-my-tasks`` 的 ``--size`` 只接受 1..20（不是 --limit），
  ``--status`` 只接受 true/false，``--role-types`` 只接受
  creator/executor/participant，``--priority`` 只接受 10/20/30/40。
- todo ``finalStatusStage`` **2 = 已完成**（交叉验证：全量 20 条均为 2，
  同时 --status false 返 0 条、--status true 返同批 stage=2 的项）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.dws_adapter import DwsAdapter


def _make_adapter(run_return):
    adapter = DwsAdapter(cli_path="dws", timeout=5)
    adapter.run = MagicMock(return_value=run_return)
    return adapter


class TestCalendarCoordination:
    def test_conflicts_command_shape(self):
        adapter = _make_adapter({
            "ok": True, "outcome": "success",
            "data": {"date": "2026-10-23", "eventCount": 2, "conflictCount": 1,
                     "conflicts": [{"a": 1}], "hasConflict": True, "complete": True},
        })
        res = adapter.calendar_conflicts(in_days=1)
        args = adapter.run.call_args[0][0]
        assert args[:2] == ["calendar", "+conflicts"]
        assert "--in-days" in args and "1" in args
        assert res["date"] == "2026-10-23"
        assert res["event_count"] == 2
        assert res["conflict_count"] == 1
        assert res["has_conflict"] is True
        assert res["conflicts"] == [{"a": 1}]

    def test_conflicts_default_is_today(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.calendar_conflicts()
        args = adapter.run.call_args[0][0]
        assert args[args.index("--in-days") + 1] == "0"

    def test_conflicts_negative_in_days_clamped(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.calendar_conflicts(in_days=-5)
        args = adapter.run.call_args[0][0]
        assert args[args.index("--in-days") + 1] == "0"

    def test_free_slots_command_shape(self):
        adapter = _make_adapter({
            "ok": True, "outcome": "success",
            "data": {"date": "2026-10-23", "window": "09:00-18:00", "slotCount": 2,
                     "allBusy": False,
                     "freeSlots": [{"start": "09:00", "end": "14:00", "durationMin": 300}]},
        })
        res = adapter.calendar_free_slots(in_days=1, from_hour=9, to_hour=18)
        args = adapter.run.call_args[0][0]
        assert args[:2] == ["calendar", "+free-slots"]
        assert "--in-days" in args and "--from" in args and "--to" in args
        assert res["slot_count"] == 2
        assert res["free_slots"] == [
            {"start": "09:00", "end": "14:00", "duration_min": 300}]

    def test_free_slots_hours_clamped(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.calendar_free_slots(in_days=0, from_hour=99, to_hour=-3)
        args = adapter.run.call_args[0][0]
        assert args[args.index("--from") + 1] == "23"
        assert args[args.index("--to") + 1] == "1"

    def test_suggest_time_command_shape(self):
        adapter = _make_adapter({
            "ok": True, "outcome": "success",
            "data": {"suggestions": [{"start": "s1", "end": "e1"}], "count": 1, "complete": True},
        })
        res = adapter.calendar_suggest_time(
            participants=["张三", "李四"], start="2026-10-23T09:00:00+08:00",
            end="2026-10-23T18:00:00+08:00", duration=30)
        args = adapter.run.call_args[0][0]
        assert args[:2] == ["calendar", "+suggest-time"]
        assert args[args.index("--with") + 1] == "张三,李四"
        assert "--duration" in args and "30" in args
        assert res["count"] == 1
        assert res["suggestions"] == [{"start": "s1", "end": "e1"}]

    def test_suggest_time_requires_participants(self):
        adapter = _make_adapter({})
        res = adapter.calendar_suggest_time(
            participants=[], start="s", end="e")
        assert "error" in res
        adapter.run.assert_not_called()

    def test_suggest_time_requires_time_range(self):
        adapter = _make_adapter({})
        res = adapter.calendar_suggest_time(participants=["张三"], start="", end="e")
        assert "error" in res
        adapter.run.assert_not_called()

    def test_suggest_time_omits_duration_when_zero(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.calendar_suggest_time(participants=["张三"], start="s", end="e")
        args = adapter.run.call_args[0][0]
        assert "--duration" not in args

    def test_atomic_result_wrapper_also_handled(self):
        # 原子命令形态 {result:{...}} 也要能解（与 shortcut 的 {data:{...}} 兼容）
        adapter = _make_adapter({
            "result": {"date": "2026-10-23", "eventCount": 3, "conflictCount": 0,
                       "hasConflict": False},
        })
        res = adapter.calendar_conflicts()
        assert res["event_count"] == 3
        assert res["has_conflict"] is False


class TestTodoDomain:
    def test_list_mine_command_shape(self):
        adapter = _make_adapter({
            "ok": True, "outcome": "success",
            "data": {"count": 2, "hasMore": True, "page": 1, "size": 20,
                     "todos": [
                         {"taskId": "T1", "subject": "写周报", "dueTime": 1792,
                          "priority": 20, "finalStatusStage": 2},
                         {"taskId": "T2", "subject": "交接", "dueTime": 1793,
                          "priority": 30, "finalStatusStage": 1},
                     ]},
        })
        res = adapter.todo_list_mine(size=20, page=1)
        args = adapter.run.call_args[0][0]
        assert args[:2] == ["todo", "+get-my-tasks"]
        # 必须是 --size（不是 --limit），实测 --limit 会被 CLI 拒绝
        assert "--size" in args and "--limit" not in args
        assert args[args.index("--size") + 1] == "20"
        assert res["count"] == 2
        assert res["has_more"] is True
        assert res["todos"][0]["task_id"] == "T1"
        assert res["todos"][0]["done"] is True
        assert res["todos"][1]["done"] is False

    def test_list_mine_clamps_size(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.todo_list_mine(size=999, page=1)
        args = adapter.run.call_args[0][0]
        assert args[args.index("--size") + 1] == "20"

    def test_list_mine_clamps_page(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.todo_list_mine(size=10, page=0)
        args = adapter.run.call_args[0][0]
        assert args[args.index("--page") + 1] == "1"

    def test_list_mine_passes_valid_filters_only(self):
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.todo_list_mine(status="false", role_types="creator", priority="40")
        args = adapter.run.call_args[0][0]
        assert args[args.index("--status") + 1] == "false"
        assert args[args.index("--role-types") + 1] == "creator"
        assert args[args.index("--priority") + 1] == "40"

    def test_list_mine_drops_invalid_status(self):
        # --status 只接受 true/false，其他值会被 CLI 拒绝
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.todo_list_mine(status="yes")
        args = adapter.run.call_args[0][0]
        assert "--status" not in args

    def test_list_mine_no_all_flag(self):
        # --all 会触发服务端 40 页上限（pagination_limit_reached），不传
        adapter = _make_adapter({"ok": True, "data": {}})
        adapter.todo_list_mine()
        args = adapter.run.call_args[0][0]
        assert "--all" not in args

    def test_complete_command_shape(self):
        adapter = _make_adapter({"ok": True, "outcome": "success", "data": {}})
        adapter.todo_complete("T1")
        args = adapter.run.call_args[0][0]
        assert args[:2] == ["todo", "+complete"]
        assert args[args.index("--task-id") + 1] == "T1"

    def test_complete_requires_task_id(self):
        adapter = _make_adapter({})
        res = adapter.todo_complete("")
        assert res["success"] is False
        adapter.run.assert_not_called()
