"""回归：HIGH#1 — 发送失败/被拦截后入站消息去重标记，避免每轮轮询重复处理。

- 敏感词命中 / 空回复 / 单聊 peer 缺失：属「永久跳过」，必须标记入站消息已处理，
  否则下一轮轮询重拉→重跑 LLM→反复入死信/反复发兜底刷屏。
- DWS 异常（瞬时失败）：不标记（允许重试），但置退避窗口避免每轮(5s)硬刷重发。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from main import LinkoraEngine


def _msg(**kw):
    base = dict(msg_id="m1", chat_id="C1", chat_name="测试", sender_id="U1",
               sender_name="人", content="hi", msg_type="text", chat_type="single", raw={})
    base.update(kw)
    return SimpleNamespace(**base)


def _make_app():
    app = LinkoraEngine.__new__(LinkoraEngine)
    app.store = MagicMock()
    app._active_ctx.reply_semaphore = threading.Semaphore(1)
    app.poller = MagicMock()
    app.dws = MagicMock()
    app.config = MagicMock()
    app.config.poller.reply_cooldown_seconds = 0
    app.config.poller.graceful_fallback_msg_types = []
    app.config.safety.default_fallback = ""
    app.config.safety.media_fallback_text = ""
    app.config.dws.dry_run = True
    app.config.dws.cli_path = "dws"
    app._send_backoff_until = {}
    app._replying_lock = threading.Lock()
    app._replying_chats = set()
    app._reply_rate_limited_until = 0.0  # __init__ 设置，但本 fixture 用 __new__ 绕过，需手动补齐
    return app


def test_sensitive_word_marks_inbound_deduped():
    app = _make_app()
    app._filter_sensitive_words = lambda text: None
    assert app._send_reply(_msg(), "badword") is False
    app.store._conversation_repo.update_last_replied_msg_id.assert_called_once_with("C1", "m1")
    app.poller._mark_msg_processed.assert_called_once_with("m1", "C1")
    app.store._draft_repo.add_dead_letter.assert_called_once()


def test_empty_content_marks_inbound_deduped():
    app = _make_app()
    app._filter_sensitive_words = lambda text: ""  # 过滤后为空
    assert app._send_reply(_msg(), "   ") is False
    app.store._conversation_repo.update_last_replied_msg_id.assert_called_once_with("C1", "m1")
    app.poller._mark_msg_processed.assert_called_once_with("m1", "C1")


def test_peer_empty_marks_inbound_deduped():
    app = _make_app()
    app._filter_sensitive_words = lambda text: text
    app.store._conversation_repo.get_conversation= lambda *a, **k: {}
    app.poller._resolve_single_chat_peer = lambda *a, **k: None
    assert app._send_reply(_msg(chat_type="single", sender_id=""), "hello") is False
    app.poller._mark_msg_processed.assert_called_once_with("m1", "C1")


def test_dws_exception_sets_backoff():
    app = _make_app()
    app._filter_sensitive_words = lambda text: text
    app.store._conversation_repo.get_conversation= lambda *a, **k: {}
    # 群聊发送优先走原生引用回复（chat_message_reply）；原生失败（fallback_to_send=False）
    # 回落到普通 chat_message_send，两者都失败才置退避。故两个接口都需抛异常。
    app.dws.chat_message_reply.side_effect = RuntimeError("dws down")
    app.dws.chat_message_send.side_effect = RuntimeError("dws down")
    msg = _msg(chat_type="group")
    assert app._send_reply(msg, "hello") is False
    assert app.dws.chat_message_reply.call_count == 1
    assert app.dws.chat_message_send.call_count == 1
    # 退避期内立即再次发送应被拦截，不再调用 DWS
    assert app._send_reply(msg, "hello") is False
    assert app.dws.chat_message_reply.call_count == 1
    assert app.dws.chat_message_send.call_count == 1
