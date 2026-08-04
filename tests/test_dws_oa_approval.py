"""DWS Adapter · OA 审批读取方法单元测试。

覆盖 dws_adapter.DwsAdapter 新增的 oa approval 封装：
- oa_approval_list_forms / search_forms / detail / list_pending / tasks
验证命令前缀、关键 flag 透传，以及 _get_result 解析与失败兜底。
（dws CLI 调用经 MagicMock 拦截，不真正执行子进程。）
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.dws_adapter import DwsAdapter


def _make_adapter(run_return):
    adapter = DwsAdapter(cli_path="dws", timeout=5)
    adapter.run = MagicMock(return_value=run_return)
    return adapter


class TestOaApprovalReadMethods:
    def test_list_forms_passes_cursor_limit(self):
        adapter = _make_adapter({"result": [{"name": "报销"}], "success": True})
        forms = adapter.oa_approval_list_forms(cursor="0", limit=50)
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "list-forms"]
        assert "--cursor" in args and "0" in args
        assert "--limit" in args and "50" in args
        assert forms == [{"name": "报销"}]

    def test_search_forms_query(self):
        adapter = _make_adapter({"result": [{"name": "报销单"}], "success": True})
        res = adapter.oa_approval_search_forms("报销")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "search-forms"]
        assert "--query" in args and "报销" in args
        assert res == [{"name": "报销单"}]

    def test_detail_returns_dict(self):
        adapter = _make_adapter({"result": {"fields": []}, "success": True})
        d = adapter.oa_approval_detail("inst-1")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "detail"]
        assert "--instance-id" in args and "inst-1" in args
        assert d == {"fields": []}

    def test_detail_none_on_error(self):
        adapter = _make_adapter(None)
        adapter.run.side_effect = RuntimeError("boom")
        assert adapter.oa_approval_detail("x") is None

    def test_list_pending_requires_start_end(self):
        adapter = _make_adapter({"result": []})
        adapter.oa_approval_list_pending(
            "2026-01-01T00:00:00+08:00", "2026-12-31T23:59:59+08:00")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "list-pending"]
        assert "--start" in args and "--end" in args
        # 无 query 时不带 --query / 不重复
        assert "--query" not in args

    def test_list_pending_adds_optional_query(self):
        adapter = _make_adapter({"result": []})
        adapter.oa_approval_list_pending(
            "2026-01-01T00:00:00+08:00", "2026-12-31T23:59:59+08:00", query="报销")
        args = adapter.run.call_args[0][0]
        assert "--query" in args and "报销" in args

    def test_tasks_returns_list(self):
        adapter = _make_adapter({"result": ["task-1"]})
        res = adapter.oa_approval_tasks("inst-1")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "tasks"]
        assert "--instance-id" in args and "inst-1" in args
        assert res == ["task-1"]

    def test_list_forms_empty_on_non_list_result(self):
        adapter = _make_adapter({"result": {"unexpected": 1}})
        assert adapter.oa_approval_list_forms() == []

    def test_list_forms_real_dict_schema(self):
        """真实 API 返回 {'processCodeList':[...], 'totalCount':-1}（dict），
        应归一化为 list，而非被 isinstance(list) 守卫误判为空。"""
        real = {
            "result": {
                "processCodeList": [
                    {"dirName": "人力资源", "processCode": "PROC-A", "processName": "补卡申请"},
                    {"dirName": "研发流程", "processCode": "PROC-B", "processName": "项目"},
                ],
                "totalCount": -1,
            },
            "success": True,
        }
        adapter = _make_adapter(real)
        forms = adapter.oa_approval_list_forms(cursor="0", limit=100)
        assert isinstance(forms, list)
        assert len(forms) == 2
        assert forms[0]["processCode"] == "PROC-A"
        # processCodeList 内仍是 dict（含 dirName/processName），原样透传
        assert forms[1]["processName"] == "项目"

    def test_list_initiated_passes_required_flags(self):
        # 真实 dws 返回 dict（含 processInstanceList），由上层工具剥壳；mock 对齐该契约
        adapter = _make_adapter({"result": {"processInstanceList": [{"processInstanceId": "PI-1"}]}})
        res = adapter.oa_approval_list_initiated(
            "PROC-X", "2026-01-01T00:00:00+08:00", "2026-01-31T23:59:59+08:00",
            cursor="0", limit=20)
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "list-initiated"]
        assert "--process-code" in args and "PROC-X" in args
        assert "--start" in args and "--end" in args
        assert "--cursor" in args and "--limit" in args
        assert res == {"processInstanceList": [{"processInstanceId": "PI-1"}]}
