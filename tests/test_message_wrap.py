"""wrap_incoming_message 单测：truncate + 群前缀 + 发言人归属三层包装。

零依赖：只传 Message + truncate_fn 桩函数。

发言人归属（2026-08 修复）：
多人会话必须把「谁说的」写进消息文本，否则 LLM 看不到发言人、
会把不同人的话合并成一条无署名文本，从而无法判断「哪个话题已闭环」
（线上事故：对方说「老数据先不用了」AI 仍追问工号手机号）。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.message_wrap import wrap_incoming_message
from src.models import Message


def _make_msg(content: str, *, chat_type: str = "p2p", chat_name: str = "tester",
             sender_name: str = "user_a") -> Message:
    return Message(
        msg_id="m1",
        chat_id="c1",
        chat_type=chat_type,
        chat_name=chat_name,
        sender_id="u1",
        sender_name=sender_name,
        content=content,
        msg_type="text",
        timestamp=datetime.now(),
        role="user",
    )


class _Recorder:
    """记录 truncate 调用的桩函数。"""
    def __init__(self):
        self.calls = []

    def __call__(self, content: str, max_chars: int = 500) -> str:
        self.calls.append((content, max_chars))
        return content[:max_chars]


class TestWrapBasic:
    """基础场景：p2p 文本消息。"""

    def test_short_text_exposes_sender(self):
        """p2p 短文本带发言人前缀返回（2026-08 修复）。"""
        rec = _Recorder()
        msg = _make_msg("你好世界")
        out = wrap_incoming_message(msg, truncate_fn=rec, max_chars=1000)
        assert out == "user_a：你好世界"
        assert len(rec.calls) == 1
        assert rec.calls[0] == ("你好世界", 1000)

    def test_long_text_truncated_then_prefixed(self):
        """超 max_chars 文本先截断再加发言人前缀。"""
        rec = _Recorder()
        msg = _make_msg("a" * 2000)
        out = wrap_incoming_message(msg, truncate_fn=rec, max_chars=1000)
        assert out == f"user_a：{'a' * 1000}"
        assert len(out) == 1000 + len("user_a：")


class TestWrapGroupPrefix:
    """群消息加 [群]chat_name 前缀 + 发言人。"""

    def test_group_message_gets_chat_and_sender(self):
        rec = _Recorder()
        msg = _make_msg("hi", chat_type="group", chat_name="研发群")
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "[群]研发群 user_a：hi"

    def test_group_no_sender_name(self):
        """群消息无 sender_name 时只保留群前缀（不崩、不留空发言人）。"""
        rec = _Recorder()
        msg = _make_msg("hi", chat_type="group", chat_name="研发群", sender_name=None)
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "[群]研发群：hi"

    def test_group_long_truncated_first(self):
        rec = _Recorder()
        msg = _make_msg("x" * 2000, chat_type="group", chat_name="g")
        out = wrap_incoming_message(msg, truncate_fn=rec, max_chars=500)
        assert out == f"[群]g user_a：{'x' * 500}"

    def test_group_no_chat_name(self):
        rec = _Recorder()
        msg = _make_msg("hi", chat_type="group", sender_name=None)
        msg.chat_name = None
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "[群]None：hi"  # chat_name=None 保留原行为


class TestWrapEdgeCases:
    """边界：非字符串 content / max_chars 默认值 / 无发言人。"""

    def test_non_string_content_unchanged(self):
        rec = _Recorder()
        msg = _make_msg("dummy")
        msg.content = {"type": "image", "url": "..."}
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == {"type": "image", "url": "..."}
        assert rec.calls == []

    def test_p2p_no_sender_name_no_prefix(self):
        """p2p 且 sender_name 为 None 时原样返回（不强行加空前缀）。"""
        rec = _Recorder()
        msg = _make_msg("hi", sender_name=None)
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "hi"

    def test_default_max_chars_is_1000(self):
        rec = _Recorder()
        msg = _make_msg("a" * 1500)
        wrap_incoming_message(msg, truncate_fn=rec)
        assert rec.calls[0][1] == 1000

    def test_custom_max_chars_passed_through(self):
        rec = _Recorder()
        msg = _make_msg("a" * 200)
        wrap_incoming_message(msg, truncate_fn=rec, max_chars=50)
        assert rec.calls[0][1] == 50

    def test_truncate_failure_does_not_crash_wrap(self):
        def _boom(content, max_chars=500):
            raise ValueError("simulated")
        msg = _make_msg("hi")
        try:
            wrap_incoming_message(msg, truncate_fn=_boom)
        except ValueError as e:
            assert "simulated" in str(e)
        else:
            raise AssertionError("expected ValueError to propagate")
