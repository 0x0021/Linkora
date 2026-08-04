"""回归：全模型限频(429)耗尽时 main 层的处理行为（⑤ 修订）。

需求：模型全失效时**不**向用户回复，仅打印日志并计入死信队列（DLQ），
由管理员在管理台手动重放；同时标记用户消息已处理，避免重复轮询刷屏。

- DLQ 开启：add_dead_letter 调用一次、_send_reply 不调用、_mark_msg_processed 调用。
- DLQ 关闭：add_dead_letter 不调用（仅日志）、_send_reply 不调用、_mark_msg_processed 仍调用。

注：此处直接单测抽取出的 _handle_rate_limit_exhausted 辅助方法，
避免跑整个 _handle_message_impl 的脆弱集成路径，又能精确覆盖三件事。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from main import LinkoraEngine
from src.llm.exceptions import LLMRateLimitExhaustedError


def _make_msg():
    return SimpleNamespace(
        msg_id="m_rate_1",
        chat_id="C1",
        chat_name="测试群",
        sender_id="U1",
        sender_name="测试人",
        content="hi",
        msg_type="text",
        raw={},
    )


def _make_app(dlq_enabled: bool):
    app = LinkoraEngine.__new__(LinkoraEngine)
    app.config = MagicMock()
    app.config.dead_letter.enabled = dlq_enabled
    app.store = MagicMock()
    app.poller = MagicMock()
    app._send_reply = MagicMock()
    return app


def test_rate_limit_exhausted_no_reply_but_dlq():
    app = _make_app(dlq_enabled=True)
    msg = _make_msg()
    exc = LLMRateLimitExhaustedError(
        "all models 429",
        original=RuntimeError("429 rate_limit"),
        stage="rate_limit",
    )
    app._handle_rate_limit_exhausted(msg, exc)
    # 关键：不向用户发送任何回复
    app._send_reply.assert_not_called()
    # 计入死信队列
    app.store._draft_repo.add_dead_letter.assert_called_once()
    # 标记已处理，避免重复轮询
    app.poller._mark_msg_processed.assert_called_once_with("m_rate_1", "C1")


def test_rate_limit_exhausted_dlq_disabled_logs_only():
    app = _make_app(dlq_enabled=False)
    msg = _make_msg()
    exc = LLMRateLimitExhaustedError("all models 429", stage="rate_limit")
    app._handle_rate_limit_exhausted(msg, exc)
    app._send_reply.assert_not_called()
    # 未启用 DLQ → 不落库，仅日志
    app.store._draft_repo.add_dead_letter.assert_not_called()
    # 仍标记已处理
    app.poller._mark_msg_processed.assert_called_once_with("m_rate_1", "C1")
