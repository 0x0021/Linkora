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
    ApprovalListOrgTool,
    ApprovalListCcTool,
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
        {"process_code": "PC", "start": "2026-09-01T00:00:00+08:00", "end": "2026-10-26T00:00:00+08:00"}
    )
    assert out["count"] == 1
    dws.oa_approval_list_initiated.assert_called_once_with(
        process_code="PC", start="2026-09-01T00:00:00+08:00",
        end="2026-10-26T00:00:00+08:00", cursor="0", limit=20,
    )


def test_list_initiated_missing_required(dws):
    out = _tool(ApprovalListInitiatedTool, dws).execute({"process_code": "PC"})
    assert "error" in out
    dws.oa_approval_list_initiated.assert_not_called()


def test_list_initiated_invalid_time_rejected(dws):
    # 非法时间串不再透传给 dws（曾导致服务端报误导性的「时间戳无效」）
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "s", "end": "e"}
    )
    assert "error" in out
    dws.oa_approval_list_initiated.assert_not_called()


def test_list_initiated_limit_coerce(dws):
    dws.oa_approval_list_initiated.return_value = []
    _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "2026-09-01T00:00:00+08:00",
         "end": "2026-10-26T00:00:00+08:00", "limit": "0", "cursor": "c"}
    )
    dws.oa_approval_list_initiated.assert_called_once_with(
        process_code="PC", start="2026-09-01T00:00:00+08:00",
        end="2026-10-26T00:00:00+08:00", cursor="c", limit=20,
    )


def test_list_initiated_overlong_window_auto_split(dws):
    # 239 天窗口（一年内）自动切为 2 段（120+119）
    dws.oa_approval_list_initiated.return_value = {"processInstanceList": []}
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "2026-03-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    calls = dws.oa_approval_list_initiated.call_args_list
    assert len(calls) == 2
    assert out["windows_queried"] == 2
    starts = [c.kwargs["start"] for c in calls]
    ends = [c.kwargs["end"] for c in calls]
    assert starts[0] == "2026-03-01T00:00:00+08:00"
    assert ends[-1] == "2026-10-26T23:59:59+08:00"
    assert starts[1] == ends[0]  # 首尾相接不重叠


def test_list_initiated_start_older_than_year_clamped(dws):
    # start 超一年（接口限制）自动 clamp 到一年前并在 meta 标注
    dws.oa_approval_list_initiated.return_value = {"processInstanceList": []}
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "2025-10-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    assert out["start_clamped"] is True
    assert out["effective_start"] > "2025-10-01"
    calls = dws.oa_approval_list_initiated.call_args_list
    assert calls[0].kwargs["start"] > "2025-10-01"  # 实际请求用的是 clamp 后起点


def test_list_initiated_window_too_long_rejected(dws):
    # end 在远未来：跨度超 12 段上限（1440 天），不发起请求风暴
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "2026-10-01T00:00:00+08:00",
         "end": "2031-01-01T00:00:00+08:00"}
    )
    assert "error" in out
    dws.oa_approval_list_initiated.assert_not_called()


def test_list_initiated_overlong_window_dedup(dws):
    # 跨段重复实例按 processInstanceId 去重
    inst = {"processInstanceId": "dup"}
    dws.oa_approval_list_initiated.return_value = {"processInstanceList": [inst]}
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "2025-10-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    assert out["count"] == 1


def test_list_initiated_dedup_across_segments(dws):
    # 跨段重复实例按 processInstanceId 去重
    inst = {"processInstanceId": "dup"}
    dws.oa_approval_list_initiated.return_value = {"processInstanceList": [inst]}
    out = _tool(ApprovalListInitiatedTool, dws).execute(
        {"process_code": "PC", "start": "2026-03-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    assert out["count"] == 1


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


# ---------- approval_list_org（管理员视角，别人转发场景） ----------
def test_list_org_success(dws):
    dws.oa_approval_list_by_admin.return_value = {
        "list": [{"processInstanceId": "i1", "title": "周萍提交的其他合同审批"}]
    }
    out = _tool(ApprovalListOrgTool, dws).execute(
        {"process_code": "PC", "start": "2026-09-26T00:00:00+08:00",
         "end": "2026-10-26T00:00:00+08:00"}
    )
    assert out["count"] == 1
    assert out["items"][0]["title"] == "周萍提交的其他合同审批"
    dws.oa_approval_list_by_admin.assert_called_once_with(
        process_code="PC", start="2026-09-26T00:00:00+08:00",
        end="2026-10-26T00:00:00+08:00", cursor="0", limit=20,
        statuses=None, user_ids=None,
    )


def test_list_org_filters_passthrough(dws):
    dws.oa_approval_list_by_admin.return_value = {"list": []}
    _tool(ApprovalListOrgTool, dws).execute(
        {"process_code": "PC", "start": "2026-09-26T00:00:00+08:00",
         "end": "2026-10-26T00:00:00+08:00", "statuses": ["RUNNING"],
         "user_ids": ["u1", "u2"]}
    )
    kwargs = dws.oa_approval_list_by_admin.call_args.kwargs
    assert kwargs["statuses"] == ["RUNNING"]
    assert kwargs["user_ids"] == ["u1", "u2"]


def test_list_org_overlong_window_auto_split(dws):
    # 239 天窗口自动切为 2 段
    dws.oa_approval_list_by_admin.return_value = {"list": []}
    out = _tool(ApprovalListOrgTool, dws).execute(
        {"process_code": "PC", "start": "2026-03-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    calls = dws.oa_approval_list_by_admin.call_args_list
    assert len(calls) == 2
    assert out["windows_queried"] == 2
    starts = [c.kwargs["start"] for c in calls]
    ends = [c.kwargs["end"] for c in calls]
    assert starts[0] == "2026-03-01T00:00:00+08:00"
    assert ends[-1] == "2026-10-26T23:59:59+08:00"
    assert starts[1] == ends[0]  # 首尾相接不重叠


def test_list_org_start_older_than_year_clamped(dws):
    dws.oa_approval_list_by_admin.return_value = {"list": []}
    out = _tool(ApprovalListOrgTool, dws).execute(
        {"process_code": "PC", "start": "2025-10-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    assert out["start_clamped"] is True
    calls = dws.oa_approval_list_by_admin.call_args_list
    assert calls[0].kwargs["start"] > "2025-10-01"


def test_list_org_dedup_across_segments(dws):
    inst = {"processInstanceId": "dup", "title": "t"}
    dws.oa_approval_list_by_admin.return_value = {"list": [inst]}
    out = _tool(ApprovalListOrgTool, dws).execute(
        {"process_code": "PC", "start": "2026-03-01T00:00:00+08:00",
         "end": "2026-10-26T23:59:59+08:00"}
    )
    assert out["count"] == 1


def test_list_org_invalid_time_rejected(dws):
    out = _tool(ApprovalListOrgTool, dws).execute(
        {"process_code": "PC", "start": "s", "end": "e"}
    )
    assert "error" in out
    dws.oa_approval_list_by_admin.assert_not_called()


# ---------- approval_list_cc（抄送我的） ----------
def test_list_cc_success(dws):
    dws.oa_approval_list_cc.return_value = {
        "values": [{"originatorName": "王晓航", "formMassage": "x"}], "hasMore": True,
    }
    out = _tool(ApprovalListCcTool, dws).execute({"limit": 5, "page": "2"})
    assert out["count"] == 1
    assert out["page"] == 2
    assert out["has_more"] is True
    dws.oa_approval_list_cc.assert_called_once_with(
        page=2, limit=5, query=None, process_code=None,
        originator_user_id=None, create_time_from=None, create_time_to=None,
    )


def test_list_cc_limit_cap_and_filters(dws):
    dws.oa_approval_list_cc.return_value = {"values": []}
    _tool(ApprovalListCcTool, dws).execute(
        {"limit": 500, "query": "合同", "process_code": "PC",
         "originator_user_id": "u1", "create_time_from": "2026-10-01",
         "create_time_to": "2026-10-26"}
    )
    kwargs = dws.oa_approval_list_cc.call_args.kwargs
    assert kwargs["limit"] == 100  # 上限 100
    assert kwargs["query"] == "合同"
    assert kwargs["create_time_from"] == "2026-10-01"


def test_list_cc_exception(dws):
    dws.oa_approval_list_cc.side_effect = RuntimeError("boom")
    out = _tool(ApprovalListCcTool, dws).execute({})
    assert "error" in out
