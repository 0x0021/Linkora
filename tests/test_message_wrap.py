"""wrap_incoming_message 单测：truncate + 群前缀两层包装。

零依赖：只传 Message + truncate_fn 桩函数。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.message_wrap import wrap_incoming_message
from src.models import Message


def _make_msg(content: str, *, chat_type: str = "p2p", chat_name: str = "tester") -> Message:
    return Message(
        msg_id="m1",
        chat_id="c1",
        chat_type=chat_type,
        chat_name=chat_name,
        sender_id="u1",
        sender_name="user_a",
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

    def test_short_text_not_truncated(self):
        """短文本原样返回。"""
        rec = _Recorder()
        msg = _make_msg("你好世界")
        out = wrap_incoming_message(msg, truncate_fn=rec, max_chars=1000)
        assert out == "你好世界"
        # truncate 一定被调用过一次（哪怕不截断），用于保持调用一致性
        assert len(rec.calls) == 1
        assert rec.calls[0] == ("你好世界", 1000)

    def test_long_text_truncated(self):
        """超 max_chars 文本被截断。"""
        rec = _Recorder()
        msg = _make_msg("a" * 2000)
        out = wrap_incoming_message(msg, truncate_fn=rec, max_chars=1000)
        assert len(out) == 1000
        assert out == "a" * 1000


class TestWrapGroupPrefix:
    """群消息加 [群]chat_name: 前缀。"""

    def test_group_message_gets_prefix(self):
        rec = _Recorder()
        msg = _make_msg("hi", chat_type="group", chat_name="研发群")
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "[群]研发群:hi"

    def test_p2p_no_prefix(self):
        rec = _Recorder()
        msg = _make_msg("hi", chat_type="p2p")
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "hi"

    def test_group_long_truncated_first(self):
        """群消息先 truncate 再加前缀（截断在文本层，不影响 [群] 前缀完整性）。"""
        rec = _Recorder()
        msg = _make_msg("x" * 2000, chat_type="group", chat_name="g")
        out = wrap_incoming_message(msg, truncate_fn=rec, max_chars=500)
        assert out == f"[群]g:{'x' * 500}"
        assert len(out) == 500 + len("[群]g:")

    def test_group_no_chat_name(self):
        """chat_name 为 None 时用空字符串（不崩）。"""
        rec = _Recorder()
        msg = _make_msg("hi", chat_type="group")
        msg.chat_name = None
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == "[群]None:hi"  # Message 字段允许 None，保留原行为


class TestWrapEdgeCases:
    """边界：非字符串 content / max_chars 默认值。"""

    def test_non_string_content_unchanged(self):
        """非字符串 content（如 dict 图片 marker）跳过 truncate。"""
        rec = _Recorder()
        msg = _make_msg("dummy")  # str
        msg.content = {"type": "image", "url": "..."}
        out = wrap_incoming_message(msg, truncate_fn=rec)
        assert out == {"type": "image", "url": "..."}
        assert rec.calls == []  # 非字符串不调 truncate

    def test_default_max_chars_is_1000(self):
        """未传 max_chars 时默认 1000（与拆分前一致）。"""
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
        """truncate_fn 抛异常时不崩溃（透传给调用方，不吞错）。"""
        def _boom(content, max_chars=500):
            raise ValueError("simulated")
        msg = _make_msg("hi")
        try:
            wrap_incoming_message(msg, truncate_fn=_boom)
        except ValueError as e:
            assert "simulated" in str(e)
        else:
            raise AssertionError("expected ValueError to propagate")
