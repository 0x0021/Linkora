"""AI 听记 / 会议纪要工具测试。"""
from unittest.mock import MagicMock

from src.tools.minutes import ListMinutesTool, GetMinutesTool, _normalize_minutes


class TestNormalizeMinutes:
    def test_full(self):
        it = {"minutesId": "m1", "title": "周会", "gmtCreate": "2026-01-01", "creator": "张三"}
        assert _normalize_minutes(it) == {
            "id": "m1", "title": "周会", "created_time": "2026-01-01", "creator": "张三", "status": ""
        }

    def test_fallback_id_field(self):
        it = {"id": "x9", "name": "月会"}
        assert _normalize_minutes(it)["id"] == "x9"
        assert _normalize_minutes(it)["title"] == "月会"

    def test_non_dict(self):
        assert _normalize_minutes("str") == {"raw": "str"}


class TestListMinutes:
    def test_success(self):
        dws = MagicMock()
        dws.minutes_list.return_value = [
            {"minutesId": "m1", "title": "周会", "gmtCreate": "2026-01-01"},
            {"minutesId": "m2", "title": "月会"},
        ]
        tool = ListMinutesTool(dws)
        res = tool.execute({"scope": "all"})
        assert res["count"] == 2
        assert res["items"][0]["id"] == "m1"
        assert res["items"][1]["title"] == "月会"
        # 校验透传参数
        _, kwargs = dws.minutes_list.call_args
        assert kwargs["scope"] == "all"

    def test_default_scope_normalized(self):
        dws = MagicMock()
        dws.minutes_list.return_value = []
        tool = ListMinutesTool(dws)
        tool.execute({"scope": "invalid"})
        _, kwargs = dws.minutes_list.call_args
        assert kwargs["scope"] == "mine"

    def test_empty(self):
        dws = MagicMock()
        dws.minutes_list.return_value = []
        tool = ListMinutesTool(dws)
        res = tool.execute({})
        assert res["count"] == 0

    def test_exception(self):
        dws = MagicMock()
        dws.minutes_list.side_effect = RuntimeError("无权限")
        tool = ListMinutesTool(dws)
        res = tool.execute({})
        assert "error" in res
        assert "无权限" in res["error"]


class TestGetMinutes:
    def test_summary(self):
        dws = MagicMock()
        dws.minutes_get_summary.return_value = "# 摘要\n- 结论A"
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "summary"})
        assert res["aspect"] == "summary"
        assert "结论A" in res["result"]

    def test_todos(self):
        dws = MagicMock()
        dws.minutes_get_todos.return_value = [{"content": "跟进X"}]
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "todos"})
        assert res["result"][0]["content"] == "跟进X"

    def test_transcription(self):
        dws = MagicMock()
        dws.minutes_get_transcription.return_value = " raw text "
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "transcription"})
        assert res["result"] == " raw text "

    def test_info(self):
        dws = MagicMock()
        dws.minutes_get_info.return_value = {"title": "周会"}
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "info"})
        assert res["result"]["title"] == "周会"

    def test_default_aspect_summary(self):
        dws = MagicMock()
        dws.minutes_get_summary.return_value = "s"
        tool = GetMinutesTool(dws)
        tool.execute({"minutes_id": "m1"})
        dws.minutes_get_summary.assert_called_once_with("m1")

    def test_missing_id(self):
        dws = MagicMock()
        tool = GetMinutesTool(dws)
        res = tool.execute({})
        assert "error" in res

    def test_bad_aspect(self):
        dws = MagicMock()
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1", "aspect": "bogus"})
        assert "error" in res

    def test_exception(self):
        dws = MagicMock()
        dws.minutes_get_summary.side_effect = RuntimeError("挂了")
        tool = GetMinutesTool(dws)
        res = tool.execute({"minutes_id": "m1"})
        assert "error" in res
        assert "挂了" in res["error"]
