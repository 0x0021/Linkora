"""回复后延迟标记已读（mark_read_delay_seconds）行为测试。

覆盖：
- 默认 120 秒：回复发出后只挂 Timer，不立即 mark_read；Timer 触发才标记
- 0 秒：回复发出后立即标记（旧行为）
- 同会话窗口内二次回复：取消旧 Timer，旧回调不再标记
- mark_read_after_process=false 与非法 / 缺失配置值的处理
- _send_reply 集成：只有发送成功才调度，门控拦下 / 发送失败都不调度
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.platform.runtime import RuntimeMixin
from src.platform.runtime_reply_guard import ReplyGuardMixin


class _Msg(SimpleNamespace):
    msg_type = "text"
    content = "hi"
    raw = {}
    chat_id = "c1"
    chat_name = "peer"
    sender_name = "peer"
    sender_id = "peerId"
    msg_id = "m1"
    timestamp = None


def _guard(delay=120, mark_after=True):
    inst = ReplyGuardMixin()
    inst.config = SimpleNamespace(poller=SimpleNamespace(
        mark_read_delay_seconds=delay,
        mark_read_after_process=mark_after,
    ))
    inst.dws = MagicMock()
    inst._mark_read_before_reply = MagicMock()
    return inst


def test_default_delay_schedules_timer_and_marks_only_when_fired():
    rt = _guard()
    with patch("src.platform.runtime_reply_guard.threading.Timer") as timer_cls:
        timer = MagicMock()
        timer_cls.return_value = timer
        rt._schedule_mark_read_after_reply(_Msg())

    assert timer_cls.call_args.args[0] == 120
    timer.start.assert_called_once()
    rt._mark_read_before_reply.assert_not_called()

    callback = timer_cls.call_args.args[1]
    callback()
    rt._mark_read_before_reply.assert_called_once()


def test_zero_delay_marks_immediately_without_timer():
    rt = _guard(delay=0)
    with patch("src.platform.runtime_reply_guard.threading.Timer") as timer_cls:
        rt._schedule_mark_read_after_reply(_Msg())
    timer_cls.assert_not_called()
    rt._mark_read_before_reply.assert_called_once()


def test_real_mark_read_command_still_runs_on_zero_delay():
    rt = _guard(delay=0)
    del rt._mark_read_before_reply  # 回落到 mixin 的真实实现
    rt._schedule_mark_read_after_reply(_Msg())
    rt.dws.mark_read.assert_called_once_with(conversation_id="c1", message_id="m1")


def test_second_reply_cancels_previous_timer():
    rt = _guard()
    with patch("src.platform.runtime_reply_guard.threading.Timer") as timer_cls:
        timers = [MagicMock(), MagicMock()]
        timer_cls.side_effect = timers
        rt._schedule_mark_read_after_reply(_Msg())
        first_callback = timer_cls.call_args.args[1]
        rt._schedule_mark_read_after_reply(_Msg(msg_id="m2"))
        second_callback = timer_cls.call_args.args[1]

    timers[0].cancel.assert_called_once()
    first_callback()
    rt._mark_read_before_reply.assert_not_called()
    second_callback()
    rt._mark_read_before_reply.assert_called_once()


def test_no_schedule_when_mark_read_after_process_disabled():
    rt = _guard(mark_after=False)
    with patch("src.platform.runtime_reply_guard.threading.Timer") as timer_cls:
        rt._schedule_mark_read_after_reply(_Msg())
    timer_cls.assert_not_called()
    rt._mark_read_before_reply.assert_not_called()


def test_invalid_or_missing_delay_falls_back_to_120():
    assert _guard(delay="abc")._mark_read_delay_seconds() == 120
    assert _guard(delay=-5)._mark_read_delay_seconds() == 0
    inst = ReplyGuardMixin()
    inst.config = SimpleNamespace(poller=SimpleNamespace())
    assert inst._mark_read_delay_seconds() == 120


class _FakeRuntime(RuntimeMixin):
    """驱动真实 _send_reply 的最小宿主，只 stub 分发与门控相关方法。"""

    def __init__(self, should_reply=True, send_ok=True):
        self.config = SimpleNamespace(poller=SimpleNamespace(
            reply_send_min_interval=0.0,
            reply_send_rate_limit_backoff_seconds=60.0,
            reply_shard_limit=4000,
            suppress_when_owner_read=True,
            read_grace_seconds=0,
            mark_read_delay_seconds=0,
            mark_read_after_process=True,
        ))
        self.dws = MagicMock()
        self.dws.dry_run = False
        self.poller = MagicMock()
        self._last_reply_send_ts = 0.0
        self._reply_send_throttle_lock = threading.Lock()
        self._reply_rate_limited_until = 0.0
        self._send_backoff_until = {}
        self._reply_cooldown_active = lambda m: False
        self._filter_sensitive_words = lambda t: t
        self._prepare_outgoing_text = lambda f, m: ("title", f)
        self._schedule_mark_read_after_reply = MagicMock()
        self._mark_read_before_reply = MagicMock()
        self._dispatch_reply_send = MagicMock(return_value=(send_ok, {"result": {}}))
        self._record_reply_success = MagicMock()
        self._mark_inbound_processed = MagicMock()
        self._should_reply_now = lambda m: should_reply


def test_send_reply_schedules_only_after_success_and_never_before_send():
    rt = _FakeRuntime(should_reply=True, send_ok=True)
    assert rt._send_reply(_Msg(), "答复") is True
    rt._schedule_mark_read_after_reply.assert_called_once()
    rt._mark_read_before_reply.assert_not_called()


def test_send_reply_no_schedule_when_gate_blocks():
    rt = _FakeRuntime(should_reply=False)
    assert rt._send_reply(_Msg(), "答复") is False
    rt._schedule_mark_read_after_reply.assert_not_called()


def test_send_reply_no_schedule_when_send_fails():
    rt = _FakeRuntime(should_reply=True, send_ok=False)
    assert rt._send_reply(_Msg(), "答复") is False
    rt._schedule_mark_read_after_reply.assert_not_called()
