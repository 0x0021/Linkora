"""审批流转交能力（通用层 + 平台专有实现）。

分层：
- models.py   通用数据模型与异常体系（平台无关）
- base.py     ApprovalProvider 抽象（平台能力协议）
- service.py  ApprovalTransferService 编排（解析 → 校验 → 执行 → 结果）
- dingtalk.py 钉钉专有实现（基于 dws CLI）

新增平台（飞书/企微）只需实现 ApprovalProvider，service 层零改动。
"""
from src.approval.models import (
    ApprovalDetail,
    ApprovalNode,
    ApprovalNotFoundError,
    ApprovalTransferError,
    NoTransferableTaskError,
    TargetUserInvalidError,
    TransferExecutionError,
    TransferResult,
    TransferTarget,
)
from src.approval.base import ApprovalProvider
from src.approval.service import ApprovalTransferService

__all__ = [
    "ApprovalDetail",
    "ApprovalNode",
    "ApprovalNotFoundError",
    "ApprovalProvider",
    "ApprovalTransferError",
    "ApprovalTransferService",
    "NoTransferableTaskError",
    "TargetUserInvalidError",
    "TransferExecutionError",
    "TransferResult",
    "TransferTarget",
]
