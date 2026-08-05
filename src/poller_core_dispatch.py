from __future__ import annotations

import logging
from datetime import datetime


logger = logging.getLogger(__name__)


from src.poller_mixins_base import PollerMixinBase


class DispatchMixin(PollerMixinBase):
    """MessagePoller 子系统萃取（mixin，经多继承组合回主类）。"""

    def _dispatch_messages(self, messages: list, is_cold_start: bool = False) -> list:
        """背压派发：按时间戳升序（最旧优先）排列，并受 max_dispatch_per_cycle 限速，
        返回本轮应派发的消息列表。

        未被选中的消息保持“未处理”状态，下一轮（间隔后）再被 poll_once 取回，
        实现自然限速、不丢消息。这样重启/突发时即使 poll_once 一次捞出大量积压，
        也不会在同一轮内无限制地并发派发打爆 LLM/接口。

        is_cold_start: 重启后首次轮询（_last_list_all_time 为 None）标记，用于日志提示。
        """
        if not messages:
            return []
        ordered = sorted(messages, key=lambda m: m.timestamp or datetime.min)
        cap = self.config.max_dispatch_per_cycle
        if cap and cap < len(ordered):
            logger.info(
                "[背压] 单轮派发上限 %d，本轮处理 %d/%d 条（最旧优先）%s，"
                "剩余 %d 条留待后续轮次自然限速处理",
                cap, cap, len(ordered),
                "（冷启动积压）" if is_cold_start else "",
                len(ordered) - cap)
            return ordered[:cap]
        return ordered

    def get_backpressure_metrics(self) -> dict:
        """返回背压相关监控指标（P1-E），供 /api/backpressure-metrics 读取。

        - dispatched_total / deferred_total: 累计派发 / 被限速延迟的条数
        - last_cycle_dispatched / last_cycle_deferred: 上一轮实际派发 / 延迟条数
        - cold_start_pending: 是否仍处于重启后首次轮询
        - max_dispatch_per_cycle / max_concurrent_replies: 当前生效的限速阈值
        """
        return {
            "dispatched_total": self._dispatch_total,
            "deferred_total": self._deferred_total,
            "last_cycle_dispatched": self._last_cycle_dispatched,
            "last_cycle_deferred": self._last_cycle_deferred,
            "cold_start_pending": self._first_poll,
            "max_dispatch_per_cycle": self.config.max_dispatch_per_cycle,
            "max_concurrent_replies": self.config.max_concurrent_replies,
        }

    def get_observability(self) -> dict:
        """返回轮询器综合可观测指标（供 /api/poller-status 读取）。

        - last_poll_at:        最近一次轮询开始时间（ISO）
        - last_error / last_error_at: 最近一次轮询异常信息及时间
        - queue_depth:         最近一轮拉取到的待处理消息数（当前负载）
        - poll_count:          累计轮询次数
        - 其余字段复用背压指标（派发/限速/冷启动/阈值）
        """
        return {
            "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at.isoformat() if self._last_error_at else None,
            "queue_depth": self._queue_depth,
            "poll_count": self._poll_count,
            **self.get_backpressure_metrics(),
        }
