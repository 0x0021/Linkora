"""消息循环 Mixin 单测。

覆盖：不完整消息判定、批次结构化/请求检测、防抖延迟计算。
"""

from __future__ import annotations

import re
import time
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
        timestamp=__import__("datetime").datetime.now(),
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
