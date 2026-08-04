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
