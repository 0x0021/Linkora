"""审批流转交 · 编排服务（通用层，平台无关）。

完整链路：定位实例 → 解析详情 → 校验目标人 → 列可转交任务 → 逐个执行转交
→ 汇总为带时间戳的 TransferResult（真实执行结果，供反馈发起人）。

异常策略：内部所有已知失败都转成 ApprovalTransferError 子类，最终统一
收敛为 status=failed 的 TransferResult；未知异常兜底捕获，绝不向调用方崩栈。
"""
from __future__ import annotations

import logging

from src.approval.base import ApprovalProvider
from src.audit import audit
from src.approval.models import (
    ApprovalDetail,
    ApprovalNotFoundError,
    ApprovalTransferError,
    NoTransferableTaskError,
    TargetUserInvalidError,
    TransferResult,
    TransferTarget,
)

logger = logging.getLogger(__name__)

# 候选人展示上限（同名多人提示时避免刷屏）
_MAX_CANDIDATES_SHOWN = 5


class ApprovalTransferService:
    """审批转交编排服务。持有一个平台 Provider，本身不含平台代码。"""

    def __init__(self, provider: ApprovalProvider):
        self.provider = provider

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    def transfer(self, *, target_name: str, instance_id: str = "",
                 title_query: str = "", remark: str = "") -> TransferResult:
        """执行审批转交，返回带时间戳的真实结果（永不抛异常）。

        Args:
            target_name: 转交目标人姓名（yy），将在通讯录校验唯一性。
            instance_id: 审批实例 ID（优先使用）。
            title_query: 审批标题（instance_id 缺失时反查实例）。
            remark: 转交说明（如「原审批人已离职」）。
        """
        try:
            result = self._transfer_impl(
                target_name=target_name, instance_id=instance_id,
                title_query=title_query, remark=remark)
            audit("approval_transfer", "transfer_approval", result.status,
                  actor=target_name, target=(instance_id or title_query),
                  detail=f"platform={result.platform} tasks={result.task_ids}")
            return result
        except ApprovalTransferError as e:
            logger.warning("[审批转交] 失败: %s", e.reason)
            audit("approval_transfer", "transfer_approval", "failed",
                  actor=target_name, target=(instance_id or title_query),
                  detail=e.reason)
            return TransferResult(
                status="failed", platform=self.provider.platform,
                instance_id=instance_id, approval_title=title_query,
                message=e.reason)
        except Exception as e:  # noqa: BLE001 - 兜底：绝不向调用方崩栈
            logger.exception("[审批转交] 未预期异常: %s", e)
            audit("approval_transfer", "transfer_approval", "error",
                  actor=target_name, target=(instance_id or title_query),
                  detail=f"内部错误：{e}")
            return TransferResult(
                status="failed", platform=self.provider.platform,
                instance_id=instance_id, approval_title=title_query,
                message=f"内部错误：{e}")

    # ------------------------------------------------------------------
    # 编排实现
    # ------------------------------------------------------------------

    def _transfer_impl(self, *, target_name: str, instance_id: str,
                       title_query: str, remark: str) -> TransferResult:
        target_name = (target_name or "").strip()
        if not target_name:
            raise TargetUserInvalidError("未提供转交目标人姓名")

        # 1) 定位审批实例
        inst_id = (instance_id or "").strip()
        if not inst_id:
            if not (title_query or "").strip():
                raise ApprovalNotFoundError(
                    "缺少审批实例 ID 或审批标题，无法定位要转交的审批")
            inst_id = self.provider.find_instance_id(title_query.strip())
            if not inst_id:
                raise ApprovalNotFoundError(
                    f"按标题「{title_query.strip()}」未找到待处理的审批实例")

        # 2) 解析详情（标题/发起人/当前节点/表单字段）
        detail = self.provider.get_detail(inst_id)
        if detail is None:
            raise ApprovalNotFoundError(f"审批实例 {inst_id} 详情获取失败或不存在")

        # 3) 校验目标人 yy 有效性（通讯录唯一解析）
        target = self._resolve_target(target_name)

        # 4) 列出可转交任务
        tasks = self.provider.list_transferable_tasks(inst_id)
        task_ids = [t.task_id for t in tasks if t.task_id]
        if not task_ids:
            raise NoTransferableTaskError(
                f"审批「{detail.title or inst_id}」当前没有可由我转交的任务"
                "（可能已审批完成或任务不在我名下）")

        # 5) 逐个执行转交，收集平台真实回执
        done_ids: list[str] = []
        for task_id in task_ids:
            ok, receipt = self.provider.transfer_task(task_id, target, remark)
            if not ok:
                # 已成功部分如实上报，避免误导「全部失败」
                prefix = (f"任务 {done_ids} 已转交成功，" if done_ids else "")
                return TransferResult(
                    status="failed", platform=self.provider.platform,
                    instance_id=inst_id, approval_title=detail.title,
                    task_ids=done_ids, target=target,
                    message=f"{prefix}任务 {task_id} 转交失败：{receipt}")
            done_ids.append(task_id)

        # 6) 汇总成功结果（含目标人确认信息 + 时间戳）
        who = target.name + (f"（{target.title}）" if target.title else "")
        return TransferResult(
            status="success", platform=self.provider.platform,
            instance_id=inst_id, approval_title=detail.title,
            task_ids=done_ids, target=target,
            message=f"已将审批「{detail.title or inst_id}」的 {len(done_ids)} 个任务"
                    f"转交给 {who}")

    # ------------------------------------------------------------------
    # 目标人校验
    # ------------------------------------------------------------------

    def _resolve_target(self, name: str) -> TransferTarget:
        """通讯录解析 + 唯一性校验。0 个/无法唯一确定 → TargetUserInvalidError。"""
        candidates = self.provider.resolve_user(name)
        candidates = [c for c in candidates if c.user_id]
        if not candidates:
            raise TargetUserInvalidError(
                f"通讯录中未找到「{name}」，请确认姓名是否正确")

        # 精确同名优先：搜索可能命中拼音/模糊，先按 name 完全一致收窄
        exact = [c for c in candidates if c.name == name]
        pool = exact or candidates
        if len(pool) == 1:
            return pool[0]

        shown = ", ".join(
            f"{c.name}({c.title or c.user_id})" for c in pool[:_MAX_CANDIDATES_SHOWN])
        raise TargetUserInvalidError(
            f"「{name}」在通讯录中匹配到 {len(pool)} 人，无法唯一确定，"
            f"候选：{shown}。请补充职位或全名后重试", candidates=pool)


def get_detail_or_none(provider: ApprovalProvider,
                       instance_id: str) -> ApprovalDetail | None:
    """便捷方法：安全获取审批详情（供只读场景复用，不抛异常）。"""
    try:
        return provider.get_detail(instance_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[审批转交] 获取详情异常: %s", e)
        return None
