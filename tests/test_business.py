"""业务工具单元测试。

覆盖：
- _unwrap 辅助函数（dict/非dict/各嵌套键）
- _to_items 辅助函数（list/dict内嵌list/单dict/None/非典型值）
- GetAttendanceTool（正常/缺userId/获取用户失败/异常/limit边界）
- SendDingTool（正常/缺users/content/无效type/缺robot_code/异常/env robot_code）

注：查待我审批/审批详情由 oa_approval.py 的 approval_list_pending / approval_get_detail
承担（原 get_my_approvals / get_approval_detail 已去重移除），相关测试见
test_tools_oa_approval.py。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# _unwrap
# ============================================================================
class TestUnwrap:
    def test_non_dict_passthrough(self):
        from src.tools.business import _unwrap
        assert _unwrap([1, 2, 3]) == [1, 2, 3]
        assert _unwrap("string") == "string"
        assert _unwrap(None) is None

    def test_result_key(self):
        from src.tools.business import _unwrap
        assert _unwrap({"result": [{"id": 1}]}) == [{"id": 1}]

    def test_items_key(self):
        from src.tools.business import _unwrap
        assert _unwrap({"items": [1, 2]}) == [1, 2]

    def test_list_key(self):
        from src.tools.business import _unwrap
        assert _unwrap({"list": [{"a": 1}]}) == [{"a": 1}]

    def test_records_key(self):
        from src.tools.business import _unwrap
        assert _unwrap({"records": [{"b": 2}]}) == [{"b": 2}]

    def test_no_known_keys(self):
        from src.tools.business import _unwrap
        data = {"unknown": [1, 2], "extra": 3}
        assert _unwrap(data) == data  # 原样返回

    def test_result_not_list_or_dict(self):
        from src.tools.business import _unwrap
        assert _unwrap({"result": 42}) == {"result": 42}  # result 非 list/dict，不提取


# ============================================================================
# _to_items
# ============================================================================
class TestToItems:
    def test_list(self):
        from src.tools.business import _to_items
        result = _to_items([{"id": i} for i in range(100)], limit=50)
        assert result["count"] == 100
        assert len(result["items"]) == 50

    def test_list_under_limit(self):
        from src.tools.business import _to_items
        result = _to_items([1, 2, 3], limit=50)
        assert result["count"] == 3
        assert result["items"] == [1, 2, 3]

    def test_dict_with_nested_list(self):
        from src.tools.business import _to_items
        for key in ("items", "list", "records", "tasks", "approvals"):
            result = _to_items({key: [{"id": 1}, {"id": 2}]}, limit=50)
            assert result["count"] == 2, f"failed for key={key}"

    def test_plain_dict(self):
        from src.tools.business import _to_items
        result = _to_items({"single": True}, limit=50)
        assert result["count"] == 1
        assert result["items"] == [{"single": True}]

    def test_none(self):
        from src.tools.business import _to_items
        result = _to_items(None, limit=50)
        assert result["count"] == 0
        assert result["items"] == []

    def test_primitive(self):
        from src.tools.business import _to_items
        result = _to_items("result", limit=50)
        assert result["count"] == 1
        assert result["items"] == ["result"]


# ============================================================================
# 公共 fixture
# ============================================================================
@pytest.fixture
def dws():
    return MagicMock()


# ============================================================================
# GetAttendanceTool
# ============================================================================
class TestGetAttendanceTool:
    @pytest.fixture
    def tool(self, dws):
        from src.tools.business import GetAttendanceTool
        return GetAttendanceTool(dws)

    def test_success(self, tool, dws):
        dws.contact_user_get_self.return_value = {
            "userId": "u123",
        }
        dws.run.return_value = {"records": [
            {"date": "2026-07-01", "onDuty": "09:00", "offDuty": "18:00"},
        ]}
        result = tool.execute({"start": "2026-07-01", "end": "2026-07-11"})
        assert result["count"] == 1

    def test_org_employee_model_fallback(self, tool, dws):
        """当 me 中外层无 userId，从 orgEmployeeModel 提取。"""
        dws.contact_user_get_self.return_value = {
            "orgEmployeeModel": {"userId": "u456"},
        }
        dws.run.return_value = []
        result = tool.execute({})
        assert result["count"] == 0

    def test_staff_id_fallback(self, tool, dws):
        """staffId 作为 userId 的备用字段。"""
        dws.contact_user_get_self.return_value = {
            "orgEmployeeModel": {"staffId": "s789"},
        }
        dws.run.return_value = []
        result = tool.execute({})
        assert result["count"] == 0

    def test_invalid_limit(self, tool, dws):
        """limit 非整数时应回退到默认值 50。"""
        dws.contact_user_get_self.return_value = {"userId": "u1"}
        dws.run.return_value = []
        tool.execute({"limit": "abc"})
        # 验证 run 被调用时 limit 是 "50"
        call_args = dws.run.call_args[0][0]
        limit_idx = call_args.index("--limit") + 1
        assert call_args[limit_idx] == "50"

    def test_default_dates(self, tool, dws):
        """不传 start/end 时默认本月 1 号到今日。"""
        from datetime import date
        dws.contact_user_get_self.return_value = {"userId": "u1"}
        dws.run.return_value = []
        tool.execute({})
        call_args = dws.run.call_args[0][0]
        today = date.today()
        assert call_args[call_args.index("--start") + 1] == today.replace(day=1).isoformat()
        assert call_args[call_args.index("--end") + 1] == today.isoformat()

    def test_get_self_failure(self, tool, dws):
        dws.contact_user_get_self.side_effect = RuntimeError("no user")
        result = tool.execute({})
        assert "error" in result

    def test_get_self_not_dict(self, tool, dws):
        dws.contact_user_get_self.return_value = []
        result = tool.execute({})
        assert "error" in result

    def test_missing_user_id(self, tool, dws):
        dws.contact_user_get_self.return_value = {}
        result = tool.execute({})
        assert "error" in result

    def test_user_id_all_empty_strings(self, tool, dws):
        """所有 userId 字段均为空字符串时返回错误。"""
        dws.contact_user_get_self.return_value = {
            "userId": "",
            "orgEmployeeModel": {"userId": "", "staffId": ""},
        }
        result = tool.execute({})
        assert "error" in result
        assert "userId" in result["error"]

    def test_run_exception(self, tool, dws):
        dws.contact_user_get_self.return_value = {"userId": "u1"}
        dws.run.side_effect = RuntimeError("api error")
        result = tool.execute({})
        assert "error" in result


# ============================================================================
# SendDingTool
# ============================================================================
class TestSendDingTool:
    @pytest.fixture
    def tool(self, dws):
        from src.tools.business import SendDingTool
        return SendDingTool(dws)

    def test_success(self, tool, dws):
        dws.run.return_value = {"result": {"success": True}}
        result = tool.execute({
            "users": "u1,u2",
            "content": "紧急通知",
            "type": "app",
            "robot_code": "rc123",
        })
        assert result["count"] == 1

    def test_missing_users(self, tool, dws):
        result = tool.execute({"content": "hi"})
        assert "error" in result

    def test_missing_content(self, tool, dws):
        result = tool.execute({"users": "u1"})
        assert "error" in result

    def test_invalid_type(self, tool, dws):
        result = tool.execute({
            "users": "u1", "content": "hi", "type": "email",
        })
        assert "error" in result

    def test_missing_robot_code(self, tool, dws):
        result = tool.execute({
            "users": "u1", "content": "hi", "type": "app",
        })
        assert "error" in result
        assert "robot_code" in result["error"]

    def test_env_robot_code(self, tool, dws):
        """从环境变量读取 robot_code。"""
        dws.run.return_value = {"items": [{"ok": True}]}
        with patch.dict("os.environ", {"DINGTALK_DING_ROBOT_CODE": "env_rc"}, clear=False):
            result = tool.execute({
                "users": "u1", "content": "hi",
            })
            assert result["count"] == 1

    def test_exception(self, tool, dws):
        dws.run.side_effect = RuntimeError("send failed")
        result = tool.execute({
            "users": "u1", "content": "hi", "robot_code": "rc",
        })
        assert "error" in result

    def test_default_type_app(self, tool, dws):
        """不传 type 时默认 app。"""
        dws.run.return_value = []
        tool.execute({
            "users": "u1", "content": "hi", "robot_code": "rc",
        })
        cmd = dws.run.call_args[0][0]
        assert "--type" in cmd
        assert cmd[cmd.index("--type") + 1] == "app"
