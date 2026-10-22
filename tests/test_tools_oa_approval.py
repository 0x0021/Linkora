"""oa_approval 只读工具单测（镜像 test_tools_wiki.py）。

覆盖 6 个只读工具：成功 / 缺必填 / 异常 / limit 越界归一 / detail 未找到。
"""
import pytest

from src.tools.oa_approval import (
    ApprovalListFormsTool,
    ApprovalSearchFormsTool,
    ApprovalGetDetailTool,
    ApprovalListPendingTool,
    ApprovalListTasksTool,
    ApprovalListInitiatedTool,
    ApprovalGetFormSchemaTool,
)
from src.dws_adapter import DwsAdapter


@pytest.fixture
def dws():
    return __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock(spec=DwsAdapter)


def _tool(cls, dws):
    return cls(dws)


# ---------- list_forms ----------
def test_list_forms_success(dws):
    dws.oa_approval_list_forms.return_value = [{"processCode": "P1", "processName": "请假"}]
    out = _tool(ApprovalListFormsTool, dws).execute({})
    assert out["count"] == 1
    assert out["items"][0]["processCode"] == "P1"
    dws.oa_approval_list_forms.assert_called_once_with(cursor="0", limit=100)


def test_list_forms_limit_coerce(dws):
    dws.oa_approval_list_forms.return_value = []
    _tool(ApprovalListFormsTool, dws).execute({"limit": "abc", "cursor": "x"})
    dws.oa_approval_list_forms.assert_called_once_with(cursor="x", limit=100)


def test_list_forms_exception(dws):
    dws.oa_approval_list_forms.side_effect = RuntimeError("boom")
    out = _tool(ApprovalListFormsTool, dws).execute({})
    assert "error" in out


# ---------- search_forms ----------
def test_search_forms_success(dws):
    dws.oa_approval_search_forms.return_value = [{"processName": "报销"}]
    out = _tool(ApprovalSearchFormsTool, dws).execute({"query": "报销"})
    assert out["query"] == "报销"
    assert out["count"] == 1


def test_search_forms_empty_query(dws):
    out = _tool(ApprovalSearchFormsTool, dws).execute({"query": "  "})
    assert "error" in out
    dws.oa_approval_search_forms.assert_not_called()


def test_search_forms_exception(dws):
    dws.oa_approval_search_forms.side_effect = RuntimeError("x")
    out = _tool(ApprovalSearchFormsTool, dws).execute({"query": "a"})
    assert "error" in out


# ---------- get_detail ----------
def test_get_detail_success(dws):
    dws.oa_approval_detail.return_value = {"status": "approved"}
    out = _tool(ApprovalGetDetailTool, dws).execute({"instance_id": "i1"})
    assert out["found"] is True
    assert out["detail"]["status"] == "approved"


def test_get_detail_not_found(dws):
    dws.oa_approval_detail.return_value = None
    out = _tool(ApprovalGetDetailTool, dws).execute({"instance_id": "i1"})
    assert out["found"] is False
    assert out["detail"] is None


def test_get_detail_empty_id(dws):
    out = _tool(ApprovalGetDetailTool, dws).execute({"instance_id": ""})
    assert "error" in out
    dws.oa_approval_detail.assert_not_called()


def test_get_detail_exception(dws):
    dws.oa_approval_detail.side_effect = RuntimeError("x")
    out = _tool(ApprovalGetDetailTool, dws).execute({"instance_id": "i1"})
    assert "error" in out


# ---------- list_pending ----------
def test_list_pending_success(dws):
    dws.oa_approval_list_pending.return_value = [{"instanceId": "x"}]
    out = _tool(ApprovalListPendingTool, dws).execute(
        {"start": "2026-01-01T00:00:00+08:00", "end": "2026-02-01T00:00:00+08:00"}
    )
    assert out["count"] == 1
    # 默认 50 但钉钉单页硬上限 20（a0237a0），工具封顶后实际传 20
    dws.oa_approval_list_pending.assert_called_once_with(
        start="2026-01-01T00:00:00+08:00", end="2026-02-01T00:00:00+08:00",
        query=None, limit=20,
    )


def test_list_pending_default_window(dws):
    """不传 start/end 时默认近 30 天到当前时刻（承接原 get_my_approvals 的职责）。"""
    from datetime import datetime, timedelta, timezone

    dws.oa_approval_list_pending.return_value = []
    _tool(ApprovalListPendingTool, dws).execute({})
    call = dws.oa_approval_list_pending.call_args
    start, end = call.kwargs["start"], call.kwargs["end"]
    now = datetime.now(timezone(timedelta(hours=8)))
    expected_start = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    expected_end = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    assert start == expected_start
    assert end == expected_end
    assert call.kwargs["query"] is None
    assert call.kwargs["limit"] == 20  # 钉钉单页硬上限 20


def test_list_pending_partial_window(dws):
    """只传 start 时 end 默认当前时刻；只传 end 时 start 默认近 30 天。"""
    dws.oa_approval_list_pending.return_value = []
    _tool(ApprovalListPendingTool, dws).execute({"start": "2026-01-01T00:00:00+08:00"})
    call = dws.oa_approval_list_pending.call_args
    assert call.kwargs["start"] == "2026-01-01T00:00:00+08:00"
    assert call.kwargs["end"].startswith("20")


def test_list_pending_query_and_limit(dws):
    dws.oa_approval_list_pending.return_value = []
    _tool(ApprovalListPendingTool, dws).execute(
        {"start": "s", "end": "e", "query": "报销", "limit": 10}
    )
    dws.oa_approval_list_pending.assert_called_once_with(
        start="s", end="e", query="报销", limit=10
    )


# ---------- list_tasks ----------
def test_list_tasks_success(dws):
    dws.oa_approval_tasks.return_value = [{"taskId": "t1"}]
    out = _tool(ApprovalListTasksTool, dws).execute({"instance_id": "i1"})
    assert out["count"] == 1
    dws.oa_approval_tasks.assert_called_once_with(instance_id="i1")


def test_list_tasks_empty_id(dws):
    out = _tool(ApprovalListTasksTool, dws).execute({"instance_id": None})
    assert "error" in out


def test_list_tasks_exception(dws):
    dws.oa_approval_tasks.side_effect = RuntimeError("x")
    out = _tool(ApprovalListTasksTool, dws).execute({"instance_id": "i1"})
    assert "error" in out


# ---------- list_initiated ----------
def test_list_initiated_success(dws):
    # 真实 dws 返回 dict（含 processInstanceList），工具剥壳后累计
    dws.oa_approval_list_initiated.return_value = {"processInstanceList": [{"instanceId": "a"}]}
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "s", "end": "e"}
    )
    assert out["count"] == 1
    dws.oa_approval_list_initiated.assert_called_once_with(
        process_code="PC", start="s", end="e", cursor="0", limit=20
    )


def test_list_initiated_missing_required(dws):
    out = _tool(ApprovalListInitiatedTool, dws).execute({"process_code": "PC"})
    assert "error" in out
    dws.oa_approval_list_initiated.assert_not_called()


def test_list_initiated_limit_coerce(dws):
    dws.oa_approval_list_initiated.return_value = []
    _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "s", "end": "e", "limit": "0", "cursor": "c"}
    )
    dws.oa_approval_list_initiated.assert_called_once_with(
        process_code="PC", start="s", end="e", cursor="c", limit=20
    )


# ---------- 工具元数据 ----------
def test_tool_metadata_and_intent(dws):
    for cls in (
        ApprovalListFormsTool, ApprovalSearchFormsTool, ApprovalGetDetailTool,
        ApprovalListPendingTool, ApprovalListTasksTool, ApprovalListInitiatedTool,
    ):
        t = cls(dws)
        assert t.platforms == ["dingtalk"]
        assert t.intent_categories == ["domain.oa_approval"]
        assert t.require_confirm is False
        assert t.name.startswith("approval_")


# ---------- get_form_schema ----------
def _schema(process_name="项目", fields=None):
    return {
        "process_code": "P1",
        "process_name": process_name,
        "process_status": "PUBLISHED",
        "can_modify": False,
        "fields": fields if fields is not None else [],
        "field_count": len(fields or []),
    }


def test_get_form_schema_by_process_code(dws):
    dws.oa_approval_form_schema.return_value = _schema(
        fields=[{"label": "项目", "component": "DDSelectField", "id": "F1",
                 "required": False, "options": []}]
    )
    out = _tool(ApprovalGetFormSchemaTool, dws).execute({"process_code": "P1"})
    assert out["count"] == 1
    assert out["items"][0]["process_name"] == "项目"
    assert out["items"][0]["field_count"] == 1
    dws.oa_approval_form_schema.assert_called_once_with("P1")


def test_get_form_schema_requires_one_of_code_or_name(dws):
    out = _tool(ApprovalGetFormSchemaTool, dws).execute({})
    assert "error" in out
    dws.oa_approval_form_schema.assert_not_called()


def test_get_form_schema_by_form_name_searches_first(dws):
    dws.oa_approval_search_forms.return_value = [
        {"processCode": "P1", "name": "项目"},
        {"processCode": "P2", "name": "项目验收"},
    ]
    dws.oa_approval_form_schema.return_value = _schema()
    out = _tool(ApprovalGetFormSchemaTool, dws).execute({"form_name": "项目"})
    assert out["count"] == 2
    dws.oa_approval_search_forms.assert_called_once_with(query="项目")
    assert dws.oa_approval_form_schema.call_count == 2


def test_get_form_schema_field_name_filters(dws):
    dws.oa_approval_search_forms.return_value = [{"processCode": "P1", "name": "项目"}]
    dws.oa_approval_form_schema.return_value = _schema(
        fields=[
            {"label": "项目编号", "component": "TextField", "id": "F1",
             "required": True, "options": []},
            {"label": "备注", "component": "TextareaField", "id": "F2",
             "required": False, "options": []},
        ]
    )
    out = _tool(ApprovalGetFormSchemaTool, dws).execute(
        {"form_name": "项目", "field_name": "项目"})
    assert out["count"] == 1
    matched = out["items"][0]["matched_fields"]
    assert len(matched) == 1
    assert matched[0]["label"] == "项目编号"
    assert out["field_name"] == "项目"


def test_get_form_schema_no_match_returns_hint(dws):
    dws.oa_approval_form_schema.return_value = _schema(
        fields=[{"label": "备注", "component": "TextareaField", "id": "F2",
                 "required": False, "options": []}]
    )
    out = _tool(ApprovalGetFormSchemaTool, dws).execute(
        {"process_code": "P1", "field_name": "项目"})
    assert out["count"] == 0
    assert "没有表单" in out["hint"]


def test_get_form_schema_search_miss(dws):
    dws.oa_approval_search_forms.return_value = []
    out = _tool(ApprovalGetFormSchemaTool, dws).execute({"form_name": "不存在"})
    assert out["count"] == 0
    assert out["items"] == []
    assert "未找到" in out["hint"]


def test_get_form_schema_error_collected(dws):
    dws.oa_approval_form_schema.return_value = {"error": "无权限"}
    out = _tool(ApprovalGetFormSchemaTool, dws).execute({"process_code": "P1"})
    assert out["count"] == 0
    assert out["errors"][0]["process_code"] == "P1"


def test_get_form_schema_max_forms_caps(dws):
    dws.oa_approval_search_forms.return_value = [
        {"processCode": f"P{i}", "name": f"表单{i}"} for i in range(10)
    ]
    dws.oa_approval_form_schema.return_value = _schema()
    _tool(ApprovalGetFormSchemaTool, dws).execute({"form_name": "表单", "max_forms": 3})
    assert dws.oa_approval_form_schema.call_count == 3


def test_get_form_schema_metadata(dws):
    t = _tool(ApprovalGetFormSchemaTool, dws)
    assert t.platforms == ["dingtalk"]
    assert t.intent_categories == ["domain.oa_approval"]
    assert t.require_confirm is False
    assert t.name == "approval_get_form_schema"
