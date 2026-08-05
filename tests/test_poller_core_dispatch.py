"""poller_core_dispatch.DispatchMixin 单元测试。

覆盖: _dispatch_messages 正向/冷启动/限速 + get_backpressure_metrics/get_observability。
"""

from datetime import datetime
from unittest.mock import MagicMock


from src.poller_core_dispatch import DispatchMixin


class FakeDispatch(DispatchMixin):
    """最小 fake，提供 dispatch mixin 所需的属性。"""

    def __init__(self, max_dispatch=10):
        self.config = MagicMock()
        self.config.max_dispatch_per_cycle = max_dispatch
        self._dispatch_total = 0
        self._deferred_total = 0
        self._last_cycle_dispatched = 0
        self._last_cycle_deferred = 0
        self._first_poll = True
        self._last_poll_at = datetime(2026, 7, 26, 12, 0, 0)
        self._last_error = None
        self._last_error_at = None
        self._queue_depth = 0
        self._poll_count = 0


# ============ _dispatch_messages ============

class TestDispatchMessages:
    def make_msg(self, msg_id, ts_str):
        from src.models import Message
        return Message(msg_id=msg_id, timestamp=datetime.fromisoformat(ts_str),
                       content="test", chat_id="c1", chat_type="group", chat_name="群",
                       sender_id="s1", sender_name="张三", msg_type="text")

    def test_empty_list(self):
        fd = FakeDispatch()
        assert fd._dispatch_messages([]) == []

    def test_all_within_cap(self):
        fd = FakeDispatch(max_dispatch=100)
        msgs = [self.make_msg("m1", "2026-07-26T10:00:00"),
                self.make_msg("m2", "2026-07-26T11:00:00")]
        result = fd._dispatch_messages(msgs)
        assert len(result) == 2

    def test_exceeds_cap_oldest_first(self):
        fd = FakeDispatch(max_dispatch=2)
        msgs = [self.make_msg("m3", "2026-07-26T13:00:00"),
                self.make_msg("m1", "2026-07-26T10:00:00"),
                self.make_msg("m2", "2026-07-26T11:00:00")]
        result = fd._dispatch_messages(msgs)
        assert len(result) == 2
        # 最旧的优先
        assert result[0].msg_id == "m1"
        assert result[1].msg_id == "m2"

    def test_cold_start_flag_passed(self):
        fd = FakeDispatch(max_dispatch=1)
        msgs = [self.make_msg("m1", "2026-07-26T10:00:00"),
                self.make_msg("m2", "2026-07-26T11:00:00")]
        result = fd._dispatch_messages(msgs, is_cold_start=True)
        assert len(result) == 1

    def test_no_cap(self):
        fd = FakeDispatch(max_dispatch=0)  # 0 表示不限
        msgs = [self.make_msg("m1", "2026-07-26T10:00:00")] * 20
        result = fd._dispatch_messages(msgs)
        assert len(result) == 20

    def test_none_timestamp(self):
        fd = FakeDispatch(max_dispatch=100)
        from src.models import Message
        base = {"chat_id": "c1", "chat_type": "group", "chat_name": "群",
                "sender_id": "s1", "sender_name": "张三", "msg_type": "text"}
        m1 = Message(msg_id="m1", content="test", timestamp=datetime(1970, 1, 1), **base)  # 远古时间
        m2 = Message(msg_id="m2", timestamp=datetime(2026, 7, 26, 10, 0, 0), content="test", **base)
        result = fd._dispatch_messages([m1, m2])
        assert len(result) == 2


# ============ get_backpressure_metrics ============

class TestGetBackpressureMetrics:
    def test_initial_state(self):
        fd = FakeDispatch()
        m = fd.get_backpressure_metrics()
        assert m["dispatched_total"] == 0
        assert m["deferred_total"] == 0
        assert m["cold_start_pending"] is True
        assert m["max_dispatch_per_cycle"] == 10

    def test_returns_all_keys(self):
        fd = FakeDispatch()
        m = fd.get_backpressure_metrics()
        expected_keys = {"dispatched_total", "deferred_total", "last_cycle_dispatched",
                         "last_cycle_deferred", "cold_start_pending",
                         "max_dispatch_per_cycle", "max_concurrent_replies"}
        assert set(m.keys()) == expected_keys


# ============ get_observability ============

class TestGetObservability:
    def test_initial_state(self):
        fd = FakeDispatch()
        m = fd.get_observability()
        assert m["last_poll_at"] == "2026-07-26T12:00:00"
        assert m["last_error"] is None
        assert m["last_error_at"] is None
        assert m["queue_depth"] == 0
        assert m["poll_count"] == 0

    def test_includes_backpressure_metrics(self):
        fd = FakeDispatch()
        m = fd.get_observability()
        assert "dispatched_total" in m
        assert "cold_start_pending" in m
