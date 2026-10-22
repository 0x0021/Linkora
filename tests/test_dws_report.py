"""钉钉日志（日报/周报）适配器与工具层测试。

关键回归点（均来自 dws v1.0.63 实测）：
- 两种返回体形态：shortcut 是 ``{ok,outcome,data}``（**无** result 包装），
  原子 ``report entry get`` 是 ``{result:...}``（有 result 包装）
- 时间窗硬约束：inbox ≤ 180 天，outbox ≤ 20 天；且「正好等于上限」也会被拒
- 返回键是 camelCase（reportId/templateName），工具层才归一成 snake_case
- ``report_content[].value`` 是 JSON 字符串（多选带引号包裹），须按原文保留
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.dws_adapter import DwsAdapter
from src.tools.report import (
    GetReportDetailTool,
    ListReportsTool,
    ListReportTemplatesTool,
)


@pytest.fixture
def adapter():
    return DwsAdapter(cli_path="dws", dry_run=False)


def _ok(data: dict) -> MagicMock:
    """shortcut 形态：ok/outcome/data 三层，无 result 包装。"""
    return MagicMock(returncode=0,
                     stdout='{"ok":true,"outcome":"success","data":%s,"meta":{}}'
                     % _json(data),
                     stderr="")


def _json(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


# ============ 时间窗归一 ============

class TestReportWindow:
    def test_default_window_within_20_days(self, adapter):
        start, end, err = adapter._report_window()
        assert err == ""
        from datetime import datetime
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        assert 0 < delta.days < 20, f"默认窗口须落在 20 天内（dws 对等于上限也拒绝），实得 {delta.days}"

    def test_inbox_default_window_uses_180_days(self, adapter):
        start, end, err = adapter._report_window(max_days=180)
        assert err == ""
        from datetime import datetime
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        assert 100 < delta.days <= 180

    def test_yyyy_mmdd_end_expands_to_end_of_day(self, adapter):
        """YYYY-MM-DD 形式的 end 必须补到23:59:59，否则当天数据会被漏掉。"""
        start, end, err = adapter._report_window("2026-10-01", "2026-10-02")
        assert err == ""
        assert end.endswith("23:59:59+08:00") or "T23:59:59" in end

    def test_oversized_window_narrowed_to_limit(self, adapter):
        start, end, err = adapter._report_window("2026-01-01", "2026-10-22")
        assert err == ""
        from datetime import datetime
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        assert delta.days <= 19, "超限窗口必须被自动收窄（dws 报 time_range_too_large）"

    def test_invalid_format_returns_error(self, adapter):
        _, _, err = adapter._report_window("bad-date", "2026-10-22")
        assert "时间格式非法" in err

    def test_end_before_start_returns_error(self, adapter):
        _, _, err = adapter._report_window("2026-10-22", "2026-10-01")
        assert "结束时间须晚于开始时间" in err


# ============ 返回体归一 ============

class TestReportDataUnwrap:
    def test_shortcut_shape_uses_data_key(self, adapter):
        """shortcut 返回 ok/outcome/data，_report_data 必须取 data 层。"""
        out = adapter._report_data({"ok": True, "outcome": "success",
                                    "data": {"reports": [{"reportId": "r1"}]}})
        assert out["reports"][0]["reportId"] == "r1"

    def test_atomic_shape_uses_result_key(self, adapter):
        """原子 report entry get 返回 result 包装。"""
        out = adapter._report_data({"result": {"report_name": "x"}, "success": True})
        assert out["report_name"] == "x"

    @pytest.mark.parametrize("bad", [None, "str", 42, []])
    def test_non_dict_returns_empty(self, adapter, bad):
        assert adapter._report_data(bad) == {}

    def test_dict_without_known_keys_returns_empty(self, adapter):
        assert adapter._report_data({"ok": True}) == {}


# ============ 命令形态 ============

class TestReportCommands:
    def test_inbox_command_shape(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _ok({"reports": [{"reportId": "r1"}]})
            out = adapter.report_inbox_list(start="2026-10-01", end="2026-10-22", size=5)
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["dws", "report", "+inbox-list"]
        assert "--size" in cmd and "5" in cmd
        assert out[0]["reportId"] == "r1"

    def test_outbox_command_shape(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _ok({"reports": []})
            adapter.report_outbox_list(start="2026-10-01", end="2026-10-22")
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["dws", "report", "+outbox-list"]

    def test_size_clamped_to_20(self, adapter):
        """dws 的 --size 合法区间是 1..20，越界须夹紧而非透传。"""
        for requested, expected in ((100, "20"), (0, "1")):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = _ok({"reports": []})
                adapter.report_inbox_list(size=requested)
            cmd = mock_run.call_args[0][0]
            assert cmd[cmd.index("--size") + 1] == expected

    def test_cursor_floored_at_zero(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _ok({"reports": []})
            adapter.report_inbox_list(cursor=-5)
        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--cursor") + 1] == "0"

    def test_entry_get_command_shape(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout='{"result":{"report_name":"x"},"success":true}', stderr="")
            out = adapter.report_entry_get("rid-1")
        cmd = mock_run.call_args[0][0]
        assert cmd[:4] == ["dws", "report", "entry", "get"]
        assert cmd[cmd.index("--report-id") + 1] == "rid-1"
        assert out["report_name"] == "x"

    def test_entry_get_empty_id_short_circuits(self, adapter):
        """空 report_id 不应发起 CLI 调用。"""
        with patch("subprocess.run") as mock_run:
            assert adapter.report_entry_get("") == {}
        mock_run.assert_not_called()

    def test_template_search_command_shape(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _ok({"templates": [{"name": "周报"}]})
            out = adapter.report_template_search("周报")
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["dws", "report", "+template-search"]
        assert out[0]["name"] == "周报"

    def test_template_search_empty_query_short_circuits(self, adapter):
        with patch("subprocess.run") as mock_run:
            assert adapter.report_template_search("  ") == []
        mock_run.assert_not_called()

    def test_no_deprecated_flat_aliases_used(self, adapter):
        """report 域的扁平别名（list/detail/created/sent/stats）已 deprecated，
        命令里不得出现。"""
        deprecated = {"list", "detail", "created", "sent", "stats", "create"}
        for call in (
            lambda: adapter.report_inbox_list(),
            lambda: adapter.report_outbox_list(),
            lambda: adapter.report_entry_get("r1"),
            lambda: adapter.report_template_search("周报"),
        ):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = _ok({"reports": [], "templates": [], "count": 0})
                call()
            cmd = mock_run.call_args[0][0]
            assert not (set(cmd) & deprecated), f"误用已废弃的扁平别名: {cmd}"

    def test_cli_failure_returns_empty_not_raise(self, adapter):
        from src.dws_adapter import DwsError
        with patch.object(adapter, "run", side_effect=DwsError("boom")):
            assert adapter.report_inbox_list() == []
            assert adapter.report_outbox_list() == []
            assert adapter.report_entry_get("r1") == {}
            assert adapter.report_template_search("周报") == []


# ============ 工具层 ============

class _FakeAdapter:
    """中性化假适配器：不含任何真实姓名。"""

    def __init__(self, inbox=None, entry=None, templates=None):
        self._inbox = inbox if inbox is not None else []
        self._entry = entry or {}
        self._templates = templates or []

    def report_inbox_list(self, start="", end="", *, size=20, cursor=0, sender_user_ids=""):
        return self._inbox

    def report_outbox_list(self, start="", end="", *, size=20, cursor=0,
                           modified_start="", modified_end=""):
        return self._inbox

    def report_entry_get(self, report_id):
        return self._entry if report_id else {}

    def report_template_search(self, query):
        return self._templates


class TestListReportsTool:
    def test_normalizes_camel_to_snake(self):
        fake = _FakeAdapter(inbox=[{
            "reportId": "r1", "templateName": "产品部周报",
            "creatorUserId": "u1", "createTime": 1792405414000,
            "modifiedTime": 1792405414000,
        }])
        out = ListReportsTool(fake).execute({"box": "inbox"})
        item = out["items"][0]
        assert item["report_id"] == "r1"
        assert item["template_name"] == "产品部周报"
        assert item["created_at"].startswith("20"), "毫秒时间戳应转成可读时间"

    def test_default_box_is_inbox(self):
        assert ListReportsTool(_FakeAdapter()).execute({})["box"] == "inbox"

    def test_invalid_box_falls_back_to_inbox(self):
        assert ListReportsTool(_FakeAdapter()).execute({"box": "???"})["box"] == "inbox"

    def test_empty_result_gives_actionable_hint(self):
        """空结果必须给出可操作提示（outbox 与 inbox 的时间窗上限不同）。"""
        out = ListReportsTool(_FakeAdapter()).execute({"box": "outbox"})
        assert out["count"] == 0
        assert "20" in out["hint"], "发件箱空结果应提示其 20 天跨度上限"

    def test_limit_clamped(self):
        out = ListReportsTool(_FakeAdapter(inbox=[{"reportId": "r"}])).execute({"limit": 999})
        assert out["count"] == 1

    def test_adapter_failure_returns_error_dict(self):
        from src.im_adapter.errors import IMAdapterError

        class _Boom(_FakeAdapter):
            def report_inbox_list(self, *a, **k):
                raise IMAdapterError("boom")

        out = ListReportsTool(_Boom()).execute({"box": "inbox"})
        assert "error" in out


class TestGetReportDetailTool:
    _ENTRY = {
        "report_Id": "r1",
        "report_name": "示例提交人的产品部周报",
        "report_template_name": "产品部周报",
        "creatorName": "示例提交人",
        "deptName": "示例部门",
        "createTime": 1792405414000,
        "url": "dingtalk://example?corpId=secret",
        "report_content": [
            {"key": "本周进展", "value": "第一段", "sort": 0, "type": 1, "images": []},
            {"key": "下周计划", "value": '["选项A","选项B"]', "sort": 1, "type": 7,
             "images": []},
        ],
    }

    def test_sections_extracted(self):
        out = GetReportDetailTool(_FakeAdapter(entry=self._ENTRY)).execute({"report_id": "r1"})
        assert out["section_count"] == 2
        assert out["sections"][0]["key"] == "本周进展"
        assert out["template_name"] == "产品部周报"

    def test_multi_value_json_string_preserved(self):
        """type=7 的多选值是 JSON 字符串，须按原文保留而非解析破坏格式。"""
        out = GetReportDetailTool(_FakeAdapter(entry=self._ENTRY)).execute({"report_id": "r1"})
        assert out["sections"][1]["value"] == '["选项A","选项B"]'

    def test_long_field_truncated_with_flag(self):
        long_entry = {"report_content": [{"key": "长文", "value": "x" * 9000, "type": 1}]}
        out = GetReportDetailTool(_FakeAdapter(entry=long_entry)).execute({"report_id": "r"})
        section = out["sections"][0]
        assert section["truncated"] is True
        assert len(section["value"]) == 4000, "超长字段应截断，避免冲爆 LLM 上下文"

    def test_url_not_leaked_only_flag(self):
        """URL 含 corpId 等敏感参数，只透出「有没有链接」，不带原文。"""
        out = GetReportDetailTool(_FakeAdapter(entry=self._ENTRY)).execute({"report_id": "r1"})
        assert out["has_link"] is True
        assert "url" not in out
        assert "secret" not in str(out)

    def test_missing_report_id_returns_error(self):
        out = GetReportDetailTool(_FakeAdapter()).execute({})
        assert "error" in out and "report_id" in out["error"]

    def test_not_found_returns_error(self):
        out = GetReportDetailTool(_FakeAdapter(entry={})).execute({"report_id": "bad"})
        assert "error" in out

    def test_non_dict_content_ignored(self):
        entry = {"report_content": ["坏数据", {"key": "ok", "value": "v"}]}
        out = GetReportDetailTool(_FakeAdapter(entry=entry)).execute({"report_id": "r"})
        assert out["section_count"] == 1


class TestListReportTemplatesTool:
    def test_normalizes(self):
        fake = _FakeAdapter(templates=[{
            "templateId": "t1", "name": "周报", "lastModifiedTime": 1470896774000}])
        out = ListReportTemplatesTool(fake).execute({"query": "周报"})
        assert out["items"][0]["template_id"] == "t1"
        assert out["count"] == 1

    def test_empty_query_returns_error(self):
        out = ListReportTemplatesTool(_FakeAdapter()).execute({"query": "  "})
        assert "error" in out
