"""DwsAdapter 钉钉日志（OA 周报/日报应用）只读 mixin。拆分自 dws_adapter.py。

⚠️ 载体辨义（勿与钉钉在线文档混淆）：本mixin 管的是「钉钉日志」OA 应用——按模版
填报日报/周报、收件箱（别人发给我的）、发件箱（我发出的）。在线文档是 ``dws doc``，
待办事项是 ``dws todo``。

⚠️ 只用新的「资源.动词」命令树，**不用**已deprecated 的扁平别名（旧名→新名）：
``list``→``inbox list``、``created``→``outbox list``、``detail``→``entry get``、
``stats``→``entry stats``、``create``→``entry submit``、``sent``→``outbox list``。

⚠️ **返回体有两种形态**（v1.0.63 实测，勿混用）：
- Shortcut（``+inbox-list`` / ``+outbox-list`` / ``+template-search`` / ``+report-latest``）
  返回 ``{"ok":true,"outcome":"success","data":{...},"meta":{...}}``——**没有** ``result``
  包装，``_get_result()`` 会原样返回，故必须先取 ``data`` 层。
- 原子 ``report entry get`` 返回 ``{"result":{...},"success":true}``——**有** ``result`` 包装。
故本mixin 统一用 ``_report_data()`` 归一：先解 ``data``，再回落 ``result``。
"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging
from datetime import datetime, timedelta

from src.dws_adapter.core import DwsError

logger = logging.getLogger(__name__)

# dws 对时间窗的硬约束（实测）：
# - inbox（别人发给我的）：跨度 ≤ 180 天
# - outbox（我发出的） / report-latest：跨度 ≤ 20 天（实测「正好 20 天」也被拒，
#   故留1 天余量，见 _report_window）
_INBOX_MAX_DAYS = 180
_OUTBOX_MAX_DAYS = 20
# CLI 的 --size 合法区间是 1..20
_REPORT_SIZE_MIN, _REPORT_SIZE_MAX = 1, 20


class DwsAdapterReportMixin(DwsAdapterBase):
    # === 内部工具 ===

    @staticmethod
    def _report_data(data: object) -> dict:
        """归一 report 两种返回形态为同一个 dict。

        v1.0.63 实测：Shortcut 返回 ``{"ok":..,"outcome":..,"data":{...}}``（无 result
        包装），原子命令返回 ``{"result":{...},"success":true}``（有 result 包装）。
        两层都试，仍非 dict 则返回空 dict。
        """
        if not isinstance(data, dict):
            return {}
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner
        result = data.get("result")
        if isinstance(result, dict):
            return result
        return {}

    @staticmethod
    def _report_window(start: str = "", end: str = "", *,
                       max_days: int = _OUTBOX_MAX_DAYS,
                       end_now: bool = True) -> tuple[str, str, str]:
        """把可选的日期字符串规整为 dws 要求的 ISO-8601 时间窗。

        Args:
            start / end: ``YYYY-MM-DD`` 或完整 ISO-8601；为空时按``end_now`` 回落。
            max_days: 时间窗跨度上限（天）。
            end_now: end 为空时是否用「当前时刻」（inbox/outbox 为 True；不需要时False）。

        Returns:
            ``(start_iso, end_iso, error)``；参数非法时 ``error`` 非空，前两个为 ``""``。

        dws 要求**显式**时间窗，且跨度有硬上限，超限直接报``time_range_too_large``
        （且「正好等于上限」也会被拒——实测 20 天整的 outbox 窗口被拒，19 天通过）。
        故这里按 ``max_days - 1`` 留一天余量。
        """
        now = datetime.now().astimezone()

        def _parse(value: str, *, is_end: bool) -> datetime:
            # YYYY-MM-DD → 当天 00:00:00（结束日则补到 23:59:59，否则当天数据会被漏掉）
            if len(value) == 10:
                day = datetime.strptime(value, "%Y-%m-%d")
                base = day.replace(hour=23, minute=59, second=59) if is_end \
                    else day.replace(hour=0, minute=0, second=0)
                return base.astimezone()
            return datetime.fromisoformat(value).astimezone()

        try:
            start_dt = _parse(start, is_end=False) if start else \
                (now - timedelta(days=max_days - 1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
            end_dt = _parse(end, is_end=True) if end else \
                (now if end_now else now.replace(hour=23, minute=59, second=59,
                                                 microsecond=0))
        except ValueError:
            return "", "", f"时间格式非法（需 YYYY-MM-DD 或 ISO-8601）: start={start!r} end={end!r}"

        if end_dt <= start_dt:
            return "", "", f"时间窗非法：结束时间须晚于开始时间（start={start_dt} end={end_dt}）"

        # dws对「正好等于上限」的窗口也拒绝，留 1 天余量
        window_days = (end_dt - start_dt).days
        if window_days > max_days - 1:
            start_dt = (end_dt - timedelta(days=max_days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            logger.info("[DWS] report 时间窗超过 %d 天，已收窄至起始日 %s",
                        max_days, start_dt.date())

        fmt = "%Y-%m-%dT%H:%M:%S%z"
        return start_dt.strftime(fmt)[:-2] + ":" + start_dt.strftime(fmt)[-2:], \
            end_dt.strftime(fmt)[:-2] + ":" + end_dt.strftime(fmt)[-2:], ""

    @staticmethod
    def _report_items(data: object, *keys: str) -> list:
        """从归一后的 dict 里取列表字段（多键fallback，非 list 时返回 []）。"""
        if not isinstance(data, dict):
            return []
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        return []

    # === 对外能力 ===

    def report_inbox_list(self, start: str = "", end: str = "", *,
                          size: int = 20, cursor: int = 0,
                          sender_user_ids: str = "") -> list:
        """列出**我收到的**日志（收件箱），返回摘要 list。

        Args:
            start / end: 时间窗，默认近 180 天（inbox 跨度上限）。
            size: 每页条数，1..20。
            cursor: 续页游标（≥0，须严格前进）。
            sender_user_ids: 发送人 staffId 列表（逗号分隔），可空。

        返回 dws 原始摘要项（**camelCase**，v1.0.63 实测）：
        ``{"reportId","templateName","creatorUserId","createTime","modifiedTime"}``，
        时间戳为毫秒 epoch。``reportId`` 可直接喂给 :meth:`report_entry_get`。

        ⚠️ 时间窗跨度上限 180 天（outbox 只有 20 天，别混用）。
        """
        start_iso, end_iso, err = self._report_window(
            start, end, max_days=_INBOX_MAX_DAYS)
        if err:
            logger.warning("[DWS] report_inbox_list 参数非法: %s", err)
            return []
        args = [
            "report", "+inbox-list",
            "--start", start_iso,
            "--end", end_iso,
            "--size", str(max(_REPORT_SIZE_MIN, min(int(size), _REPORT_SIZE_MAX))),
            "--cursor", str(max(0, int(cursor))),
        ]
        if sender_user_ids:
            args.extend(["--sender-user-ids", sender_user_ids])
        try:
            data = self.run(args, operation="report_inbox_list", force_no_dry_run=True)
        except (DwsError, ValueError, OSError) as e:
            logger.warning("[DWS] 列出收件箱日志失败: %s", e)
            return []
        payload = self._report_data(data)
        return self._report_items(payload, "reports", "reportList", "items")

    def report_outbox_list(self, start: str = "", end: str = "", *,
                           size: int = 20, cursor: int = 0,
                           modified_start: str = "", modified_end: str = "") -> list:
        """列出**我发出的**日志（发件箱），返回摘要 list。

        ⚠️ 时间窗跨度上限仅 **20 天**（inbox 是 180 天），超限 dws 直接报
        ``time_range_too_large``；本方法会自动收窄起始日并打info 日志。
        """
        start_iso, end_iso, err = self._report_window(
            start, end, max_days=_OUTBOX_MAX_DAYS)
        if err:
            logger.warning("[DWS] report_outbox_list 参数非法: %s", err)
            return []
        args = [
            "report", "+outbox-list",
            "--size", str(max(_REPORT_SIZE_MIN, min(int(size), _REPORT_SIZE_MAX))),
            "--cursor", str(max(0, int(cursor))),
            "--start", start_iso,
            "--end", end_iso,
        ]
        # 修改时间窗必须成对提供，且跨度同样 ≤ 20 天
        if modified_start and modified_end:
            m_start, m_end, m_err = self._report_window(
                modified_start, modified_end, max_days=_OUTBOX_MAX_DAYS)
            if not m_err:
                args.extend(["--modified-start", m_start, "--modified-end", m_end])
            else:
                logger.warning("[DWS] report_outbox_list 忽略非法的修改时间窗: %s", m_err)
        try:
            data = self.run(args, operation="report_outbox_list", force_no_dry_run=True)
        except (DwsError, ValueError, OSError) as e:
            logger.warning("[DWS] 列出发件箱日志失败: %s", e)
            return []
        payload = self._report_data(data)
        return self._report_items(payload, "reports", "reportList", "items")

    def report_entry_get(self, report_id: str) -> dict:
        """读取单份日志详情，返回 dws 原始项（camelCase）。

        结构（v1.0.63 实测）：``report_content[]`` 为结构化字段列表，每项含
        ``key``（字段名）/ ``value``（JSON 字符串，如多选值带引号包裹）/ ``sort``
        / ``type`` / ``images``；同级还有 ``report_name`` / ``report_template_name``
        / ``template_id`` / ``creator_id`` / ``dept_name`` / ``url``（钉钉跳转链接）。

        ⚠️ 返回体**含提交人真实姓名**（``creator_name``）等个人信息，只应透传给请求方
        本人，**不得**写入日志 / 落库 / 外发。

        失败或 report_id 为空时返回空 dict。
        """
        if not report_id:
            return {}
        try:
            data = self.run(
                ["report", "entry", "get", "--report-id", report_id],
                operation="report_entry_get", force_no_dry_run=True,
            )
        except (DwsError, ValueError, OSError) as e:
            logger.warning("[DWS] 读取日志详情失败: %s", e)
            return {}
        return self._report_data(data)

    def report_template_search(self, query: str) -> list:
        """按名称关键词搜索可用的日志模板，返回 dws 原始项（camelCase）。

        每项形如 ``{"name","templateId","lastModifiedTime"}``；``templateId`` 是
        ``report entry get`` 之外的模板维度标识（读正文仍需 ``reportId``）。

        名称不区分大小写；query 为空直接返回 []（dws 会拒绝空关键词）。
        """
        keyword = (query or "").strip()
        if not keyword:
            return []
        try:
            data = self.run(
                ["report", "+template-search", "--query", keyword],
                operation="report_template_search", force_no_dry_run=True,
            )
        except (DwsError, ValueError, OSError) as e:
            logger.warning("[DWS] 搜索日志模板失败: %s", e)
            return []
        payload = self._report_data(data)
        return self._report_items(payload, "templates", "templateList", "items")
