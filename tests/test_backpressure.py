"""P1-E：积压回放背压测试。

覆盖：
1. 空输入 → 返回空列表（不派发、不报错）。
2. 单轮限速：消息数 > max_dispatch_per_cycle 时，只返回前 cap 条（最旧优先）。
3. 最旧优先：返回列表按 timestamp 升序排列。
4. 不限速（cap=0）→ 返回全部。
5. get_backpressure_metrics 返回结构正确（字段齐全）。
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta

from src.models import Message
from src.poller import MessagePoller


def _make_poller(cap: int = 30):
    """构造最小 MessagePoller 裸实例，仅装配 P1-E 相关属性。"""
    p = MessagePoller.__new__(MessagePoller)
    p.config = types.SimpleNamespace(
        max_dispatch_per_cycle=cap,
        max_concurrent_replies=4,
    )
    # get_backpressure_metrics 读取的计数器（__init__ 中赋值，裸实例需手动补）
    p._dispatch_total = 0
    p._deferred_total = 0
    p._last_cycle_dispatched = 0
    p._last_cycle_deferred = 0
    p._first_poll = True
    return p


def _msg(msg_id: str, ts: datetime) -> Message:
    return Message(
        msg_id=msg_id,
        chat_id="c1",
        chat_type="single",
        chat_name="测试会话",
        sender_id="u1",
        sender_name="张三",
        content="hello",
        msg_type="text",
        timestamp=ts,
        raw={},
    )


def test_empty_input_returns_empty():
    p = _make_poller()
    assert p._dispatch_messages([]) == []


def test_cap_respected_and_oldest_first():
    p = _make_poller(cap=2)
    base = datetime(2026, 7, 13, 10, 0, 0)
    # 乱序时间戳
    m_old = _msg("m3", base - timedelta(minutes=2))
    m_new = _msg("m1", base)
    m_mid = _msg("m2", base - timedelta(minutes=1))
    out = p._dispatch_messages([m_new, m_mid, m_old], is_cold_start=True)
    assert len(out) == 2, "应受 cap=2 限制"
    # 最旧优先：第一条必须是 m3（最早时间戳）
    assert out[0].msg_id == "m3" and out[1].msg_id == "m2", "应最旧优先（升序）"


def test_no_cap_returns_all_sorted():
    p = _make_poller(cap=0)  # 0 = 不限制
    base = datetime(2026, 7, 13, 10, 0, 0)
    msgs = [
        _msg("m1", base),
        _msg("m2", base - timedelta(minutes=5)),
        _msg("m3", base - timedelta(minutes=2)),
    ]
    out = p._dispatch_messages(msgs)
    assert len(out) == 3
    assert [m.msg_id for m in out] == ["m2", "m3", "m1"], "应全部按升序返回"


def test_under_cap_returns_all():
    p = _make_poller(cap=10)
    base = datetime(2026, 7, 13, 10, 0, 0)
    msgs = [_msg(f"m{i}", base - timedelta(minutes=i)) for i in range(3)]
    out = p._dispatch_messages(msgs)
    assert len(out) == 3
    assert out[0].msg_id == "m2"  # 最旧


def test_metrics_structure():
    p = _make_poller(cap=30)
    p._dispatch_total = 100
    p._deferred_total = 20
    p._last_cycle_dispatched = 5
    p._last_cycle_deferred = 3
    p._first_poll = False
    m = p.get_backpressure_metrics()
    assert m["dispatched_total"] == 100
    assert m["deferred_total"] == 20
    assert m["last_cycle_dispatched"] == 5
    assert m["last_cycle_deferred"] == 3
    assert m["cold_start_pending"] is False
    assert m["max_dispatch_per_cycle"] == 30
    assert m["max_concurrent_replies"] == 4
