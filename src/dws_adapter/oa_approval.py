"""DwsAdapter OA 审批只读查询 mixin。拆分自 dws_adapter.py。"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class DwsAdapterOaApprovalMixin:
    def oa_approval_list_forms(self, cursor: str = "0", limit: int = 100) -> list:
        """列出当前用户可见的审批表单模板。

        用于 OA 表单批量盘点（如统计含「项目」字段的 25 个表单）。
        真实 API 返回 {"processCodeList":[{dirName,processCode,processName}], "totalCount":-1}，
        本方法归一化为 list（兼容 mock 直接返回 list 的形态）。失败返回空 list。
        """
        try:
            data = self.run(
                ["oa", "approval", "list-forms", "--cursor", str(cursor), "--limit", str(limit)],
                operation="oa_approval_list_forms", force_no_dry_run=True,
            )
            result = self._get_result(data)
            if isinstance(result, dict):
                forms = result.get("processCodeList") or []
                return forms if isinstance(forms, list) else []
            if isinstance(result, list):
                return result
            return []
        except Exception as e:
            logger.warning("[DWS] 列出审批表单失败: %s", e)
            return []

    def oa_approval_search_forms(self, query: str) -> list:
        """按关键字模糊搜索审批表单（匹配 processCode 或表单名称）。"""
        try:
            data = self.run(
                ["oa", "approval", "search-forms", "--query", query],
                operation="oa_approval_search_forms", force_no_dry_run=True,
            )
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 搜索审批表单失败: %s", e)
            return []

    def oa_approval_detail(self, instance_id: str) -> dict | None:
        """获取审批实例详情（含表单字段与填写值）。失败返回 None。"""
        try:
            data = self.run(
                ["oa", "approval", "detail", "--instance-id", instance_id],
                operation="oa_approval_detail", force_no_dry_run=True,
            )
            result = self._get_result(data)
            return result if isinstance(result, dict) else None
        except Exception as e:
            logger.warning("[DWS] 获取审批详情失败: %s", e)
            return None

    def oa_approval_list_pending(self, start: str, end: str,
                                 query: str | None = None,
                                 page: int = 1, limit: int = 50) -> list:
        """查询待我处理的审批。start/end 为 ISO-8601（如 2026-03-10T00:00:00+08:00）。"""
        args = ["oa", "approval", "list-pending", "--start", start, "--end", end,
                "--page", str(page), "--limit", str(limit)]
        if query:
            args += ["--query", query]
        try:
            data = self.run(args, operation="oa_approval_list_pending", force_no_dry_run=True)
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 查询待审批列表失败: %s", e)
            return []

    def oa_approval_tasks(self, instance_id: str) -> list:
        """查询某审批实例下待我审批的任务 ID。失败返回空 list。"""
        try:
            data = self.run(
                ["oa", "approval", "tasks", "--instance-id", instance_id],
                operation="oa_approval_tasks", force_no_dry_run=True,
            )
            result = self._get_result(data)
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.warning("[DWS] 查询审批任务失败: %s", e)
            return []

    def oa_approval_list_initiated(self, process_code: str, start: str, end: str,
                                   cursor: str = "0", limit: int = 20) -> dict:
        """查询某审批模板下已发起的审批记录（按时间窗）。失败抛异常（不再吞掉，
        避免上层把错误当成功）。

        返回 DingTalk 原始 result dict（含 ``processInstanceList`` / ``hasMore`` /
        ``nextCursor``），由上层工具负责取数与自动翻页。
        start/end 为 ISO-8601（如 2026-03-10T00:00:00+08:00），且 DingTalk API 对
        时间跨度有上限，调用方需传入合理窗口。单页 limit 硬上限 20，超限会
        success=false。
        """
        data = self.run(
            ["oa", "approval", "list-initiated", "--process-code", process_code,
             "--start", start, "--end", end, "--cursor", str(cursor), "--limit", str(limit)],
            operation="oa_approval_list_initiated", force_no_dry_run=True,
        )
        result = self._get_result(data)
        return result if isinstance(result, dict) else {"processInstanceList": []}

    def oa_approval_list_executed(self, page: int = 1, limit: int = 20,
                                  query: str | None = None) -> dict:
        """查询当前用户「已处理」的审批单列表（审批人视角）。失败抛异常。

        钉钉接口 ``oa/list_executed`` 不支持时间参数，只有 ``--limit/--page/--query``，
        故时间窗过滤由上层工具在客户端完成。单页 limit 硬上限 20，超限会返回空。
        返回 DingTalk 原始 result dict（含 ``values`` / ``hasMore``），由上层工具负责
        取数、自动翻页与时间窗过滤。
        """
        args = ["oa", "approval", "list-executed",
                "--page", str(page), "--limit", str(min(limit, 20))]
        if query:
            args += ["--query", query]
        last_exc: Exception | None = None
        # get_done_tasks 偶发返回 success=false（business_error），实测重试即可恢复；
        # 这里做调用级轻量重试，避免把瞬态错误直接抛给上层工具导致整次查询崩溃。
        for _ in range(3):
            try:
                data = self.run(args, operation="oa_approval_list_executed", force_no_dry_run=True)
                break
            except Exception as e:  # noqa: BLE001 - 瞬态错误重试
                last_exc = e
                time.sleep(1.0)
        else:
            if last_exc is not None:
                raise last_exc
        result = self._get_result(data)
        return result if isinstance(result, dict) else {"values": []}

    def oa_approval_redirect_task(self, *, task_id: str, to_actioner_id: str,
                                  remark: str = "") -> dict:
        """转交审批任务给其他人（写操作）。

        封装 `dws oa approval redirect-task --task-id --to-actioner-id [--remark]`。
        与其他写操作（chat_message_send / todo_task_create）一致：
        - 尊重全局 dry_run 配置（不加 force_no_dry_run），干跑模式下仅预览；
        - 失败向上抛异常，由调用方（DingTalkApprovalProvider.transfer_task）
          捕获并转成真实失败回执，保证结果可反馈发起人。
        """
        args = ["oa", "approval", "redirect-task",
                "--task-id", str(task_id),
                "--to-actioner-id", str(to_actioner_id)]
        if remark:
            args.extend(["--remark", remark])
        return self.run(args, operation="oa_approval_redirect_task")
