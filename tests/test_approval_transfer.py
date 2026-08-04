"""审批流转交（通用服务层 + 钉钉 Provider + LLM 工具）单元测试。

覆盖需求：
- 解析审批流关键字段（标题/发起人/当前节点/备注）
- 目标人 yy 有效性校验（不存在 / 同名多人无法唯一确定）
- 转交执行与真实结果回执（成功 / 失败 / 部分成功）
- 异常捕获与错误提示（绝不向调用方崩栈）
- 结果含转交状态、目标人确认信息、时间戳
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.approval.base import ApprovalProvider
from src.approval.models import (
    ApprovalDetail,
    ApprovalNode,
    ApprovalNotFoundError,
    NoTransferableTaskError,
    TargetUserInvalidError,
    TransferResult,
    TransferTarget,
)
from src.approval.service import ApprovalTransferService
from src.approval.dingtalk import DingTalkApprovalProvider
from src.tools.business import TransferApprovalTool


# =====================================================================
# 内存版 Provider 桩（驱动服务层编排测试，零外部依赖）
# =====================================================================

class _StubProvider(ApprovalProvider):
    """可配置的 ApprovalProvider 桩，按用例注入行为。"""

    platform = "stub"

    def __init__(self, *, detail=None, find_id="", users=None, tasks=None,
                 transfer_cb=None):
        self._detail = detail
        self._find_id = find_id
        self._users = users if users is not None else []
        self._tasks = tasks if tasks is not None else []
        self._transfer_cb = transfer_cb or (lambda task_id, target, remark: (True, "ok"))
        self.calls = []

    def get_detail(self, instance_id: str):
        return self._detail

    def find_instance_id(self, title_query: str) -> str:
        return self._find_id

    def resolve_user(self, name: str):
        return list(self._users)

    def list_transferable_tasks(self, instance_id: str):
        return list(self._tasks)

    def transfer_task(self, task_id: str, target: TransferTarget, remark: str = ""):
        self.calls.append((task_id, target, remark))
        return self._transfer_cb(task_id, target, remark)


def _detail(title="报销审批", initiator="张三", inst="INST1"):
    return ApprovalDetail(
        instance_id=inst, title=title, initiator_id="uid-z",
        initiator_name=initiator, status="RUNNING",
        form_fields=[{"key": "金额", "value": "1000"}],
        current_nodes=[ApprovalNode(name="主管审批", status="RUNNING",
                                    approver_id="uid-x", task_id="T1")],
        remark="原审批人已离职",
    )


def _target(name="李四", uid="uid-li", title="财务"):
    return TransferTarget(user_id=uid, name=name, title=title)


# =====================================================================
# 服务层编排
# =====================================================================

def test_transfer_success():
    prov = _StubProvider(detail=_detail(), users=[_target()],
                         tasks=[ApprovalNode(task_id="T1", status="RUNNING")])
    svc = ApprovalTransferService(prov)
    r = svc.transfer(target_name="李四", instance_id="INST1")
    assert isinstance(r, TransferResult)
    assert r.success is True
    assert r.status == "success"
    assert r.task_ids == ["T1"]
    assert r.target is not None and r.target.user_id == "uid-li"
    assert "李四" in r.message and "报销审批" in r.message
    # 时间戳：ISO-8601 含日期与偏移
    assert "T" in r.timestamp and ("+" in r.timestamp or "Z" in r.timestamp)
    # 真实调用了转交执行
    assert prov.calls == [("T1", r.target, "")]


def test_transfer_target_not_found():
    prov = _StubProvider(detail=_detail(), users=[],
                         tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="不存在的人",
                                               instance_id="INST1")
    assert r.success is False
    assert "未找到" in r.message
    assert r.target is None


def test_transfer_multiple_candidates():
    # 同名两人无法唯一确定 → 失败并给出候选
    prov = _StubProvider(
        detail=_detail(),
        users=[_target(name="王五", uid="u1"), _target(name="王五", uid="u2")],
        tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="王五",
                                               instance_id="INST1")
    assert r.success is False
    assert "匹配到 2 人" in r.message
    assert isinstance(r.target, TransferTarget) or r.target is None


def test_transfer_no_transferable_task():
    prov = _StubProvider(detail=_detail(), users=[_target()], tasks=[])
    r = ApprovalTransferService(prov).transfer(target_name="李四",
                                               instance_id="INST1")
    assert r.success is False
    assert "没有可由我转交" in r.message


def test_transfer_partial_failure_reports_done():
    # 第一个成功、第二个失败：返回失败，但已成功的任务如实上报
    def cb(task_id, target, remark):
        return (True, "ok") if task_id == "T1" else (False, "钉钉拒绝：无权限")
    prov = _StubProvider(
        detail=_detail(), users=[_target()],
        tasks=[ApprovalNode(task_id="T1"), ApprovalNode(task_id="T2")],
        transfer_cb=cb)
    r = ApprovalTransferService(prov).transfer(target_name="李四",
                                               instance_id="INST1")
    assert r.success is False
    assert r.task_ids == ["T1"]            # 已成功的任务在回执里
    assert "T1" in r.message and "T2" in r.message and "无权限" in r.message


def test_transfer_missing_instance_and_title():
    prov = _StubProvider(detail=None, users=[_target()],
                         tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="李四")
    assert r.success is False
    assert isinstance(r, object) and "无法定位" in r.message


def test_transfer_find_by_title_when_no_instance():
    # 无 instance_id，靠标题反查
    prov = _StubProvider(detail=_detail(inst="INST-FOUND"), find_id="INST-FOUND",
                         users=[_target()],
                         tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="李四",
                                               title_query="报销审批")
    assert r.success is True
    assert r.instance_id == "INST-FOUND"


def test_transfer_exact_name_preferred_over_fuzzy():
    # 候选含精确同名 + 模糊命中，应取精确同名
    prov = _StubProvider(
        detail=_detail(),
        users=[_target(name="李四", uid="u-exact"),
               TransferTarget(user_id="u-fuzzy", name="李四海", title="")],
        tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="李四",
                                               instance_id="INST1")
    assert r.success is True
    assert r.target.user_id == "u-exact"


def test_transfer_unexpected_exception_is_caught():
    # get_detail 抛非 ApprovalTransferError 异常 → 兜底捕获为 failed，不崩栈
    class _BoomProvider(_StubProvider):
        def get_detail(self, instance_id: str):
            raise RuntimeError("db connection lost")
    prov = _BoomProvider(detail=_detail(), users=[_target()],
                         tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="李四",
                                               instance_id="INST1")
    assert r.success is False
    assert r.message.startswith("内部错误")


def test_resolve_target_empty_name_raises():
    prov = _StubProvider(detail=_detail(), users=[_target()],
                         tasks=[ApprovalNode(task_id="T1")])
    r = ApprovalTransferService(prov).transfer(target_name="   ")
    assert r.success is False
    assert "未提供转交目标人姓名" in r.message


# =====================================================================
# 钉钉 Provider 字段解析（需求：解析关键字段）
# =====================================================================

def _make_dws_for_detail():
    dws = MagicMock()
    dws.oa_approval_detail.return_value = {
        "title": "离职交接-权限回收",
        "originatorUserId": "uid-zhang",
        "originatorDisplayName": "张三",
        "status": "RUNNING",
        "formComponentValues": [
            {"name": "离职原因", "value": "个人发展"},
            {"name": "工号", "value": "E123"},
        ],
        "tasks": [
            {"activityName": "部门主管审批", "status": "RUNNING",
             "userId": "uid-x", "taskId": "T-A"},
            {"activityName": "HR审批", "status": "COMPLETED",
             "userId": "uid-hr", "taskId": "T-B"},  # 已完成节点应被过滤
        ],
        "operationRecords": [
            {"remark": ""},
            {"remark": "原审批人已离职，请转交"},
        ],
    }
    return dws


def test_dingtalk_parse_detail_extracts_fields():
    dws = _make_dws_for_detail()
    prov = DingTalkApprovalProvider(dws)
    detail = prov.get_detail("INST9")
    assert isinstance(detail, ApprovalDetail)
    assert detail.title == "离职交接-权限回收"
    assert detail.initiator_name == "张三"
    assert detail.initiator_id == "uid-zhang"
    assert detail.status == "RUNNING"
    assert {"key": "离职原因", "value": "个人发展"} in detail.form_fields
    # 仅 RUNNING 节点被保留，COMPLETED 被过滤
    assert [n.task_id for n in detail.current_nodes] == ["T-A"]
    assert detail.remark == "原审批人已离职，请转交"


def test_dingtalk_resolve_user_maps_to_target():
    dws = MagicMock()
    dws.contact_user_search.return_value = [
        {"userId": "uid-li", "name": "李四", "title": "财务主管"},
    ]
    prov = DingTalkApprovalProvider(dws)
    targets = prov.resolve_user("李四")
    assert len(targets) == 1
    assert targets[0].user_id == "uid-li"
    assert targets[0].title == "财务主管"


# =====================================================================
# LLM 工具层（TransferApprovalTool）
# =====================================================================

def _make_dws_for_tool_success():
    dws = MagicMock()
    dws.oa_approval_detail.return_value = {
        "title": "报销审批", "originatorUserId": "uid-z",
        "status": "RUNNING",
        "tasks": [{"activityName": "主管审批", "status": "RUNNING",
                   "taskId": "T1"}],
    }
    dws.contact_user_search.return_value = [
        {"userId": "uid-li", "name": "李四", "title": "财务"},
    ]
    dws.oa_approval_tasks.return_value = [
        {"taskId": "T1", "status": "RUNNING"},
    ]
    dws.oa_approval_redirect_task.return_value = {"success": True}
    return dws


def test_tool_missing_target_name():
    tool = TransferApprovalTool(MagicMock())
    out = tool.execute({})
    assert "error" in out
    assert "target_name" in out["error"]


def test_tool_missing_id_and_title():
    tool = TransferApprovalTool(MagicMock())
    out = tool.execute({"target_name": "李四"})
    assert "error" in out
    assert "instance_id" in out["error"] or "approval_title" in out["error"]


def test_tool_execute_success():
    dws = _make_dws_for_tool_success()
    tool = TransferApprovalTool(dws)
    out = tool.execute({
        "target_name": "李四",
        "instance_id": "INST1",
        "remark": "原审批人已离职",
    })
    assert out.get("success") is True
    assert "error" not in out
    assert out["task_ids"] == ["T1"]
    assert out["target"]["user_id"] == "uid-li"
    assert "T" in out["timestamp"]
    # 转交命令确实带上了 target 与 remark
    args, kwargs = dws.oa_approval_redirect_task.call_args
    assert kwargs["to_actioner_id"] == "uid-li"
    assert kwargs["remark"] == "原审批人已离职"


def test_tool_execute_failure_propagates_error():
    dws = _make_dws_for_tool_success()
    dws.oa_approval_redirect_task.return_value = {
        "success": False, "error": "目标人无接手权限"}
    tool = TransferApprovalTool(dws)
    out = tool.execute({"target_name": "李四", "instance_id": "INST1"})
    assert out.get("success") is False
    assert out.get("error")  # 失败原因回传给 LLM/发起人
    assert "无接手权限" in out["error"]


def test_tool_preview_faithful():
    """build_confirmation_preview 应只读预检，生成精确预览（含标题/任务数/目标人/说明）。"""
    from unittest.mock import patch
    detail = _detail(title="差旅报销-张三")
    target = _target(name="李四", title="财务部")
    tasks = [ApprovalNode(task_id="T1", status="RUNNING"),
             ApprovalNode(task_id="T2", status="RUNNING")]
    stub = _StubProvider(detail=detail, users=[target], tasks=tasks)
    tool = TransferApprovalTool(MagicMock())
    with patch("src.approval.dingtalk.DingTalkApprovalProvider", return_value=stub):
        preview = tool.build_confirmation_preview({
            "target_name": "李四", "instance_id": "INST1",
            "remark": "原审批人已离职"})
    assert "差旅报销-张三" in preview
    assert "2 个" in preview
    assert "李四" in preview and "财务部" in preview
    assert "原审批人已离职" in preview
    assert preview.endswith("请确认后回复「确认」以执行。")


def test_tool_preview_fallback_on_error():
    """预览预检异常时必须兜底为通用文案，绝不向外抛。"""
    from unittest.mock import patch

    def _boom(inst):
        raise RuntimeError("network down")

    boom = _StubProvider(detail=None, users=[], tasks=[])
    boom.get_detail = _boom
    tool = TransferApprovalTool(MagicMock())
    with patch("src.approval.dingtalk.DingTalkApprovalProvider", return_value=boom):
        preview = tool.build_confirmation_preview(
            {"target_name": "王五", "instance_id": "X"})
    assert preview.startswith("即将把 OA 审批转交给")
