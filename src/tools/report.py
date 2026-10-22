"""钉钉日志（日报/周报）工具（domain.report）。

把 `src/dws_adapter/report.py` 的 ``report_*`` 方法接成 Agent 工具，让数字分身在对话中
能直接回答「我上周交的周报写了啥 / 谁给我发过日报 / 有哪些日志模板 / 我的发件箱」，
支撑「问日报周报进展时自动给出有依据的内容」这一自动回复场景。

⚠️ 载体辨义：本工具查的是**钉钉日志 OA 应用**（按模版填报的日报/周报），不是钉钉在线文档
（那是 ``domain.doc``）。

⚠️ 隐私：``get_report_detail`` 的返回体含提交人真实姓名等个人信息，仅透传给请求方本人，
**不落库、不写日志、不外发**。

全部为只读操作，无需二次确认。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError
from src.tools.base import BaseTool

logger = logging.getLogger(__name__)

# 单份日志正文可达数万字，整段塞给 LLM 会冲爆上下文窗口并飙升 token 成本。
# 只截断单个字段值，保留字段结构（让 LLM 知道这份日志有哪些栏目）。
MAX_FIELD_CHARS = 4000
# 列表类工具返回条数上限
MAX_LIST_ITEMS = 20


def _ms_to_iso(ms: object) -> str:
    """毫秒epoch → ``YYYY-MM-DDTHH:MM:SS+08:00``；非法值原样返回。"""
    try:
        value = int(ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds")


def _normalize_report(item: object) -> dict:
    """把 dws 日志摘要项规整为稳定字段（camelCase → snake_case），非 dict 走 raw。"""
    if not isinstance(item, dict):
        return {"raw": item}
    return {
        "report_id": item.get("reportId") or item.get("report_id") or "",
        "template_name": item.get("templateName") or item.get("template_name") or "",
        "creator_user_id": item.get("creatorUserId") or item.get("creator_user_id") or "",
        "created_at": _ms_to_iso(item.get("createTime") or item.get("create_time")),
        "modified_at": _ms_to_iso(item.get("modifiedTime") or item.get("modified_time")),
    }


def _normalize_template(item: object) -> dict:
    if not isinstance(item, dict):
        return {"raw": item}
    return {
        "template_id": item.get("templateId") or item.get("template_id") or "",
        "name": item.get("name") or "",
        "last_modified_at": _ms_to_iso(
            item.get("lastModifiedTime") or item.get("last_modified_time")),
    }


class ListReportsTool(BaseTool):
    name = "list_reports"
    display_name = "列出钉钉日志"
    short_description = "列出我收到/发出的钉钉日报周报（可按时间范围筛选，支持按发送人过滤收到的日志）"
    description = "列出钉钉日志（日报/周报）"
    # 场景关键词统一维护在 IntentRegistry 的 domain.report（单一真源）
    intent_categories = ["domain.report"]
    # 钉钉日志 OA 应用专属
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "box": {
                "type": "string",
                "enum": ["inbox", "outbox"],
                "description": "inbox=我收到的日志(默认)，outbox=我发出的日志",
            },
            "start": {
                "type": "string",
                "description": "起始日期 YYYY-MM-DD，可选。outbox 跨度上限 20 天，inbox 上限 180 天",
            },
            "end": {
                "type": "string",
                "description": "结束日期 YYYY-MM-DD，可选，默认今天",
            },
            "limit": {
                "type": "integer",
                "description": "返回条数上限，默认 20，最大 20",
            },
        },
        "required": [],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        box = (args.get("box") or "inbox").strip() or "inbox"
        if box not in ("inbox", "outbox"):
            box = "inbox"
        start = (args.get("start") or "").strip()
        end = (args.get("end") or "").strip()
        try:
            limit = int(args.get("limit") or MAX_LIST_ITEMS)
        except (TypeError, ValueError) as exc:
            logger.debug("execute: limit 解析失败，回退默认值: %s", exc)
            limit = MAX_LIST_ITEMS
        limit = max(1, min(limit, MAX_LIST_ITEMS))

        try:
            if box == "outbox":
                raw = self.dws.report_outbox_list(start=start, end=end, size=limit)
            else:
                raw = self.dws.report_inbox_list(start=start, end=end, size=limit)
        except (IMAdapterError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("列出钉钉日志失败（%s）: %s", box, exc)
            return {"error": f"列出钉钉日志失败: {exc}"}

        items = [_normalize_report(it) for it in raw] if isinstance(raw, list) else []
        hint = ""
        if not items:
            hint = ("所选时间窗内没有日志。" + (
                "发件箱跨度上限 20 天，可缩小时间范围再试。" if box == "outbox" else
                "可放宽时间范围（收件箱上限 180 天）再试。"))
        return {"box": box, "count": len(items), "items": items[:limit], "hint": hint}


class GetReportDetailTool(BaseTool):
    name = "get_report_detail"
    display_name = "读取日报周报正文"
    short_description = "读取一份钉钉日志（日报/周报）的完整正文，按栏目结构化返回，可用于总结进展、提炼待办"
    description = "读取钉钉日志正文内容"
    intent_categories = ["domain.report"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "report_id": {
                "type": "string",
                "description": "日志 ID（先调 list_reports 取得 report_id）",
            },
        },
        "required": ["report_id"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        report_id = (args.get("report_id") or "").strip()
        if not report_id:
            return {"error": "需提供 report_id（可先用 list_reports 获取）"}

        try:
            entry = self.dws.report_entry_get(report_id)
        except (IMAdapterError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("读取钉钉日志详情失败: %s", exc)
            return {"error": f"读取钉钉日志详情失败: {exc}"}
        if not isinstance(entry, dict) or not entry:
            return {"error": "未找到该日志（report_id 可能已失效，或无权限查看）"}

        # report_content[] 是结构化字段列表；value 多为 JSON 字符串（如多选带引号包裹）
        sections: list[dict] = []
        raw_sections = entry.get("report_content") or entry.get("reportContent") or []
        if isinstance(raw_sections, list):
            for item in raw_sections:
                if not isinstance(item, dict):
                    continue
                value = item.get("value")
                text = value if isinstance(value, str) else ("" if value is None else str(value))
                sections.append({
                    "key": item.get("key") or "",
                    "value": text[:MAX_FIELD_CHARS],
                    "truncated": len(text) > MAX_FIELD_CHARS,
                    "type": item.get("type"),
                })

        # 仅取钉钉跳转链接的路径部分，避免整串 URL（含 corpId 等）外泄
        url = entry.get("url") or ""
        return {
            "report_id": entry.get("report_Id") or entry.get("reportId") or report_id,
            "title": entry.get("report_name") or entry.get("reportName") or "",
            "template_name": entry.get("report_template_name")
            or entry.get("reportTemplateName") or "",
            "creator": entry.get("creatorName") or entry.get("creator_name") or "",
            "dept_name": entry.get("deptName") or entry.get("dept_name") or "",
            "created_at": _ms_to_iso(entry.get("createTime") or entry.get("create_time")),
            "section_count": len(sections),
            "sections": sections,
            "has_link": bool(url),
        }


class ListReportTemplatesTool(BaseTool):
    name = "list_report_templates"
    display_name = "搜索钉钉日志模板"
    short_description = "按名称搜索可用的钉钉日报/周报模板（如「周报」「日报」），用于确认本单位的填报模板"
    description = "搜索钉钉日志模板"
    intent_categories = ["domain.report"]
    platforms = ["dingtalk"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "模板名称关键词，不区分大小写，如「周报」",
            },
        },
        "required": ["query"],
    }

    def __init__(self, dws: DwsAdapter):
        self.dws = dws

    def execute(self, args: dict) -> str | dict:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "需提供模板名称关键词（如「周报」）"}
        try:
            raw = self.dws.report_template_search(query)
        except (IMAdapterError, ValueError, OSError, RuntimeError) as exc:
            logger.warning("搜索钉钉日志模板失败: %s", exc)
            return {"error": f"搜索钉钉日志模板失败: {exc}"}
        items = [_normalize_template(it) for it in raw] if isinstance(raw, list) else []
        return {"query": query, "count": len(items), "items": items[:MAX_LIST_ITEMS]}
