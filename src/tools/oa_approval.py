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
from src.im_adapter.errors import IMAdapterError
from src.tools.base import BaseTool
from src.tools.utils import _coerce_limit, list_result

# 钉钉 list-initiated / list-by-admin 接口硬限制：start/end 间隔不超过 120 天，
# 超窗服务端报「时间戳无效」/ business error（误导性，2026-10-26 实测），
# 故工具层自动按 ≤120 天分段查询后聚合。分段数设上限防 LLM 传超长窗口造成请求风暴。
# 另经实测（list-by-admin 二分定位）：start 不得早于 365 天前，否则同样报错——
# 超出时自动 clamp 到一年前并在结果 meta 标注，避免 LLM 浪费轮次重试。
_MAX_WINDOW_DAYS = 120
_MAX_SEGMENTS = 12
_MAX_START_AGE_DAYS = 365


def _parse_iso_dt(value: str) -> datetime | None:
    """解析 ISO-8601；无时区时按东八区处理，非法返回 None。"""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt


def _split_time_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """把超长时间窗切成 ≤120 天的连续段（首尾相接不重叠），段数超上限时截断。"""
    windows: list[tuple[datetime, datetime]] = []
    cur = start
    while cur < end and len(windows) < _MAX_SEGMENTS:
        nxt = min(cur + timedelta(days=_MAX_WINDOW_DAYS), end)
        windows.append((cur, nxt))
        cur = nxt
    return windows

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
    description = (
        "查看钉钉审批实例详情。典型场景：别人把审批通知转发到聊天里时，先从通知文本提取"
        "模板名与提交人（如「周萍提交的其他合同审批」），用 approval_search_forms 找到"
        " processCode、再用 approval_list_org 按近期时间窗定位实例拿到 instance_id，"
        "最后用本工具取表单明细回答。"
    )
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


class ApprovalGetFormSchemaTool(BaseTool):
    name = "approval_get_form_schema"
    display_name = "查看审批表单字段配置"
    short_description = (
        "查询审批模板的字段级配置（字段名/组件类型/是否必填/选项），"
        "可按字段名反查哪些表单含该字段（如「项目」字段盘点）"
    )
    description = "查询钉钉审批表单的字段配置"
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "process_code": {
                "type": "string",
                "description": "审批模板 processCode（与 form_name 二选一）",
            },
            "form_name": {
                "type": "string",
                "description": "表单名称关键词（与 process_code 二选一），会先搜索再逐个取 Schema",
            },
            "field_name": {
                "type": "string",
                "description": (
                    "只关心含该字段的表单时填写（如「项目」）；"
                    "给了 process_code 就只筛该表单的字段，不填则返回全部字段"
                ),
            },
            "max_forms": {
                "type": "integer",
                "description": "按 form_name 搜索时最多检查多少个表单，默认 20，上限 50",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        process_code = (args.get("process_code") or "").strip()
        form_name = (args.get("form_name") or "").strip()
        field_name = (args.get("field_name") or "").strip()
        max_forms = _coerce_limit(args.get("max_forms"), 20)

        if not process_code and not form_name:
            return {"error": "process_code 与 form_name 至少提供一个"}

        # 目标 processCode 列表
        if process_code:
            codes = [process_code]
        else:
            try:
                found = self.dws.oa_approval_search_forms(query=form_name)
            except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
                logger.exception("搜索审批表单失败: %s", e)
                return {"error": f"搜索审批表单失败: {e}"}
            codes = []
            for it in (found or []):
                if not isinstance(it, dict):
                    continue
                code = it.get("processCode") or it.get("process_code")
                if code:
                    codes.append((str(code), str(it.get("name") or it.get("processName") or "")))
            if not codes:
                return {"count": 0, "items": [], "hint": f"未找到名称含「{form_name}」的审批表单"}
            codes = codes[:max_forms]

        items: list[dict] = []
        errors: list[dict] = []
        for entry in codes:
            code, fallback_name = entry if isinstance(entry, tuple) else (entry, "")
            try:
                schema = self.dws.oa_approval_form_schema(code)
            except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
                logger.warning("获取表单 Schema 失败(%s): %s", code, e)
                errors.append({"process_code": code, "error": str(e)})
                continue
            if not isinstance(schema, dict):
                continue
            if schema.get("error"):
                errors.append({"process_code": code, "error": schema["error"]})
                continue
            fields = schema.get("fields") or []
            if field_name:
                matched = [
                    f for f in fields
                    if isinstance(f, dict) and field_name in str(f.get("label") or "")
                ]
                if not matched:
                    continue
                items.append({
                    "process_code": schema.get("process_code") or code,
                    "process_name": schema.get("process_name") or fallback_name,
                    "matched_fields": matched,
                })
            else:
                items.append({
                    "process_code": schema.get("process_code") or code,
                    "process_name": schema.get("process_name") or fallback_name,
                    "process_status": schema.get("process_status") or "",
                    "can_modify": schema.get("can_modify"),
                    "fields": fields,
                    "field_count": schema.get("field_count") or len(fields),
                })

        result: dict = {"count": len(items), "items": items}
        if field_name:
            result["field_name"] = field_name
            result["hint"] = (
                f"共 {len(items)} 个表单含「{field_name}」字段"
                if items else f"没有表单含「{field_name}」字段"
            )
        if errors:
            result["errors"] = errors
        return result


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
    short_description = "查询某审批模板下已发起的审批记录（按时间窗），需提供模板 processCode 与起止时间；跨度超 120 天自动分段"
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
                "description": "起始时间 ISO-8601（必填），如 2026-09-01T00:00:00+08:00",
            },
            "end": {
                "type": "string",
                "description": "结束时间 ISO-8601（必填），如 2026-10-26T23:59:59+08:00；与 start 跨度可超过 120 天（自动分段查询）",
            },
            "cursor": {
                "type": "string",
                "description": "分页游标，可选，默认 '0'（仅单段窗口内有效）",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 20",
            },
        },
        "required": ["process_code", "start", "end"],
    }

    # 窗口限制与分段常量见模块级 _MAX_WINDOW_DAYS / _MAX_SEGMENTS 说明

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

        start_dt = _parse_iso_dt(start)
        end_dt = _parse_iso_dt(end)
        if start_dt is None or end_dt is None or start_dt >= end_dt:
            return {"error": "start/end 需为合法 ISO-8601 时间且 start 早于 end（如 2026-09-01T00:00:00+08:00）"}

        # start 距今超一年部分接口查不到：clamp 到一年前并在 meta 标注（而非报错让 LLM 重试）
        start_clamped = False
        effective_start = start
        floor_dt = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))) - timedelta(days=_MAX_START_AGE_DAYS)
        if start_dt < floor_dt:
            start_dt = floor_dt
            start_clamped = True
            effective_start = floor_dt.isoformat()

        span_days = (end_dt - start_dt).days
        if span_days > _MAX_WINDOW_DAYS * _MAX_SEGMENTS:
            return {
                "error": (
                    f"时间窗跨度过大（约 {span_days} 天）：单段上限 120 天、自动分段最多 "
                    f"{_MAX_SEGMENTS} 段，请缩小时间窗或分多次查询。"
                )
            }

        # 显式 cursor 只在单段窗口内有意义；分段查询从每段首页开始取（聚合后按 limit 截断）
        segments = (
            _split_time_windows(start_dt, end_dt)
            if span_days > _MAX_WINDOW_DAYS
            else [(start_dt, end_dt)]
        )

        # 自动翻页 + 分段聚合：累计到 limit 或所有段取完为止（钉钉单页上限 20）
        items: list = []
        seen_ids: set[str] = set()
        next_cursor = cursor
        has_more = False
        try:
            for seg_start, seg_end in segments:
                seg_cursor = cursor if len(segments) == 1 else "0"
                while len(items) < limit:
                    per_page = min(limit - len(items), 20)
                    raw = self.dws.oa_approval_list_initiated(
                        process_code=process_code,
                        start=seg_start.isoformat(),
                        end=seg_end.isoformat(),
                        cursor=seg_cursor, limit=per_page
                    )
                    page_items = (raw or {}).get("processInstanceList") or []
                    if not isinstance(page_items, list):
                        page_items = []
                    for it in page_items:
                        key = str(it.get("processInstanceId") or id(it))
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                        items.append(it)
                    if not (raw or {}).get("hasMore"):
                        seg_cursor = None
                        break
                    nxt = (raw or {}).get("nextCursor")
                    if not nxt:
                        seg_cursor = None
                        break
                    seg_cursor = str(nxt)
                if len(items) >= limit:
                    # 最后一段可能还有余量（hasMore 语义在分段下不跨段），如实上报
                    has_more = bool(seg_cursor is not None)
                    break
        except Exception as e:
            logger.exception("查询已发起审批记录失败: %s", e)
            return {"error": f"查询已发起审批记录失败: {e}"}

        return list_result(
            items, limit, process_code=process_code, start=start, end=end,
            has_more=has_more,
            next_cursor=next_cursor if len(segments) == 1 and has_more else "",
            windows_queried=len(segments),
            start_clamped=start_clamped, effective_start=effective_start,
        )


class ApprovalListOrgTool(BaseTool):
    name = "approval_list_org"
    display_name = "查询组织审批实例"
    short_description = "以管理员视角查询某审批模板在全组织的审批实例（按时间窗），适合查看别人转发的审批流程"
    description = (
        "以管理员身份按模板 + 时间窗查询全组织的审批实例列表（可选按状态 / 发起人过滤）。"
        "典型场景：别人把审批通知转发给你看（如「周萍提交的其他合同审批」）——此时发起人不是当前用户，"
        "list_initiated / list_pending / list_executed 三个个人视角都查不到，应改用本工具："
        "先用 approval_search_forms 按模板名找到 processCode，再用本工具按近期时间窗查实例"
        "（标题含「提交人姓名」，可据此匹配），最后用 approval_get_detail 取表单明细。"
        "时间窗超过 120 天会自动分段查询，无需关心接口限制。"
    )
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "process_code": {
                "type": "string",
                "description": "审批模板 processCode（必填），可用 approval_search_forms 按模板名获取",
            },
            "start": {
                "type": "string",
                "description": "起始时间 ISO-8601（必填），如 2026-09-26T00:00:00+08:00",
            },
            "end": {
                "type": "string",
                "description": "结束时间 ISO-8601（必填），如 2026-10-26T23:59:59+08:00",
            },
            "statuses": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按审批状态过滤（可选）：RUNNING 进行中 / COMPLETED 已完成 / TERMINATED 已终止",
            },
            "user_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按发起人 userId 过滤（可选，多个用数组）",
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
        limit = min(_coerce_limit(args.get("limit"), 20), 20)  # 钉钉接口单页上限 20

        statuses = [s.strip() for s in (args.get("statuses") or []) if str(s).strip()]
        user_ids = [u.strip() for u in (args.get("user_ids") or []) if str(u).strip()]

        start_dt = _parse_iso_dt(start)
        end_dt = _parse_iso_dt(end)
        if start_dt is None or end_dt is None or start_dt >= end_dt:
            return {"error": "start/end 需为合法 ISO-8601 时间且 start 早于 end（如 2026-09-01T00:00:00+08:00）"}

        # start 距今超一年部分接口查不到：clamp 到一年前并在 meta 标注（而非报错让 LLM 重试）
        start_clamped = False
        effective_start = start
        floor_dt = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))) - timedelta(days=_MAX_START_AGE_DAYS)
        if start_dt < floor_dt:
            start_dt = floor_dt
            start_clamped = True
            effective_start = floor_dt.isoformat()

        span_days = (end_dt - start_dt).days
        if span_days > _MAX_WINDOW_DAYS * _MAX_SEGMENTS:
            return {
                "error": (
                    f"时间窗跨度过大（约 {span_days} 天）：单段上限 120 天、自动分段最多 "
                    f"{_MAX_SEGMENTS} 段，请缩小时间窗或分多次查询。"
                )
            }

        items: list = []
        seen_ids: set[str] = set()
        has_more = False
        segments = _split_time_windows(start_dt, end_dt)
        try:
            for seg_start, seg_end in segments:
                seg_cursor = "0"
                while len(items) < limit:
                    per_page = min(limit - len(items), 20)
                    raw = self.dws.oa_approval_list_by_admin(
                        process_code=process_code,
                        start=seg_start.isoformat(),
                        end=seg_end.isoformat(),
                        cursor=seg_cursor, limit=per_page,
                        statuses=statuses or None,
                        user_ids=user_ids or None,
                    )
                    page_items = (raw or {}).get("list") or []
                    if not isinstance(page_items, list):
                        page_items = []
                    for it in page_items:
                        key = str(it.get("processInstanceId") or id(it))
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                        items.append(it)
                    if not (raw or {}).get("hasMore"):
                        break
                    nxt = (raw or {}).get("nextCursor")
                    if not nxt:
                        break
                    seg_cursor = str(nxt)
                if len(items) >= limit:
                    has_more = True
                    break
        except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
            logger.exception("查询组织审批实例失败: %s", e)
            return {"error": f"查询组织审批实例失败: {e}"}

        return list_result(
            items, limit, process_code=process_code, start=start, end=end,
            has_more=has_more, windows_queried=len(segments),
            start_clamped=start_clamped, effective_start=effective_start,
        )


class ApprovalListCcTool(BaseTool):
    name = "approval_list_cc"
    display_name = "查询抄送我的审批"
    short_description = "查询抄送给当前用户的审批单列表（含表单内容摘要与跳转链接）"
    description = (
        "查询抄送（CC）给当前登录用户的审批单列表，含表单内容摘要、发起人姓名与审批单跳转链接。"
        "适合回答「抄给我的有哪些审批」「最近谁抄送了我什么」。"
        "注意与转发消息不同：别人在聊天里转发审批通知请用 approval_list_org 定位实例。"
    )
    intent_categories = ["domain.oa_approval"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "关键词过滤（可选，匹配标题 / 表单内容）",
            },
            "process_code": {
                "type": "string",
                "description": "审批模板 processCode（可选）",
            },
            "originator_user_id": {
                "type": "string",
                "description": "按发起人 userId 过滤（可选）",
            },
            "create_time_from": {
                "type": "string",
                "description": "发起时间起始，格式 yyyy-MM-dd（可选）",
            },
            "create_time_to": {
                "type": "string",
                "description": "发起时间截止，格式 yyyy-MM-dd，含当日（可选）",
            },
            "page": {
                "type": "integer",
                "description": "页码，默认 1",
            },
            "limit": {
                "type": "integer",
                "description": "每页条数，默认 20，最大 100",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        page = max(_coerce_limit(args.get("page"), 1), 1)
        limit = min(_coerce_limit(args.get("limit"), 20), 100)
        query = (args.get("query") or "").strip() or None
        process_code = (args.get("process_code") or "").strip() or None
        originator = (args.get("originator_user_id") or "").strip() or None
        create_from = (args.get("create_time_from") or "").strip() or None
        create_to = (args.get("create_time_to") or "").strip() or None

        try:
            raw = self.dws.oa_approval_list_cc(
                page=page, limit=limit, query=query, process_code=process_code,
                originator_user_id=originator,
                create_time_from=create_from, create_time_to=create_to,
            )
        except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
            logger.exception("查询抄送审批失败: %s", e)
            return {"error": f"查询抄送审批失败: {e}"}

        items = (raw or {}).get("values") or []
        if not isinstance(items, list):
            items = []
        return list_result(
            items, limit, page=page,
            has_more=bool((raw or {}).get("hasMore")),
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
