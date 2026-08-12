"""审批流转交 · 钉钉专有实现（基于 dws CLI）。

底层命令链：
- 详情       dws oa approval detail --instance-id <id>
- 标题反查   dws oa approval list-pending --query <title>
- 通讯录校验 dws contact user search --query <name>
- 可转交任务 dws oa approval tasks --instance-id <id>（待我审批的任务）
- 执行转交   dws oa approval redirect-task --task-id <tid> --to-actioner-id <uid> [--remark]

钉钉字段解析做多 key 兼容（驼峰/下划线），避免 API 版本差异导致解析失败。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.approval.base import ApprovalProvider
from src.approval.models import ApprovalDetail, ApprovalNode, TransferTarget

logger = logging.getLogger(__name__)

# 标题反查实例的回溯窗口（天）：钉钉 list-pending 需要显式时间窗
_PENDING_LOOKBACK_DAYS = 60

# 视为「进行中（可转交）」的任务状态
_ACTIVE_TASK_STATUSES = {"RUNNING", "NEW", "PAUSED", ""}


def _pick(d: dict, *keys, default: str | list = ""):
    """多 key 兼容取值（钉钉 API 驼峰/下划线混用）。"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


class DingTalkApprovalProvider(ApprovalProvider):
    """钉钉审批能力提供者（包装 DwsAdapter）。"""

    platform = "dingtalk"

    def __init__(self, dws):
        self.dws = dws

    # ------------------------------------------------------------------
    # 查询类
    # ------------------------------------------------------------------

    def get_detail(self, instance_id: str) -> ApprovalDetail | None:
        raw = self.dws.oa_approval_detail(instance_id)
        if not isinstance(raw, dict):
            return None
        return self._parse_detail(instance_id, raw)

    def _parse_detail(self, instance_id: str, raw: dict) -> ApprovalDetail:
        """解析钉钉审批实例详情为通用模型（关键字段多 key 兼容）。"""
        forms = _pick(raw, "formComponentValues", "form_component_values",
                      default=[]) or []
        form_fields = [
            {"key": _pick(f, "name", "key", "componentName"),
             "value": _pick(f, "value", "componentValue")}
            for f in forms if isinstance(f, dict)
        ]

        nodes: list[ApprovalNode] = []
        for t in (_pick(raw, "tasks", default=[]) or []):
            if not isinstance(t, dict):
                continue
            status = str(_pick(t, "status", "taskStatus")).upper()
            if status not in _ACTIVE_TASK_STATUSES:
                continue
            nodes.append(ApprovalNode(
                name=str(_pick(t, "activityName", "activity_name", "name")),
                status=status,
                approver_id=str(_pick(t, "userId", "userid", "user_id")),
                task_id=str(_pick(t, "taskId", "taskid", "task_id")),
            ))

        # 审批备注：取操作记录里最新的非空 remark
        remark = ""
        for rec in reversed(_pick(raw, "operationRecords", "operation_records",
                                  default=[]) or []):
            if isinstance(rec, dict) and _pick(rec, "remark"):
                remark = str(_pick(rec, "remark"))
                break

        return ApprovalDetail(
            instance_id=instance_id,
            title=str(_pick(raw, "title")),
            initiator_id=str(_pick(raw, "originatorUserId", "originator_user_id")),
            initiator_name=str(_pick(raw, "originatorDisplayName",
                                     "originatorUserName", "originator_display_name")),
            status=str(_pick(raw, "status")),
            form_fields=form_fields,
            current_nodes=nodes,
            remark=remark,
        )

    def find_instance_id(self, title_query: str) -> str:
        """按标题在「待我处理」列表中反查实例 ID（找不到返回空串）。"""
        end = datetime.now().astimezone()
        start = end - timedelta(days=_PENDING_LOOKBACK_DAYS)
        try:
            items = self.dws.oa_approval_list_pending(
                start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"),
                query=title_query)
        except Exception as e:  # noqa: BLE001
            logger.warning("[钉钉审批] 标题反查失败: %s", e)
            return ""
        for it in items or []:
            if not isinstance(it, dict):
                continue
            inst = str(_pick(it, "processInstanceId", "process_instance_id",
                             "instanceId", "procInstId"))
            if inst:
                return inst
        return ""

    def resolve_user(self, name: str) -> list[TransferTarget]:
        try:
            users = self.dws.contact_user_search(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("[钉钉审批] 通讯录搜索失败: %s", e)
            return []
        targets = []
        for u in users or []:
            if not isinstance(u, dict):
                continue
            targets.append(TransferTarget(
                user_id=str(_pick(u, "userId", "userid", "user_id")),
                name=str(_pick(u, "name")),
                title=str(_pick(u, "title")),
            ))
        return targets

    def list_transferable_tasks(self, instance_id: str) -> list[ApprovalNode]:
        """待我审批的任务（dws oa approval tasks 已按当前用户过滤）。"""
        try:
            tasks = self.dws.oa_approval_tasks(instance_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("[钉钉审批] 查询审批任务失败: %s", e)
            return []
        nodes = []
        for t in tasks or []:
            if isinstance(t, dict):
                status = str(_pick(t, "status", "taskStatus")).upper()
                if status not in _ACTIVE_TASK_STATUSES:
                    continue
                nodes.append(ApprovalNode(
                    name=str(_pick(t, "activityName", "name")),
                    status=status,
                    task_id=str(_pick(t, "taskId", "taskid", "task_id")),
                ))
            elif isinstance(t, (str, int)):
                # 容错：极简形态直接返回任务 ID 列表
                nodes.append(ApprovalNode(task_id=str(t)))
        return nodes

    # ------------------------------------------------------------------
    # 执行类
    # ------------------------------------------------------------------

    def transfer_task(self, task_id: str, target: TransferTarget,
                      remark: str = "") -> tuple[bool, str]:
        """执行转交并解读钉钉真实回执。不抛异常。"""
        try:
            data = self.dws.oa_approval_redirect_task(
                task_id=task_id, to_actioner_id=target.user_id, remark=remark)
        except Exception as e:  # noqa: BLE001
            return False, f"钉钉转交接口调用失败：{e}"

        if not isinstance(data, dict):
            return False, f"钉钉返回了无法解析的结果：{data!r}"

        # dry-run 预览（全局 dry_run 配置开启时不真实执行，如实告知）
        if data.get("dryRun") or data.get("dry_run"):
            return True, "dry-run 预览：命令未真实执行（全局干跑模式开启）"

        if data.get("success") is False:
            reason = data.get("error") or data.get("message") or "未知原因"
            return False, f"钉钉拒绝转交：{reason}"

        result = data.get("result")
        if isinstance(result, dict) and result.get("result") is False:
            reason = result.get("message") or "平台校验未通过"
            return False, f"钉钉拒绝转交：{reason}"

        return True, "钉钉已确认转交成功"
