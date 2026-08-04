"""回归：MED#2 — 泛化 except 不再把真代码错误伪装成「正常兜底」静默回复。

LLM 步骤抛出未预期异常（AttributeError/TypeError/ValueError 等）时：
- 不调用 _send_reply 发送 default_fallback（不误导用户以为已正常作答）；
- DLQ 开启时落死信，并标记入站消息已处理（防每轮轮询重复崩溃刷屏）。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import main
from main import LinkoraEngine


def _msg(**kw):
    base = dict(msg_id="m1", chat_id="C1", chat_name="测试", sender_id="U1",
               sender_name="人", content="hi", msg_type="text", chat_type="single", raw={})
    base.update(kw)
    return SimpleNamespace(**base)


def _make_handler_app(dlq_enabled: bool):
    app = LinkoraEngine.__new__(LinkoraEngine)
    app.store = MagicMock()
    app._active_ctx.reply_semaphore = threading.Semaphore(1)
    app.poller = MagicMock()
    app.dws = MagicMock()
    app._send_reply = MagicMock()  # 仅用于断言「未被调用」
    app.config = MagicMock()
    app.config.poller.reply_cooldown_seconds = 0
    app.config.poller.graceful_fallback_msg_types = []
    app.config.poller.reply_concurrency_timeout_seconds = 1
    app.config.safety.default_fallback = "兜底"
    app.config.safety.media_fallback_text = ""
    app.config.dws.dry_run = True
    app.config.dws.cli_path = "dws"
    app.config.dead_letter.enabled = dlq_enabled
    app._send_backoff_until = {}
    app._backoff_cleanup_counter = 0  # 对应 main.py __init__ 中的初始化（测试用 __new__ 绕过）
    app._bg_throttle = MagicMock()
    app._replying_lock = threading.Lock()
    app._replying_chats = set()
    # 前置检查全部放行，直达 LLM 步骤
    app._has_replied_after = lambda msg: False
    app._has_user_taken_over = lambda msg: False
    app._filter_sensitive_words = lambda text: text
    app.store._conversation_repo.get_last_reply_time= lambda *a, **k: None
    app.store._conversation_repo.get_conversation= lambda *a, **k: {}
    app.store._message_repo.get_conversation_history= lambda *a, **k: []
    app.rule_engine = MagicMock()
    app.rule_engine.check.return_value = SimpleNamespace(
        action="business", intent="business", reply_text=None, reason="")
    app.llm_agent = MagicMock()
    app.llm_agent.process_message.side_effect = ValueError("unexpected bug")
    main.tracker.record = MagicMock()
    return app


def test_unexpected_error_not_masked_as_fallback_dlq_enabled():
    app = _make_handler_app(dlq_enabled=True)
    app._handle_message_impl(_msg())
    # 未预期异常：不发送兜底回复（不伪装成正常）
    app._send_reply.assert_not_called()
    # 落死信
    app.store._draft_repo.add_dead_letter.assert_called_once()
    # 标记入站消息已处理
    app.poller._mark_msg_processed.assert_called_once_with("m1", "C1")


def test_unexpected_error_not_masked_as_fallback_dlq_disabled():
    app = _make_handler_app(dlq_enabled=False)
    app._handle_message_impl(_msg())
    app._send_reply.assert_not_called()
    app.store._draft_repo.add_dead_letter.assert_not_called()
    app.poller._mark_msg_processed.assert_called_once_with("m1", "C1")
