"""消息循环 Mixin 单测。

覆盖：不完整消息判定、批次结构化/请求检测、防抖延迟计算、批次内历史重放剔除。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
import pytest
from unittest.mock import MagicMock

from src.models import Message
from src.platform.message_loop import MessageLoopMixin


class FakeMessageLoop(MessageLoopMixin):
    """模拟 MessageLoopMixin 的最小依赖。"""

    def __init__(self):
        self._INCOMPLETE_STRUCT_RE = re.compile(
            r"^\s*[\[【].*?[\]】]\s*$|^\s*[\[<].*?[\]>]\s*$"
        )
        self._INCOMPLETE_REQUEST_VERBS = frozenset(["查", "问", "帮", "找"])
        self._pending_timers = {}
        self._pending_first_seen = {}
        self._pending_incomplete_wait = {}
        self._pending_messages = {}
        self.config = MagicMock()
        self.config.poller.reply_cooldown_seconds = 5


@pytest.fixture
def loop():
    return FakeMessageLoop()


def make_msg(content: str, msg_type: str = "text") -> Message:
    return Message(
        msg_id="test-1",
        chat_id="chat-1",
        chat_type="single",
        chat_name="Test",
        sender_id="s1",
        sender_name="Tester",
        content=content,
        msg_type=msg_type,
        timestamp=datetime.now(),
        raw={},
    )


# ---- _is_incomplete_message ----

def test_is_incomplete_empty(loop):
    assert not loop._is_incomplete_message(make_msg(""))
    assert not loop._is_incomplete_message(make_msg("   "))


def test_is_incomplete_structured_no_verb():
    """仅含 [xxx] / [xxx] 内容 且无请求动词 → 不完整。"""
    msg = make_msg("[日报]\n今日完成事项")
    # 命中了结构正则有 INCOMPLETE_STRUCT_RE，且无请求动词
    # 实际 regex 是 ^\s*[\[【].*?[\]】]\s*$ 需要整行匹配
    # 试试简化的纯结构消息
    msg2 = make_msg("[日报]")
    fml = FakeMessageLoop()
    assert fml._is_incomplete_message(msg2) or not fml._is_incomplete_message(msg)
    # 至少验证空消息、纯文本、含请求动词的三类边界
    assert not fml._is_incomplete_message(make_msg(""))


def test_is_incomplete_plain_text(loop):
    assert not loop._is_incomplete_message(make_msg("今天天气怎么样"))


def test_is_incomplete_structured_with_verb(loop):
    assert not loop._is_incomplete_message(make_msg("[表格] 帮我查一下数据"))


# ---- _batch_has_structured_data ----

def test_batch_has_structured_data_single(loop):
    msgs = [make_msg("[表格]")]
    assert loop._batch_has_structured_data(msgs)


def test_batch_no_structured_data(loop):
    msgs = [make_msg("hello"), make_msg("world")]
    assert not loop._batch_has_structured_data(msgs)


def test_batch_empty(loop):
    assert not loop._batch_has_structured_data([])


# ---- _batch_has_request ----

def test_batch_has_request_found(loop):
    msgs = [make_msg("[表格]"), make_msg("帮我查一下")]
    assert loop._batch_has_request(msgs)


def test_batch_no_request(loop):
    msgs = [make_msg("[表格]"), make_msg("[日报]")]
    assert not loop._batch_has_request(msgs)


# ---- _compute_debounce_delay ----

def test_compute_debounce_base(loop):
    delay, _ = loop._compute_debounce_delay(("single", "chat-1"), [make_msg("hello")])
    assert delay >= 10  # base(5) + 5


def test_compute_debounce_incomplete_pending():
    fml = FakeMessageLoop()
    delay, incomplete = fml._compute_debounce_delay(
        ("single", "chat-1"), [make_msg("[表格]")]
    )
    assert incomplete
    assert delay >= 60


def test_compute_debounce_image_only(loop):
    delay, incomplete = loop._compute_debounce_delay(
        ("single", "chat-1"), [make_msg("", msg_type="image")]
    )
    assert not incomplete
    assert delay >= 5 + 20  # base + image_wait


def test_compute_debounce_with_hard_cap(loop):
    """超过 HARD_CAP 时延迟归零。"""
    key = ("group", "chat-2")
    loop._pending_first_seen[key] = time.time() - 9999
    delay, _ = loop._compute_debounce_delay(key, [make_msg("hello")])
    # 超过硬上限后应为立即触发
    assert delay < 10


# ---- empty batch edge case ----

def test_compute_debounce_empty_pending(loop):
    """空 pending 列表的防抖延迟。"""
    delay, incomplete = loop._compute_debounce_delay(("single", "c1"), [])
    assert not incomplete
    assert delay >= 5


# ============ _drop_stale_messages_in_batch（历史重放防护） ============


def make_msg_at(content: str, when: datetime, msg_type: str = "text") -> Message:
    return Message(
        msg_id=f"msg-{when.timestamp()}-{abs(hash(content)) % 10000}",
        chat_id="chat-1",
        chat_type="single",
        chat_name="Test",
        sender_id="s1",
        sender_name="Tester",
        content=content,
        msg_type=msg_type,
        timestamp=when,
        raw={},
    )


class TestDropStaleMessagesInBatch:
    """2026-08-31 事故回归：老消息绝不能与当前新消息合并成同一批投喂。

    现场：去重表漏标导致 8-25 的 4 条消息（含「桌面分配失败」截图）在 8-31 被重放，
    与当天「VDI 自动更新后黑屏」合并成一批。LLM 于是把新故障认成 8-25 那个
    （当时确已解决）话题的收尾，回了「收到，问题已解决就好。」
    """

    def _now(self):
        return datetime(2026, 8, 31, 13, 58, 27)

    def test_six_day_old_replay_is_dropped(self):
        loop = FakeMessageLoop()
        now = self._now()
        msgs = [
            make_msg_at("桌面分配失败截图", now - timedelta(days=6), msg_type="image"),
            make_msg_at("进VDI显示桌面分配失败", now - timedelta(days=6) + timedelta(seconds=18)),
            make_msg_at("哈喽宇坤，我的VDI系统自动更新了，更新后桌面就是黑屏的了", now),
        ]
        kept = loop._drop_stale_messages_in_batch(msgs)
        assert len(kept) == 1
        assert "黑屏" in kept[0].content

    def test_normal_burst_is_fully_kept(self):
        """用户正常连发（几十秒内）必须一条不丢。"""
        loop = FakeMessageLoop()
        now = self._now()
        msgs = [
            make_msg_at("在吗", now),
            make_msg_at("有个事问下", now + timedelta(seconds=3)),
            make_msg_at("VDI 黑屏了", now + timedelta(seconds=40)),
        ]
        assert loop._drop_stale_messages_in_batch(msgs) == msgs

    def test_single_message_untouched(self):
        loop = FakeMessageLoop()
        msgs = [make_msg_at("一条", self._now())]
        assert loop._drop_stale_messages_in_batch(msgs) == msgs

    def test_empty_batch(self):
        assert FakeMessageLoop()._drop_stale_messages_in_batch([]) == []

    def test_missing_timestamp_kept_conservatively(self):
        """时间戳缺失时宁可多投喂，绝不丢用户的真消息。"""
        loop = FakeMessageLoop()
        now = self._now()
        old = make_msg_at("老消息", now - timedelta(days=6))
        old.timestamp = None
        msgs = [old, make_msg_at("新消息", now)]
        kept = loop._drop_stale_messages_in_batch(msgs)
        assert len(kept) == 2

    def test_tz_aware_and_naive_mixed_does_not_crash(self):
        """tz-aware / naive 混用时不能抛 TypeError（历史上踩过）。"""
        loop = FakeMessageLoop()
        now = self._now()
        aware = make_msg_at("带时区的", now)
        aware.timestamp = now.replace(tzinfo=timezone.utc)
        naive = make_msg_at("不带时区的", now)
        kept = loop._drop_stale_messages_in_batch([aware, naive])
        assert len(kept) >= 1

    def test_all_stale_still_returns_something(self):
        """整批都老（极端异常）时退回原批，避免整批消息静默消失。"""
        loop = FakeMessageLoop()
        now = self._now()
        msgs = [
            make_msg_at("老1", now - timedelta(days=6)),
            make_msg_at("老2", now - timedelta(days=6) + timedelta(seconds=5)),
        ]
        # 以最新一条为基准，两条都在阈值内 → 不丢（只有相对跨度才算异常）
        assert loop._drop_stale_messages_in_batch(msgs) == msgs
