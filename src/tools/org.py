from __future__ import annotations

import logging

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _strip(val) -> str:
    """把可能为空/非字符串的值规整为字符串。"""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    return str(val)


class GetMyProfileTool(BaseTool):
    name = "get_my_profile"
    display_name = "查询我的信息"
    short_description = "查询当前登录用户的个人资料，包括姓名、工号、手机号、邮箱、职位、所属部门与组织，便于自我介绍或补全上下文"
    description = "查询当前登录用户的个人资料（姓名/工号/手机/邮箱/职位/部门/组织）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.profile（单一真源）
    intent_categories = ["domain.profile"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        try:
            info = self.dws.contact_user_get_self()
        except Exception as e:
            logger.exception("获取我的信息失败: %s", e)
            return {"error": f"获取我的信息失败: {e}"}

        # 个人模式 / 无权限：适配器返回 {}
        if not isinstance(info, dict) or not info:
            return {
                "name": "",
                "userId": "",
                "orgName": "",
                "note": "当前为个人模式或未开通 CLI 数据访问权限，无法获取企业用户信息",
            }

        # 不同 dws 版本返回结构可能嵌套在 orgEmployeeModel 下，做兼容
        emp = info.get("orgEmployeeModel", info)

        depts = emp.get("depts") or info.get("depts") or []
        dept_name = ""
        if isinstance(depts, list) and depts:
            first = depts[0]
            if isinstance(first, dict):
                dept_name = (
                    first.get("deptName")
                    or first.get("name")
                    or ""
                )
            else:
                dept_name = _strip(first)

        return {
            "name": _strip(emp.get("orgUserName") or emp.get("name") or info.get("name") or emp.get("nickName")),
            "userId": _strip(emp.get("userId") or emp.get("staffId") or info.get("userId") or info.get("unionId")),
            "mobile": _strip(emp.get("mobile") or info.get("mobile")),
            "email": _strip(emp.get("email") or info.get("email")),
            "jobTitle": _strip(emp.get("title") or emp.get("jobTitle") or info.get("title")),
            "dept": _strip(dept_name),
            "orgName": _strip(emp.get("orgName") or info.get("orgName") or info.get("corpName")),
        }


class ListOrgsTool(BaseTool):
    name = "list_orgs"
    display_name = "查询我加入的组织"
    platforms = ["dingtalk"]
    short_description = "列出当前账号已登录/已加入的所有组织（含 corp_id 与组织名称），用于了解多组织归属"
    description = "列出当前账号已加入的组织列表"
    # 场景关键词统一维护在 IntentRegistry 的 domain.org（单一真源）
    intent_categories = ["domain.org"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        try:
            orgs = self.dws.list_orgs()
        except Exception as e:
            logger.exception("获取组织列表失败: %s", e)
            return {"error": f"获取组织列表失败: {e}"}

        items = []
        for o in (orgs or []):
            if not isinstance(o, dict):
                continue
            items.append({
                "corp_id": _strip(o.get("corp_id")),
                "corp_name": _strip(o.get("corp_name") or o.get("corpName")),
            })
        return {"count": len(items), "orgs": items}


class GetCurrentOrgTool(BaseTool):
    name = "get_current_org"
    display_name = "查询当前组织"
    platforms = ["dingtalk"]
    short_description = "查询当前生效的组织（即正在使用的 corp_id 与组织名称），便于确认工作上下文归属"
    description = "查询当前生效的组织（corp_id 与组织名称）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.org（单一真源）
    intent_categories = ["domain.org"]
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        try:
            org = self.dws.get_current_org()
        except Exception as e:
            return {"error": f"获取当前组织失败: {e}"}

        if not isinstance(org, dict):
            org = {}

        corp_id = _strip(org.get("corp_id"))
        corp_name = _strip(org.get("corp_name") or org.get("corpName"))
        if not corp_id and not corp_name:
            return {
                "corp_id": "",
                "corp_name": "",
                "note": "未能识别当前组织（可能未配置 CLI 权限或本地 profile 缺失）",
            }
        return {"corp_id": corp_id, "corp_name": corp_name}
