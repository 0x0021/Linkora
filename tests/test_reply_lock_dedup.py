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
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from main import LinkoraEngine
from src.models import Message
from src.platform.message_loop import _is_same_physical_message


def _make_bare_app():
    """裸实例，只装配防抖派发所需属性，不触发完整初始化。"""
    app = LinkoraEngine.__new__(LinkoraEngine)
    app._pending_messages = {}
    app._pending_timers = {}
    app._pending_platform = {}
    app._timer_lock = threading.Lock()
    app._pending_first_seen = {}
    app._pending_incomplete_wait = {}
    # 「不完整消息延后」分支会累加 metrics，裸实例需一并装配，否则内容被判为
    # 不完整时抛 AttributeError（与去重逻辑无关的 fixture 缺口）
    app._metrics_lock = threading.Lock()
    app._incomplete_delay_count = 0
    app._incomplete_extra_sec = 0.0
    app._handle_message_impl = MagicMock()
    # _process_pending_messages 末尾会调用 poller.get_image_path（图片路径解析）
    app.poller = MagicMock(get_image_path=MagicMock(return_value=""))
    poller_cfg = types.SimpleNamespace(reply_cooldown_seconds=0.05)
    app.config = types.SimpleNamespace(poller=poller_cfg)
    return app


def _msg(msg_id: str, chat_id: str, sender_id: str, content: str,
         timestamp: datetime | None = None) -> Message:
    return Message(
        msg_id=msg_id,
        chat_id=chat_id,
        chat_type="single",
        chat_name="张三",
        sender_id=sender_id,
        sender_name="张三",
        content=content,
        msg_type="text",
        timestamp=timestamp or datetime.now(),
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


class TestRealResendNotSwallowed:
    """用户真实连发相同内容不得被内容去重吞掉。

    线上实证（2026-08-09 10:58/10:59，会话 cidBOuwoo7UD…）：同一位用户在 32 秒内
    发了两条一字不差的消息，openMessageId 分别为 msgTyfifNsDVb9hPaY9pzXssA==
    (ts=10:58:31) 与 msgsCYr1SZmTcofwLdXP8rVbg== (ts=10:59:03)——两条**不同的物理
    消息**。旧的纯内容去重把第二条判成重复投递直接 return，AI 完全看不到用户又催
    了一次。「在吗」…「在吗」这类催促在真实聊天里很常见，属于静默丢语境。

    修复后判据 = 内容相同 **且** 服务端时间接近（<=2s）才算重复投递。
    """

    def test_user_resend_after_32s_both_queued(self):
        """复刻线上案例：同会话内 32 秒后连发同样内容，两条都必须入队。"""
        app = _make_bare_app()
        t0 = datetime(2026, 8, 9, 10, 58, 31)
        content = "合思商场，有好几位实习生反馈手机号码绑定不了"
        first = _msg("msgTyfifNsDVb9hPaY9pzXssA==", "cidBOuwoo7UD", "u-cdh", content, t0)
        second = _msg("msgsCYr1SZmTcofwLdXP8rVbg==", "cidBOuwoo7UD", "u-cdh", content,
                      t0 + timedelta(seconds=32))

        app.handle_message(first)
        app.handle_message(second)

        total = sum(len(v) for v in app._pending_messages.values())
        assert total == 2, f"用户真实连发被吞：待处理数={total}（期望 2）"
        for t in app._pending_timers.values():
            t.cancel()

    def test_cross_key_resend_not_swallowed(self):
        """跨通道去重分支同样不得吞掉真实连发（key 不同、chat_id 相同）。"""
        app = _make_bare_app()
        t0 = datetime(2026, 8, 9, 10, 58, 31)
        first = _msg("m1", "cidX", "u-a", "在吗", t0)
        second = _msg("m2", "cidX", "u-b", "在吗", t0 + timedelta(seconds=20))

        app.handle_message(first)
        app.handle_message(second)

        total = sum(len(v) for v in app._pending_messages.values())
        assert total == 2, f"跨通道分支吞掉真实连发：待处理数={total}（期望 2）"
        for t in app._pending_timers.values():
            t.cancel()

    def test_same_physical_message_still_deduped(self):
        """回归保护：同一条物理消息（时间戳一致、msg_id 不同）仍须去重。"""
        app = _make_bare_app()
        t0 = datetime(2026, 8, 9, 10, 58, 31)
        # 两条路径对同一物理消息生成不同 msg_id，但 timestamp 同取服务端 createTime
        a = _msg("m-listall", "cidY", "u-a", "公司打印机怎么连？", t0)
        b = _msg("m-percon", "cidY", "u-b", "公司打印机怎么连？", t0)

        app.handle_message(a)
        app.handle_message(b)

        total = sum(len(v) for v in app._pending_messages.values())
        assert total == 1, f"同一物理消息未去重：待处理数={total}（期望 1）"
        for t in app._pending_timers.values():
            t.cancel()


class TestIsSamePhysicalMessage:
    """_is_same_physical_message 判据边界。"""

    @staticmethod
    def _m(content, ts):
        return types.SimpleNamespace(content=content, timestamp=ts)

    def test_different_content_never_same(self):
        t = datetime(2026, 8, 9, 10, 0, 0)
        assert _is_same_physical_message(self._m("你好", t), self._m("在吗", t)) is False

    def test_identical_timestamp_is_same(self):
        t = datetime(2026, 8, 9, 10, 0, 0)
        assert _is_same_physical_message(self._m("在吗", t), self._m("在吗", t)) is True

    def test_within_tolerance_is_same(self):
        t = datetime(2026, 8, 9, 10, 0, 0)
        assert _is_same_physical_message(self._m("在吗", t),
                                         self._m("在吗", t + timedelta(seconds=1))) is True

    def test_beyond_tolerance_is_resend(self):
        t = datetime(2026, 8, 9, 10, 0, 0)
        assert _is_same_physical_message(self._m("在吗", t),
                                         self._m("在吗", t + timedelta(seconds=8))) is False

    def test_missing_timestamp_falls_back_to_dedup(self):
        """时间戳缺失时保守去重，宁可合并也不冒重复回复的风险。"""
        t = datetime(2026, 8, 9, 10, 0, 0)
        assert _is_same_physical_message(self._m("在吗", None), self._m("在吗", t)) is True

    def test_tz_mismatch_falls_back_to_dedup(self):
        """tz-aware 与 naive 相减抛 TypeError，须退回保守去重而非崩溃。"""
        naive = datetime(2026, 8, 9, 10, 0, 0)
        aware = datetime(2026, 8, 9, 10, 0, 0, tzinfo=timezone.utc)
        assert _is_same_physical_message(self._m("在吗", naive), self._m("在吗", aware)) is True
