from __future__ import annotations

import logging
import os
from datetime import date

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _unwrap(data):
    """把 dws 返回的可能嵌套结构规整成「列表或字典」。

    dws 不同命令的返回外层结构不统一（有的包在 result / items / list / records 下），
    这里做兼容提取，便于工具稳定返回。
    """
    if not isinstance(data, dict):
        return data
    if "result" in data and isinstance(data["result"], (list, dict)):
        return data["result"]
    if "items" in data and isinstance(data["items"], list):
        return data["items"]
    if "list" in data and isinstance(data["list"], list):
        return data["list"]
    if "records" in data and isinstance(data["records"], list):
        return data["records"]
    return data


def _to_items(unwrapped, limit: int = 50) -> dict:
    """把 unwrap 结果整理成 {count, items}，对超大列表做截断。"""
    if isinstance(unwrapped, list):
        total = len(unwrapped)
        return {"count": total, "items": unwrapped[:limit]}
    if isinstance(unwrapped, dict):
        # 有些命令在外层 dict 内又嵌了列表字段
        for key in ("items", "list", "records", "tasks", "approvals"):
            if isinstance(unwrapped.get(key), list):
                items = unwrapped[key]
                return {"count": len(items), "items": items[:limit]}
        return {"count": 1, "items": [unwrapped]}
    if unwrapped is None:
        return {"count": 0, "items": []}
    return {"count": 1, "items": [unwrapped]}


class TransferApprovalTool(BaseTool):
    name = "transfer_approval"
    display_name = "转交审批"
    short_description = (
        "把 OA 审批任务转交给指定同事处理（如原审批人离职/休假），"
        "自动解析审批详情、校验目标人有效性，并返回带时间戳的真实转交结果"
    )
    description = "将 OA 审批任务转交给指定人员，返回转交状态、目标人确认信息与时间戳"
    # 场景关键词统一维护在 IntentRegistry 的 domain.approval（单一真源）
    intent_categories = ["domain.approval"]
    # 钉钉 OA 审批专属（通用编排在 src/approval/service.py，其他平台实现 Provider 即可复用）
    platforms = ["dingtalk"]
    # 不可逆写操作：转交会变更他人审批流，执行前需用户二次确认（防误解析越权）
    require_confirm = True
    parameters = {
        "type": "object",
        "properties": {
            "target_name": {
                "type": "string",
                "description": "转交目标人姓名（将在通讯录中校验唯一性）",
            },
            "instance_id": {
                "type": "string",
                "description": "审批实例 ID。OA 卡片文本若含 instanceId 行请直接传入（优先）",
            },
            "approval_title": {
                "type": "string",
                "description": "审批标题（无 instance_id 时用标题在待办中反查实例，"
                               "即 [OA审批] 后面的标题文本）",
            },
            "remark": {
                "type": "string",
                "description": "转交说明（可选，如：原审批人已离职，转交接手人处理）",
            },
        },
        "required": ["target_name"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        target_name = (args.get("target_name") or "").strip()
        instance_id = (args.get("instance_id") or "").strip()
        approval_title = (args.get("approval_title") or "").strip()
        remark = (args.get("remark") or "").strip()

        if not target_name:
            return {"error": "target_name 不能为空（需要转交给谁？）"}
        if not instance_id and not approval_title:
            return {"error": "instance_id 与 approval_title 至少提供一个，"
                             "否则无法定位要转交的审批"}

        # 延迟导入避免工具模块加载即拉起 approval 包
        from src.approval.dingtalk import DingTalkApprovalProvider
        from src.approval.service import ApprovalTransferService

        service = ApprovalTransferService(DingTalkApprovalProvider(self.dws))
        result = service.transfer(
            target_name=target_name, instance_id=instance_id,
            title_query=approval_title, remark=remark)

        payload = result.to_dict()
        if not result.success:
            # base.ToolRouter 约定：dict 含非空 error 即失败，LLM 会向用户解释原因
            payload["error"] = result.message
        return payload

    def build_confirmation_preview(self, args: dict) -> str:
        """生成「即将转交的审批」预览（只读预检，绝不执行写操作）。

        尽量还原真实场景：定位实例 → 取详情 → 列可转交任务 → 校验目标人唯一性，
        形成「将把《X》的 N 个任务转给 yy（唯一匹配）」的精确预览。任何异常均
        吞掉并回退到通用文案，确保确认流程不被预览失败阻塞。
        """
        target_name = (args.get("target_name") or "").strip()
        instance_id = (args.get("instance_id") or "").strip()
        title = (args.get("approval_title") or "").strip()
        remark = (args.get("remark") or "").strip()
        try:
            from src.approval.dingtalk import DingTalkApprovalProvider
            from src.approval.service import ApprovalTransferService
            svc = ApprovalTransferService(DingTalkApprovalProvider(self.dws))
            inst = instance_id or (svc.provider.find_instance_id(title) if title else "")
            detail = svc.provider.get_detail(inst) if inst else None
            tasks = svc.provider.list_transferable_tasks(inst) if inst else []
            target = svc._resolve_target(target_name) if inst else None
            who = (f"{target.name}（{target.title}）" if (target and target.title)
                   else (target.name if target else target_name))
            if detail is not None and tasks:
                base = (f"即将把审批「{detail.title or inst}」的 {len(tasks)} 个待处理任务"
                        f"转交给 {who}（唯一匹配）")
            elif detail is not None:
                base = f"即将把审批「{detail.title or inst}」转交给 {who}（当前无待我处理的任务，执行时将如实反馈）"
            else:
                base = f"即将把审批（实例 {inst or title or '未知'}）转交给 {who}"
        except Exception as _exc:
            logger.debug("build_confirmation_preview: swallowed exception: %s", _exc)
            base = f"即将把 OA 审批转交给「{target_name}」"
        if remark:
            base += f"；转交说明：{remark}"
        return base + "。请确认后回复「确认」以执行。"


class GetAttendanceTool(BaseTool):
    name = "get_attendance"
    display_name = "查询我的考勤打卡"
    short_description = "查询当前用户指定日期范围的考勤打卡结果（上班/下班打卡时间、是否正常/迟到/缺卡），便于回顾出勤情况"
    description = "查询当前用户的考勤打卡记录"
    # 场景关键词统一维护在 IntentRegistry 的 domain.attendance（单一真源）
    intent_categories = ["domain.attendance"]
    # 钉钉考勤专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "起始日期 YYYY-MM-DD，默认本月 1 号",
            },
            "end": {
                "type": "string",
                "description": "结束日期 YYYY-MM-DD，默认今天",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 50",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        today = date.today()
        start = (args.get("start") or "").strip() or today.replace(day=1).isoformat()
        end = (args.get("end") or "").strip() or today.isoformat()
        limit = args.get("limit") or 50
        try:
            limit = int(limit)
        except (TypeError, ValueError) as _exc:
            logger.debug("execute: limit 解析失败，回退默认值: %s", _exc)
            limit = 50

        # 考勤命令需要显式 userId：先取当前用户
        try:
            me = self.dws.contact_user_get_self()
        except Exception as e:
            return {"error": f"获取当前用户失败: {e}"}
        if not isinstance(me, dict) or not me:
            return {"error": "无法获取当前用户（个人钉钉模式或未开通 CLI 权限），无法查询考勤"}
        emp = me.get("orgEmployeeModel", me)
        user_id = emp.get("userId") or emp.get("staffId") or me.get("userId") or ""
        if not user_id:
            return {"error": "当前用户缺少可识别的 userId，无法查询考勤"}

        try:
            data = self.dws.run([
                "attendance", "check", "result",
                "--users", user_id,
                "--start", start,
                "--end", end,
                "--limit", str(limit),
            ])
        except Exception as e:
            return {"error": f"获取考勤打卡记录失败: {e}"}
        return _to_items(_unwrap(data), limit=limit)


class SendDingTool(BaseTool):
    name = "send_ding"
    display_name = "发送 DING 强提醒"
    short_description = "通过 DING 向指定用户发送强提醒消息（应用内/短信/电话），适用于免打扰也要送达的重要通知"
    description = "向指定用户发送 DING 强提醒消息"
    # 场景关键词统一维护在 IntentRegistry 的 domain.ding（单一真源）
    intent_categories = ["domain.ding"]
    # DING 是钉钉专属功能
    platforms = ["dingtalk"]
    # 默认不拦截：app 应用内提醒直接放行；仅短信/电话这类真实触达手机、产生费用
    # 或强打扰的方式需要二次确认（见 needs_confirm）。
    require_confirm = False
    parameters = {
        "type": "object",
        "properties": {
            "users": {
                "type": "string",
                "description": "接收人 userId，多个用逗号分隔，如 u1,u2",
            },
            "content": {
                "type": "string",
                "description": "DING 消息内容",
            },
            "type": {
                "type": "string",
                "enum": ["app", "sms", "call"],
                "description": "提醒方式：app=应用内(默认)，sms=短信，call=电话",
            },
            "robot_code": {
                "type": "string",
                "description": "DING 机器人 code（可选）；不填则使用环境变量 DINGTALK_DING_ROBOT_CODE",
            },
        },
        "required": ["users", "content"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        users = (args.get("users") or "").strip()
        content = (args.get("content") or "").strip()
        ding_type = (args.get("type") or "app").strip() or "app"
        robot_code = (args.get("robot_code") or "").strip() or os.environ.get("DINGTALK_DING_ROBOT_CODE", "")

        if not users:
            return {"error": "users 不能为空（接收人 userId，多个逗号分隔）"}
        if not content:
            return {"error": "content 不能为空"}
        # 【提示词泄漏防线】DING 内容由 LLM 生成且直达他人，与 send_message 同属
        # 绕过 enforce_brevity 的工具直发路径，发送前统一清洗。
        try:
            from src.llm.style import sanitize_reply
            cleaned = sanitize_reply(content)
            if cleaned != content:
                logger.warning(
                    "[sanitize send_ding] DING 内容含提示词泄漏痕迹，已清洗: %d -> %d 字符",
                    len(content), len(cleaned),
                )
                if not cleaned:
                    return {"error": "DING 内容全部为内部推理痕迹，已拦截不发送"}
                content = cleaned
        except Exception:
            logger.warning("[resilience] send_ding 清洗失败，按原文发送", exc_info=True)
        if ding_type not in ("app", "sms", "call"):
            return {"error": "type 必须是 app / sms / call 之一"}
        if not robot_code:
            return {"error": "缺少 DING 机器人 code：请传 robot_code 或配置环境变量 DINGTALK_DING_ROBOT_CODE"}

        cmd = [
            "ding", "message", "send",
            "--robot-code", robot_code,
            "--users", users,
            "--content", content,
            "--type", ding_type,
        ]
        try:
            data = self.dws.run(cmd)
        except Exception as e:
            logger.exception("发送 DING 失败: %s", e)
            return {"error": f"发送 DING 失败: {e}"}
        return _to_items(_unwrap(data), limit=20)

    def needs_confirm(self, args: dict) -> bool:
        """仅当提醒方式为短信/电话（sms/call）这类会真实触达手机、产生费用或强打扰的
        强提醒时才要求二次确认；应用内(app)提醒直接放行，避免无谓的确认摩擦。

        路由层以本方法返回值为准（而非类级 require_confirm），实现「按参数条件确认」。
        """
        return (args.get("type") or "app").strip() in ("sms", "call")

    def build_confirmation_preview(self, args: dict) -> str:
        """只读预检，生成「即将发送 DING 强提醒」预览（绝不执行发送）。

        吞掉一切异常，失败回退通用文案，确保确认流程不被预览阻塞。
        """
        try:
            users = (args.get("users") or "").strip()
            ding_type = (args.get("type") or "app").strip() or "app"
            content = (args.get("content") or "").strip()
            type_label = {"sms": "短信", "call": "电话", "app": "应用内"}.get(ding_type, ding_type)
            preview = f"即将通过【{type_label}】向 {users or '指定接收人'} 发送 DING 强提醒"
            if content:
                preview += f"：{content}"
            return preview + "。请确认后回复「确认」以执行。"
        except Exception as _exc:
            logger.debug("build_confirmation_preview: swallowed exception: %s", _exc)
            return "即将发送 DING 强提醒，请确认。"
