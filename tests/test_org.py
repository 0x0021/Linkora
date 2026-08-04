"""OrgTool 单元测试 — 覆盖 get_current_org 返回非 dict 值的边界。"""

from unittest.mock import MagicMock

from src.tools.org import GetCurrentOrgTool


def test_org_returns_non_dict_value():
    """get_current_org 返回 None / 非dict → 退化为空字典。"""
    dws = MagicMock()
    dws.get_current_org.return_value = None

    tool = GetCurrentOrgTool(dws)
    r = tool.execute({})
    assert r["corp_id"] == ""
    assert "未能识别" in r.get("note", "")


def test_org_returns_list():
    """get_current_org 返回列表 → isinstance 检查不通过。"""
    dws = MagicMock()
    dws.get_current_org.return_value = ["not a dict"]

    tool = GetCurrentOrgTool(dws)
    r = tool.execute({})
    assert r["corp_id"] == ""


def test_org_returns_string():
    """get_current_org 返回纯字符串 → 同样退化为空。"""
    dws = MagicMock()
    dws.get_current_org.return_value = "some string"

    tool = GetCurrentOrgTool(dws)
    r = tool.execute({})
    assert "未能识别" in r.get("note", "")


def test_org_valid_dict():
    """正常 dict 返回正确的 corp_id / corp_name。"""
    dws = MagicMock()
    dws.get_current_org.return_value = {"corp_id": "cid123", "corp_name": "腾讯"}

    tool = GetCurrentOrgTool(dws)
    r = tool.execute({})
    assert r["corp_id"] == "cid123"
    assert r["corp_name"] == "腾讯"


def test_org_camel_case_corp_name():
    """camelCase 'corpName' 也正确降级。"""
    dws = MagicMock()
    dws.get_current_org.return_value = {"corpId": "c2", "corpName": "Ali"}

    tool = GetCurrentOrgTool(dws)
    r = tool.execute({})
    assert r["corp_id"] == ""
    assert r["corp_name"] == "Ali"


def test_org_exception():
    """get_current_org 抛异常时返回 error。"""
    dws = MagicMock()
    dws.get_current_org.side_effect = RuntimeError("network down")

    tool = GetCurrentOrgTool(dws)
    r = tool.execute({})
    assert "error" in r
