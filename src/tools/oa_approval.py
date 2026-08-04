"""钉钉 OA 审批查询工具（domain.oa_approval）。

把 `src/dws_adapter.py` 里已实现但 0 引用的 `oa_approval_*` 只读方法接成 Agent 工具，
让数字分身在对话中能直接「列出/搜索审批表单模板、查待我审批、看审批详情、查已发起记录」，
支撑「有哪些审批表单 / 我待批的有哪些 / 这条审批进度到哪了」这类自动回复场景。

全部为只读操作，无需二次确认（require_confirm=False）。
写操作（转交审批 oa_approval_redirect_task）未接成本工具——它涉及授权变更，需独立的
require_confirm + sanitize_reply 防线，留待后续单独评估，不混入本次只读接线。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.dws_adapter import DwsAdapter
from src.tools.base import BaseTool
from src.tools.utils import _coerce_limit, list_result

logger = logging.getLogger(__name__)


class ApprovalListFormsTool(BaseTool):
    name = "approval_list_forms"
    display_name = "列出审批表单模板"
    short_description = "列出当前用户可见的钉钉审批表单模板（processCode/名称），用于审批表单盘点"
    description = "列出钉钉审批表单模板"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "cursor": {
                "type": "string",
                "description": "分页游标（上一页返回的 cursor），可选，默认 '0'",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 100",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        cursor = (args.get("cursor") or "0").strip() or "0"
        limit = _coerce_limit(args.get("limit"), 100)

        try:
            raw = self.dws.oa_approval_list_forms(cursor=cursor, limit=limit)
        except Exception as e:
            logger.exception("列出审批表单失败: %s", e)
            return {"error": f"列出审批表单失败: {e}"}

        return list_result(raw, limit)


class ApprovalSearchFormsTool(BaseTool):
    name = "approval_search_forms"
    display_name = "搜索审批表单"
    short_description = "按关键词模糊搜索钉钉审批表单模板（匹配 processCode 或表单名称）"
    description = "搜索钉钉审批表单模板"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词（必填）",
            },
        },
        "required": ["query"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query 不能为空"}

        try:
            raw = self.dws.oa_approval_search_forms(query=query)
        except Exception as e:
            logger.exception("搜索审批表单失败: %s", e)
            return {"error": f"搜索审批表单失败: {e}"}

        return list_result(raw, len(raw) if isinstance(raw, list) else 0, query=query)


class ApprovalGetDetailTool(BaseTool):
    name = "approval_get_detail"
    display_name = "查看审批详情"
    short_description = "获取某条审批实例的详情（含表单字段与填写值），需提供审批实例 ID"
    description = "查看钉钉审批实例详情"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "审批实例 ID（必填）",
            },
        },
        "required": ["instance_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        instance_id = (args.get("instance_id") or "").strip()
        if not instance_id:
            return {"error": "instance_id 不能为空"}

        try:
            raw = self.dws.oa_approval_detail(instance_id=instance_id)
        except Exception as e:
            logger.exception("获取审批详情失败: %s", e)
            return {"error": f"获取审批详情失败: {e}"}

        if raw is None:
            return {"instance_id": instance_id, "detail": None, "found": False}
        return {"instance_id": instance_id, "detail": raw, "found": True}


class ApprovalListPendingTool(BaseTool):
    name = "approval_list_pending"
    display_name = "查询待我审批"
    short_description = "查询待我处理的审批（时间窗 + 可选关键词过滤），需提供起止时间 ISO-8601"
    description = "查询待我审批的列表"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "起始时间 ISO-8601，如 2026-03-10T00:00:00+08:00（可选，默认近 30 天）",
            },
            "end": {
                "type": "string",
                "description": "结束时间 ISO-8601，如 2026-03-17T00:00:00+08:00（可选，默认当前时刻）",
            },
            "query": {
                "type": "string",
                "description": "关键词过滤，可选",
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
        now = datetime.now(timezone(timedelta(hours=8)))
        start = (args.get("start") or "").strip()
        end = (args.get("end") or "").strip()
        if not start:
            start = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        if not end:
            end = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        query = (args.get("query") or "").strip() or None
        limit = min(_coerce_limit(args.get("limit"), 50), 20)  # 钉钉接口单页上限 20，超限会 success=false

        try:
            raw = self.dws.oa_approval_list_pending(start=start, end=end, query=query, limit=limit)
        except Exception as e:
            logger.exception("查询待审批列表失败: %s", e)
            return {"error": f"查询待审批列表失败: {e}"}

        return list_result(raw, limit, start=start, end=end)


class ApprovalListTasksTool(BaseTool):
    name = "approval_list_tasks"
    display_name = "查询审批任务"
    short_description = "查询某审批实例下待我审批的任务（任务 ID 列表），需提供审批实例 ID"
    description = "查询某审批实例下的审批任务"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "审批实例 ID（必填）",
            },
        },
        "required": ["instance_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        instance_id = (args.get("instance_id") or "").strip()
        if not instance_id:
            return {"error": "instance_id 不能为空"}

        try:
            raw = self.dws.oa_approval_tasks(instance_id=instance_id)
        except Exception as e:
            logger.exception("查询审批任务失败: %s", e)
            return {"error": f"查询审批任务失败: {e}"}

        return list_result(raw, len(raw) if isinstance(raw, list) else 0, instance_id=instance_id)


class ApprovalListInitiatedTool(BaseTool):
    name = "approval_list_initiated"
    display_name = "查询已发起审批"
    short_description = "查询某审批模板下已发起的审批记录（按时间窗），需提供模板 processCode 与起止时间"
    description = "查询已发起的审批记录"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "process_code": {
                "type": "string",
                "description": "审批模板 processCode（必填）",
            },
            "start": {
                "type": "string",
                "description": "起始时间 ISO-8601（必填）",
            },
            "end": {
                "type": "string",
                "description": "结束时间 ISO-8601（必填）",
            },
            "cursor": {
                "type": "string",
                "description": "分页游标，可选，默认 '0'",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 20",
            },
        },
        "required": ["process_code", "start", "end"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        process_code = (args.get("process_code") or "").strip()
        start = (args.get("start") or "").strip()
        end = (args.get("end") or "").strip()
        if not process_code or not start or not end:
            return {"error": "process_code / start / end 均不能为空"}
        cursor = (args.get("cursor") or "0").strip() or "0"
        limit = min(_coerce_limit(args.get("limit"), 20), 20)  # 钉钉接口单页上限 20，超限会 success=false

        # 自动翻页：累计到 limit 或 hasMore=false 为止（钉钉单页上限 20）
        items: list = []
        next_cursor = cursor
        try:
            while len(items) < limit:
                per_page = min(limit - len(items), 20)
                raw = self.dws.oa_approval_list_initiated(
                    process_code=process_code, start=start, end=end,
                    cursor=next_cursor, limit=per_page
                )
                page_items = (raw or {}).get("processInstanceList") or []
                if not isinstance(page_items, list):
                    page_items = []
                items.extend(page_items)
                if not (raw or {}).get("hasMore"):
                    next_cursor = None
                    break
                nxt = (raw or {}).get("nextCursor")
                if not nxt:
                    next_cursor = None
                    break
                next_cursor = str(nxt)
        except Exception as e:
            logger.exception("查询已发起审批记录失败: %s", e)
            return {"error": f"查询已发起审批记录失败: {e}"}

        return list_result(
            items, limit, process_code=process_code, start=start, end=end,
            has_more=bool(next_cursor is not None),
            next_cursor=next_cursor or "",
        )


class ApprovalListExecutedTool(BaseTool):
    name = "approval_list_executed"
    display_name = "查询我已处理的审批"
    short_description = "查询当前用户「已处理/已审批」的审批单（审批人视角，含同意/拒绝/转交），支持时间窗过滤"
    description = (
        "查询当前登录用户作为审批人已经处理过的审批单列表。这与「已发起审批」(发起人视角) 不同——"
        "本工具回答的是「我批过哪些 / 我处理过哪些」，而非「我发起过哪些」。"
        "接口本身不支持时间参数，因此按 processCreateTime/processEndTime 在客户端按时间窗过滤。"
        "适合回答「上周OWNER处理了哪些审批」「最近我批了什么」等问题。"
    )
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    require_confirm = False
    parameters = {
        "type": "object",
        "properties": {
            "start": {
                "type": "string",
                "description": "时间窗起始 ISO-8601，如 2026-07-07T00:00:00+08:00（可选，默认近 30 天）",
            },
            "end": {
                "type": "string",
                "description": "时间窗结束 ISO-8601，如 2026-07-13T23:59:59+08:00（可选，默认当前时刻）",
            },
            "query": {
                "type": "string",
                "description": "关键词过滤（匹配标题/申请人/表单摘要），可选",
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

    @staticmethod
    def _parse_ts(value) -> int | None:
        """把钉钉毫秒时间戳转为 int；非法值返回 None。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def execute(self, args: dict) -> str | dict:
        now = datetime.now(timezone(timedelta(hours=8)))
        start_s = (args.get("start") or "").strip()
        end_s = (args.get("end") or "").strip()
        if not start_s:
            start_s = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        if not end_s:
            end_s = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        query = (args.get("query") or "").strip() or None
        limit = min(_coerce_limit(args.get("limit"), 50), 20)  # 接口单页上限 20，超限返回空

        # 把窗口解析成 ms 阈值
        try:
            start_dt = datetime.fromisoformat(start_s)
            end_dt = datetime.fromisoformat(end_s)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone(timedelta(hours=8)))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone(timedelta(hours=8)))
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(end_dt.timestamp() * 1000)
        except ValueError:
            return {"error": f"时间格式错误: start={start_s!r} end={end_s!r}"}

        items: list[dict] = []
        page = 1
        try:
            while len(items) < limit:
                raw = self.dws.oa_approval_list_executed(page=page, limit=limit, query=query)
                page_items = (raw or {}).get("values") or []
                if not isinstance(page_items, list):
                    page_items = []
                if not page_items:
                    break
                for it in page_items:
                    # 客户端时间窗过滤：processCreateTime 或 processEndTime 落在窗口内即纳入
                    create_ms = self._parse_ts(it.get("processCreateTime"))
                    end_t = self._parse_ts(it.get("processEndTime"))
                    hit = False
                    if create_ms is not None and start_ms <= create_ms <= end_ms:
                        hit = True
                    if not hit and end_t is not None and start_ms <= end_t <= end_ms:
                        hit = True
                    if hit:
                        items.append(it)
                if not (raw or {}).get("hasMore"):
                    break
                page += 1
        except Exception as e:
            logger.exception("查询已处理审批失败: %s", e)
            return {"error": f"查询已处理审批失败: {e}"}

        return list_result(
            items, limit, start=start_s, end=end_s,
            has_more=False,  # 客户端已按窗口裁剪，无法判断是否还有更早的
        )
