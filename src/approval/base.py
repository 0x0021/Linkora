"""审批流转交 · 平台能力协议（通用层）。

各 IM 平台（钉钉/飞书/企微）实现本协议后即可复用 ApprovalTransferService
的完整编排逻辑（解析 → 校验 → 执行 → 结果回执）。

约定：
- 查询类方法失败返回空值（None / []），不抛异常；
- transfer_task 返回 (成功与否, 平台真实回执说明)，实现内部必须吞掉异常
  并转成 (False, 原因)，保证 service 层拿到的是「真实执行结果」而非崩栈。
"""
from __future__ import annotations

import abc

from src.approval.models import ApprovalDetail, ApprovalNode, TransferTarget


class ApprovalProvider(abc.ABC):
    """平台审批能力提供者协议。"""

    #: 平台标识（dingtalk / feishu / wecom），写入 TransferResult.platform
    platform: str = ""

    @abc.abstractmethod
    def get_detail(self, instance_id: str) -> ApprovalDetail | None:
        """获取审批实例详情（解析后的关键字段）。失败/不存在返回 None。"""

    @abc.abstractmethod
    def find_instance_id(self, title_query: str) -> str:
        """按审批标题反查实例 ID（转发卡片拿不到 ID 时的兜底）。找不到返回空串。"""

    @abc.abstractmethod
    def resolve_user(self, name: str) -> list[TransferTarget]:
        """按姓名在通讯录中解析目标人员，返回候选列表（可能 0/1/多个）。"""

    @abc.abstractmethod
    def list_transferable_tasks(self, instance_id: str) -> list[ApprovalNode]:
        """列出该实例下当前用户可转交的审批任务。无任务返回 []。"""

    @abc.abstractmethod
    def transfer_task(self, task_id: str, target: TransferTarget,
                      remark: str = "") -> tuple[bool, str]:
        """执行单个任务转交，返回 (是否成功, 平台真实回执说明)。不抛异常。"""
