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


class TestOaApprovalFormSchema:
    """oa_approval_form_schema：审批模板字段级配置。

    实测 dws v1.0.63 返回体是**双层嵌套**：
    {"errorMsg":"ok","result":{"content":"<JSON 字符串>",...},"success":true}
    content 内部又是 {"title":..,"items":[{"componentName":..,"props":{..}}]}。
    """

    def _schema_payload(self, content_obj):
        import json
        return {
            "errorMsg": "ok",
            "success": True,
            "result": {
                "content": json.dumps(content_obj, ensure_ascii=False),
                "processCode": "PROC-1",
                "processName": "项目",
                "processStatus": "PUBLISHED",
            },
        }

    def test_passes_process_code(self):
        adapter = _make_adapter(self._schema_payload({"items": []}))
        adapter.oa_approval_form_schema("PROC-1")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["oa", "approval", "form-schema"]
        assert "--process-code" in args and "PROC-1" in args

    def test_parses_nested_content_json(self):
        payload = self._schema_payload({
            "title": "项目",
            "canModify": False,
            "items": [
                {"componentName": "DDSelectField",
                 "props": {"label": "项目", "required": False, "id": "F-1"}},
                {"componentName": "TextField",
                 "props": {"label": "项目编号", "required": True, "id": "F-2"}},
            ],
        })
        adapter = _make_adapter(payload)
        res = adapter.oa_approval_form_schema("PROC-1")
        assert res["field_count"] == 2
        assert res["process_name"] == "项目"
        assert res["process_status"] == "PUBLISHED"
        assert res["can_modify"] is False
        assert res["fields"][0] == {
            "label": "项目", "component": "DDSelectField", "id": "F-1",
            "required": False, "options": [],
        }
        assert res["fields"][1]["label"] == "项目编号"
        assert res["fields"][1]["required"] is True

    def test_options_preserved(self):
        payload = self._schema_payload({
            "items": [{"componentName": "DDSelectField",
                       "props": {"label": "项目", "options": [{"value": "P1"}]}}],
        })
        adapter = _make_adapter(payload)
        res = adapter.oa_approval_form_schema("PROC-1")
        assert res["fields"][0]["options"] == [{"value": "P1"}]

    def test_empty_process_code_returns_error(self):
        adapter = _make_adapter({})
        res = adapter.oa_approval_form_schema("")
        assert "error" in res
        adapter.run.assert_not_called()

    def test_success_false_returns_error(self):
        adapter = _make_adapter({"success": False, "errorMsg": "无权限"})
        res = adapter.oa_approval_form_schema("PROC-1")
        assert res["error"] == "无权限"

    def test_broken_content_json_returns_error(self):
        adapter = _make_adapter({"success": True, "result": {"content": "{not json"}})
        res = adapter.oa_approval_form_schema("PROC-1")
        assert "error" in res

    def test_run_failure_degrades_to_error_dict(self):
        # run() 抛的是 IMAdapterError 族（CLI 失败/JSON 解析失败），
        # 不含 RuntimeError —— 用真实异常类型验证降级路径。
        from src.im_adapter.errors import IMAdapterError
        adapter = _make_adapter(None)
        adapter.run.side_effect = IMAdapterError("dws exit 1: boom")
        res = adapter.oa_approval_form_schema("PROC-1")
        assert "error" in res
