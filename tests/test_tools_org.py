"""组织 / 个人资料工具测试。"""
from unittest.mock import MagicMock

from src.tools.org import GetCurrentOrgTool, GetMyProfileTool, ListOrgsTool, _strip


class TestStrip:
    def test_none(self): assert _strip(None) == ""
    def test_string(self): assert _strip("  hello  ") == "hello"
    def test_int(self): assert _strip(42) == "42"
    def test_empty(self): assert _strip("") == ""


class TestGetMyProfile:
    def test_nested_emp(self):
        dws = MagicMock()
        dws.contact_user_get_self.return_value = {
            "orgEmployeeModel": {
                "orgUserName": "张三", "userId": "u001",
                "mobile": "13800138000", "email": "z@c.com",
                "title": "工程师", "orgName": "某公司",
                "depts": [{"deptName": "研发部"}],
            }
        }
        tool = GetMyProfileTool(dws)
        res = tool.execute({})
        assert res["name"] == "张三"
        assert res["userId"] == "u001"
        assert res["mobile"] == "13800138000"
        assert res["dept"] == "研发部"
        assert res["orgName"] == "某公司"

    def test_flat_info(self):
        dws = MagicMock()
        dws.contact_user_get_self.return_value = {
            "name": "李四", "staffId": "s002",
            "mobile": "139", "email": "l@d.com",
        }
        tool = GetMyProfileTool(dws)
        res = tool.execute({})
        assert res["name"] == "李四"
        assert res["userId"] == "s002"

    def test_dept_first_non_dict(self):
        dws = MagicMock()
        dws.contact_user_get_self.return_value = {
            "name": "王五",
            "depts": ["产品部", "设计部"],
        }
        tool = GetMyProfileTool(dws)
        res = tool.execute({})
        assert res["dept"] == "产品部"

    def test_empty_result_personal(self):
        dws = MagicMock()
        dws.contact_user_get_self.return_value = {}
        tool = GetMyProfileTool(dws)
        res = tool.execute({})
        assert "note" in res
        assert "个人模式" in res["note"]

    def test_exception(self):
        dws = MagicMock()
        dws.contact_user_get_self.side_effect = RuntimeError("无权限")
        tool = GetMyProfileTool(dws)
        res = tool.execute({})
        assert "error" in res
        assert "无权限" in res["error"]


class TestListOrgs:
    def test_success(self):
        dws = MagicMock()
        dws.list_orgs.return_value = [
            {"corp_id": "c1", "corp_name": "阿里"},
            {"corp_id": "c2", "corpName": "某云"},
        ]
        tool = ListOrgsTool(dws)
        res = tool.execute({})
        assert res["count"] == 2
        assert res["orgs"][0]["corp_name"] == "阿里"
        assert res["orgs"][1]["corp_name"] == "某云"

    def test_empty(self):
        dws = MagicMock()
        dws.list_orgs.return_value = []
        tool = ListOrgsTool(dws)
        res = tool.execute({})
        assert res["count"] == 0

    def test_exception(self):
        dws = MagicMock()
        dws.list_orgs.side_effect = RuntimeError("fail")
        tool = ListOrgsTool(dws)
        res = tool.execute({})
        assert "error" in res

    def test_filter_non_dict_items(self):
        dws = MagicMock()
        dws.list_orgs.return_value = [None, "str", {"corp_id": "c1"}, 42]
        tool = ListOrgsTool(dws)
        res = tool.execute({})
        assert res["count"] == 1
        assert res["orgs"][0]["corp_id"] == "c1"


class TestGetCurrentOrg:
    def test_success(self):
        dws = MagicMock()
        dws.get_current_org.return_value = {"corp_id": "c99", "corp_name": "测试公司"}
        tool = GetCurrentOrgTool(dws)
        res = tool.execute({})
        assert res["corp_id"] == "c99"
        assert res["corp_name"] == "测试公司"

    def test_empty(self):
        dws = MagicMock()
        dws.get_current_org.return_value = {}
        tool = GetCurrentOrgTool(dws)
        res = tool.execute({})
        assert "note" in res

    def test_exception(self):
        dws = MagicMock()
        dws.get_current_org.side_effect = RuntimeError("boom")
        tool = GetCurrentOrgTool(dws)
        res = tool.execute({})
        assert "error" in res

    def test_corp_name_fallback(self):
        dws = MagicMock()
        dws.get_current_org.return_value = {"corp_id": "x", "corpName": "旧字段"}
        tool = GetCurrentOrgTool(dws)
        res = tool.execute({})
        assert res["corp_name"] == "旧字段"
