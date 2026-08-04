"""审批流转交 · 通用数据模型与异常体系（平台无关）。

所有面向用户的失败原因统一走 ApprovalTransferError.reason（中文），
service 层捕获后转成带时间戳的 TransferResult，保证「真实执行结果」可回传发起人。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


def now_iso() -> str:
    """本地时区 ISO-8601 时间戳（秒级），用于转交结果回执。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ============================ 异常体系 ============================

class ApprovalTransferError(Exception):
    """审批转交异常基类。reason 为面向用户的中文原因。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ApprovalNotFoundError(ApprovalTransferError):
    """找不到审批实例（instanceId 无效 / 标题反查无结果）。"""


class TargetUserInvalidError(ApprovalTransferError):
    """转交目标人员校验失败（不存在 / 同名多人无法唯一确定）。"""

    def __init__(self, reason: str, candidates: list["TransferTarget"] | None = None):
        super().__init__(reason)
        self.candidates = candidates or []


class NoTransferableTaskError(ApprovalTransferError):
    """审批实例下没有可由当前用户转交的任务。"""


class TransferExecutionError(ApprovalTransferError):
    """转交执行阶段失败（平台 API 拒绝 / 网络错误等）。"""


# ============================ 数据模型 ============================

@dataclass
class ApprovalNode:
    """审批节点/任务（当前停留节点用）。"""

    name: str = ""            # 节点名（如「部门主管审批」）
    status: str = ""          # RUNNING / NEW / COMPLETED ...
    approver_id: str = ""     # 当前审批人 userId
    approver_name: str = ""   # 当前审批人姓名（若可得）
    task_id: str = ""         # 平台任务 ID（转交必需）


@dataclass
class ApprovalDetail:
    """审批实例详情（解析后的关键字段）。"""

    instance_id: str
    title: str = ""            # 审批标题
    initiator_id: str = ""     # 发起人 userId
    initiator_name: str = ""   # 发起人姓名
    status: str = ""           # 实例状态（RUNNING / COMPLETED ...）
    form_fields: list[dict] = field(default_factory=list)   # [{key, value}]
    current_nodes: list[ApprovalNode] = field(default_factory=list)
    remark: str = ""           # 审批备注/最新评论（若可得）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TransferTarget:
    """转交目标人员（校验通过后的确认信息）。"""

    user_id: str
    name: str = ""
    title: str = ""   # 职位（用于同名区分与结果确认）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TransferResult:
    """转交操作结果回执（带时间戳，反馈给发起人）。"""

    status: str                # "success" / "failed"
    platform: str = ""         # 平台标识（dingtalk / feishu / wecom）
    instance_id: str = ""
    approval_title: str = ""
    task_ids: list[str] = field(default_factory=list)   # 实际被转交的任务 ID
    target: TransferTarget | None = None                # 目标人员确认信息
    message: str = ""          # 成功说明或失败原因（面向用户中文）
    timestamp: str = field(default_factory=now_iso)     # 操作完成时刻

    @property
    def success(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["success"] = self.success
        return d
