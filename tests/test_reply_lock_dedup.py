"""回复锁 / 双轮询器重复投递回归测试。

复现 2026-08-02 线上事故：同一条物理消息（「公司打印机怎么连？」）被 list-all 与
wecom 两个轮询器投递两次，且因两者生成的 (chat_id, sender_id) key 不同，防抖建出
两个独立缓冲区 + 两个定时器。两个定时器先后触发，先到的持锁处理中、后到的撞回复锁
被 `return` 静默丢弃 → 用户消息永久丢失，日志却显示「正在回复中」。

本测试验证：
1. 跨通道去重：同一 chat_id 下相同内容（msg_id / key 不同）只入队一次，不建第二个定时器。
2. 整合定时器触发后，消息只被派发处理一次（无重复、无丢失）。
"""
from __future__ import annotations

import threading
import types
from datetime import datetime
from unittest.mock import MagicMock

from main import LinkoraEngine
from src.models import Message


def _make_bare_app():
    """裸实例，只装配防抖派发所需属性，不触发完整初始化。"""
    app = LinkoraEngine.__new__(LinkoraEngine)
    app._pending_messages = {}
    app._pending_timers = {}
    app._pending_platform = {}
    app._timer_lock = threading.Lock()
    app._pending_first_seen = {}
    app._pending_incomplete_wait = {}
    app._handle_message_impl = MagicMock()
    # _process_pending_messages 末尾会调用 poller.get_image_path（图片路径解析）
    app.poller = MagicMock(get_image_path=MagicMock(return_value=""))
    poller_cfg = types.SimpleNamespace(reply_cooldown_seconds=0.05)
    app.config = types.SimpleNamespace(poller=poller_cfg)
    return app


def _msg(msg_id: str, chat_id: str, sender_id: str, content: str) -> Message:
    return Message(
        msg_id=msg_id,
        chat_id=chat_id,
        chat_type="single",
        chat_name="张三",
        sender_id=sender_id,
        sender_name="张三",
        content=content,
        msg_type="text",
        timestamp=datetime.now(),
    )


def test_cross_poller_dedup_single_pending():
    """list-all 与 wecom 的 key 不同，但内容相同 → 合并为一条待处理。"""
    app = _make_bare_app()
    chat_id = "cidPrinter"
    content = "公司打印机怎么连？"
    # 模拟双轮询器：key 不同（sender_id 不同），msg_id 不同，内容相同
    m_listall = _msg("m-listall", chat_id, "u-listall", content)
    m_wecom = _msg("m-wecom", chat_id, "u-wecom", content)

    app.handle_message(m_listall)
    app.handle_message(m_wecom)

    # 断言：跨通道去重后，整个 _pending_messages 里该 chat_id 只有一条内容
    total_pending = sum(len(v) for v in app._pending_messages.values())
    assert total_pending == 1, f"重复投递未去重，待处理数={total_pending}"
    # 只应有一个定时器
    assert len(app._pending_timers) == 1, f"定时器数异常: {len(app._pending_timers)}"

    # 取消残留定时器，手动触发派发，验证仅处理一次
    for t in app._pending_timers.values():
        t.cancel()
    for key in list(app._pending_messages.keys()):
        app._process_pending_messages(key)

    app._handle_message_impl.assert_called_once()
    handled = app._handle_message_impl.call_args.args[0]
    assert handled.content == content


def test_same_content_different_chat_not_deduped():
    """不同 chat_id 的相同内容不应被跨通道去重误伤。"""
    app = _make_bare_app()
    c1 = _msg("a1", "chat-A", "u1", "你好")
    c2 = _msg("b1", "chat-B", "u1", "你好")
    app.handle_message(c1)
    app.handle_message(c2)
    total_pending = sum(len(v) for v in app._pending_messages.values())
    assert total_pending == 2, f"不同会话被错误去重: {total_pending}"
    for t in app._pending_timers.values():
        t.cancel()
