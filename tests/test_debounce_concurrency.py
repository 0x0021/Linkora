"""防抖缓冲区并发安全测试。

`_pending_messages` 被 poller 循环线程（handle_message 入队）和 Timer 守护线程
（_process_pending_messages 出队 pop）并发读写。此前入队的「去重+append」在
`_timer_lock` 之外，与出队 pop 存在竞态：poller 读完 pending[key] 后、append 前，
Timer 恰好 pop(key) → append 时 KeyError → 消息被 run_loop 兜底丢弃。

本测试并发 hammer 入队与出队，验证：
1. 全程不抛异常（无 KeyError）
2. 不丢消息（入队总数 == 出队累计 + 仍在缓冲区的数量）
"""
from __future__ import annotations

import threading
import types

from main import LinkoraEngine
from src.models import Message


def _make_bare_app():
    """用 __new__ 建裸实例，只装配防抖并发所需的属性，不触发完整初始化。"""
    app = LinkoraEngine.__new__(LinkoraEngine)
    app._pending_messages = {}
    app._pending_timers = {}
    app._timer_lock = threading.Lock()
    # P1-C：防抖「纯数据」批次监控状态（与 LinkoraEngine.__init__ 保持一致）
    app._pending_first_seen = {}
    app._pending_incomplete_wait = {}
    app._incomplete_delay_count = 0
    app._incomplete_extra_sec = 0.0
    app._incomplete_fired_with_request = 0
    app._incomplete_fired_without_request = 0
    # config.poller.reply_cooldown_seconds -> 让 Timer delay 足够长，测试期内不自触发
    poller_cfg = types.SimpleNamespace(reply_cooldown_seconds=3600)
    app.config = types.SimpleNamespace(poller=poller_cfg)
    return app


def _msg(i: int, chat_id: str = "c1", sender_id: str = "u1") -> Message:
    from datetime import datetime
    return Message(
        msg_id=f"m{i}",
        chat_id=chat_id,
        chat_type="single",
        chat_name="测试",
        sender_id=sender_id,
        sender_name="张三",
        content=f"消息{i}",
        msg_type="text",
        timestamp=datetime.now(),
    )


def test_concurrent_enqueue_dequeue_no_crash_no_loss():
    app = _make_bare_app()
    key = ("c1", "u1")
    N = 500
    dequeued: list = []
    dequeue_lock = threading.Lock()
    errors: list = []

    def enqueue():
        for i in range(N):
            try:
                app.handle_message(_msg(i))
            except Exception as e:  # noqa: BLE001
                errors.append(("enqueue", repr(e)))

    def dequeue():
        # 模拟 Timer 线程：在锁内 pop（与修复后的 _process_pending_messages 一致）
        for _ in range(N * 2):
            try:
                with app._timer_lock:
                    if key in app._pending_messages:
                        msgs = app._pending_messages.pop(key)
                    else:
                        msgs = []
                if msgs:
                    with dequeue_lock:
                        dequeued.extend(msgs)
            except Exception as e:  # noqa: BLE001
                errors.append(("dequeue", repr(e)))

    t1 = threading.Thread(target=enqueue)
    t2 = threading.Thread(target=dequeue)
    t1.start(); t2.start()
    t1.join(); t2.join()

    # 取消所有残留 Timer，避免测试后回调触发
    for t in app._pending_timers.values():
        t.cancel()

    # 收尾：把缓冲区剩余的也算进来
    remaining = app._pending_messages.get(key, [])

    assert not errors, f"并发出现异常: {errors[:5]}"
    total_seen = len(dequeued) + len(remaining)
    # msg_id 去重后应恰好 N 条（m0..m{N-1}），既不丢也不重
    all_ids = {m.msg_id for m in dequeued} | {m.msg_id for m in remaining}
    assert len(all_ids) == N, f"消息丢失/重复: 去重后 {len(all_ids)} != {N}"
    assert total_seen == N, f"消息计数异常: {total_seen} != {N}"


def test_dedup_same_msg_id():
    """同一 msg_id 重复入队应被去重（list-all 与 per-conversation 双路径场景）。"""
    app = _make_bare_app()
    key = ("c1", "u1")
    app.handle_message(_msg(1))
    app.handle_message(_msg(1))  # 重复
    app.handle_message(_msg(2))
    for t in app._pending_timers.values():
        t.cancel()
    assert len(app._pending_messages[key]) == 2
