"""DwsAdapter AI 搜问（aisearch）只读 mixin。拆分自 dws_adapter.py。

⚠️ 与 Linkora 现有能力的分工（dws v1.0.63 官方「Avoid when」提示 + 实测）：
- ``aisearch enterprise``：跨类型**语义聚合**检索（document/im/mail/calendar/todo/
  minute/report/image/link/notable/baike），返回「有什么内容」的候选片段。它**不**替代
  Linkora 现有的结构化查询——``search_messages``（chat +search-msg，可枚举消息记录并
  按发送人/会话/时间筛选）、``search_doc``、``list_minutes`` 等；那些要精确答案的场景
  仍走原工具。本mixin 补的是「跨类型 + 语义」的缺口。
- ``aisearch behavior``：行为追溯（我发给谁/谁发给我/我创建过什么），按
  behavior-type（send/create/share/edit/receive）+ direction（我->某人）检索。
- ``aisearch person``：语义找人（按姓名/部门/职责/组织关系）。⚠️ 实测在当前企业
  恒返回空（应用未开通或无索引），故**不**实现——留待开通后再评估。

⚠️ 返回体形态：``{"result":[...], "searchEvidence":{...}, "success":true}``——``result``
是**顶层数组**（不是嵌套对象），且 ``_get_result()`` 会原样返回该 dict（无 ``result``
键包裹的情形不适用——这里**有** ``result`` 键但它是数组）。故统一用 ``_aisearch_items``
取数组并逐项归一。

⚠️ ``_searchEvidence`` 是**完整性证据**，必须透传给调用方：
- ``contentKind: "search_snippet"`` + ``originalContentRead: false`` 表示这是**摘要片段**、
  **未读原文**。若丢掉该字段，LLM 会误以为已读全文并据此断言细节。
- ``queryMatch``：``no_text_evidence`` / ``partial_text_evidence`` 表示**语义候选**、
  非确认的主题匹配；``not_requested`` 表示未做关键词匹配（纯时间/类型筛选）。
"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging

from src.dws_adapter.core import DwsError

logger = logging.getLogger(__name__)

# 合法内容类型（dws v1.0.63 枚举）
_AISEARCH_TYPES = {
    "all", "document", "im", "calendar", "todo", "minute", "report",
    "image", "link", "notable", "baike", "mail",
}
# 合法行为类型
_AISEARCH_BEHAVIOR_TYPES = {"all", "send", "create", "share", "edit", "receive"}


class DwsAdapterAisearchMixin(DwsAdapterBase):
    @staticmethod
    def _aisearch_items(data: object) -> list:
        """取出 aisearch 的结果数组。

        返回体是 ``{"result":[...], "searchEvidence":{...}}``——``result`` 在顶层且为
        数组。兼容 ``result`` 缺失时直接返回 list 的形态。
        """
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        result = data.get("result")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            # 少数形态把条目包在 items/records 下
            for key in ("items", "records", "list"):
                value = result.get(key)
                if isinstance(value, list):
                    return value
        return []

    @staticmethod
    def _aisearch_evidence(item: object) -> dict:
        """取单项的 ``_searchEvidence``（完整性证据），非 dict 时返回空 dict。"""
        if not isinstance(item, dict):
            return {}
        evidence = item.get("_searchEvidence") or item.get("searchEvidence")
        return evidence if isinstance(evidence, dict) else {}

    def aisearch_enterprise(self, queries: str = "", *,
                            types: str = "all",
                            time_range: str = "") -> list:
        """跨类型语义检索企业内部知识内容（文档/消息/日程/待办/听记/日志/邮件等）。

        Args:
            queries: 内容关键词，多个用逗号分隔。**只放内容关键词**——时间词归
                ``time_range``、类型词归 ``types``（dws 明确要求分槽位）。
            types: 内容类型，多个用逗号分隔；默认 all。
            time_range: 时间范围（如「今天」「本周」「9月」「过去一周」）。仅在用户
                显式给出时间词时填写。

        返回结果 list，每项含 ``title`` / ``snippet`` / ``source_type`` /
        ``source_name`` / ``date`` / ``actor`` / ``url`` / ``evidence``（完整性证据：
        ``content_kind`` / ``original_content_read`` / ``query_match``）。

        ⚠️ 返回的是**摘要片段**（``originalContentRead`` 通常为 false），
        不是原文全文——需要原文时用对应产品的专用命令再取。
        """
        args = ["aisearch", "enterprise"]
        type_list = self._aisearch_normalize_types(types)
        if type_list:
            args.extend(["--types", type_list])
        keyword = (queries or "").strip()
        if keyword:
            args.extend(["--queries", keyword])
        window = (time_range or "").strip()
        if window:
            args.extend(["--time-range", window])
        try:
            data = self.run(args, operation="aisearch_enterprise", force_no_dry_run=True)
        except (DwsError, ValueError, OSError) as e:
            logger.warning("[DWS] 企业内容语义检索失败: %s", e)
            return []
        return self._normalize_aisearch_items(self._aisearch_items(data))

    def aisearch_behavior(self, queries: str = "", *,
                          types: str = "all",
                          time_range: str = "",
                          behavior_type: str = "all",
                          direction: str = "",
                          chat_scope: str = "") -> list:
        """行为追溯检索（我发给谁 / 谁发给我 / 我创建过什么）。

        Args:
            queries: 内容关键词（只放内容词）。
            types: 内容类型，默认 all。
            time_range: 时间范围。
            behavior_type: 行为类型（all/send/create/share/edit/receive），默认 all。
            direction: 交互方向，如「我->某人」「某人->我」。
            chat_scope: 消息所在会话/群范围（仅 IM 类型且用户明确指定群名时）。

        ⚠️ 行为**方向**语义与「某个人的完整消息集合」不同——要枚举完整消息记录
        请用 ``chat message search``（Linkora 的 ``search_messages``）。
        """
        args = ["aisearch", "behavior"]
        type_list = self._aisearch_normalize_types(types)
        if type_list:
            args.extend(["--types", type_list])
        keyword = (queries or "").strip()
        if keyword:
            args.extend(["--queries", keyword])
        window = (time_range or "").strip()
        if window:
            args.extend(["--time-range", window])
        action = (behavior_type or "all").strip().lower()
        if action in _AISEARCH_BEHAVIOR_TYPES and action != "all":
            args.extend(["--behavior-type", action])
        flow = (direction or "").strip()
        if flow:
            args.extend(["--direction", flow])
        scope = (chat_scope or "").strip()
        if scope:
            args.extend(["--chat-scope", scope])
        try:
            data = self.run(args, operation="aisearch_behavior", force_no_dry_run=True)
        except (DwsError, ValueError, OSError) as e:
            logger.warning("[DWS] 企业行为检索失败: %s", e)
            return []
        return self._normalize_aisearch_items(self._aisearch_items(data))

    @staticmethod
    def _aisearch_normalize_types(types: str) -> str:
        """校验并归一类型列表（非法类型剔除，全非法则回落all）。"""
        raw = [t.strip() for t in (types or "").split(",") if t.strip()]
        valid = [t for t in raw if t in _AISEARCH_TYPES]
        if not valid:
            return "all"
        # all 与具体类型并存在 dws 侧是「或」关系，但语义含混——只留具体类型
        if "all" in valid and len(valid) > 1:
            valid = [t for t in valid if t != "all"]
        return ",".join(valid)

    @staticmethod
    def _normalize_aisearch_items(items: list) -> list:
        """归一 aisearch 结果项，保留完整性证据（evidence）。"""
        normalized: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            evidence = DwsAdapterAisearchMixin._aisearch_evidence(item)
            normalized.append({
                "title": item.get("title") or "",
                "snippet": item.get("snippet") or "",
                "source_type": item.get("sourceType") or item.get("source_type") or "",
                "source_name": item.get("sourceName") or item.get("source_name") or "",
                "date": item.get("date") or "",
                "actor": item.get("actor") or "",
                "url": item.get("url") or "",
                "evidence": {
                    "content_kind": evidence.get("contentKind") or "",
                    # 关键：False 表示「只是摘要片段、未读原文」，须让调用方知道
                    "original_content_read": bool(evidence.get("originalContentRead")),
                    # no_text_evidence / partial_text_evidence = 语义候选，非确认匹配
                    "query_match": evidence.get("queryMatch") or "",
                    "within_requested_range": (
                        (evidence.get("timeEvidence") or {}).get("withinRequestedRange")
                        if isinstance(evidence.get("timeEvidence"), dict) else None
                    ),
                },
            })
        return normalized
