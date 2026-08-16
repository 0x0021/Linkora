"""入站门控终态标记回归测试（T-B2）。

验证 _handle_message_with_rid 的三个早退分支对「终态标记」的处理：
- 「消息已有回复」(命中 _has_replied_after) → 标记入站已处理；
- 「人工已接管」(命中 _has_user_taken_over) → 标记入站已处理；
- 「真人在场」(命中 _is_owner_present)     → 不标记（真人在场是时间窗态，标记会
  让 _has_replied_after 永久为真，破坏「真人离场超时后 AI 接管」）。

复用 test_reply_gate_sendtime._PrefilterHost 同款最小宿主，驱动真实的
_handle_message_with_rid 前置过滤路径，仅用 lambda 隔离各子闸。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.platform.runtime_inbound import InboundMixin


class _TerminalMarkHost(InboundMixin):
    """最小宿主：驱动真实的 _handle_message_with_rid 前置过滤路径。"""

    @property
    def _active_ctx(self):
        return self._active_ctx_ns

    def __init__(self):
        self.config = SimpleNamespace(
            poller=SimpleNamespace(
                skip_msg_types=[],
                graceful_fallback_msg_types=[],
                skip_notification_patterns=[],
                skip_notification_sender_ids=[],
                reply_cooldown_seconds=0,
                reply_concurrency_timeout_seconds=30,
                owner_present_cooldown_seconds=600,
                suppress_when_owner_read=True,
                unread_conversation_count=20,
            ),
            safety=SimpleNamespace(media_fallback_text=""),
        )
        self._active_ctx_ns = SimpleNamespace(reply_semaphore=threading.Semaphore(1))
        self._replying_lock = threading.Lock()
        self._replying_chats: dict = {}
        self._replying_since: dict = {}
        self._reply_lock_retries: dict = {}
        self._metrics_lock = threading.Lock()
        self._backoff_cleanup_counter = 0
        self._bg_throttle = MagicMock()
        self.current_user_name = "owner"
        self.dws = MagicMock()
        self.rule_engine = MagicMock()
        self.rule_engine.check.return_value = SimpleNamespace(action="none")
        # 默认全部放行
        self._is_message_from_self = lambda m: False
        self._has_replied_after = MagicMock(return_value=False)
        self._has_user_taken_over = MagicMock(return_value=False)
        self._is_owner_present = MagicMock(return_value=False)
        self._owner_conversation_is_read = lambda m: False
        # 入站链路其余分支桩
        self._should_skip_inbound = MagicMock(return_value=False)
        self._reply_cooldown_active = MagicMock(return_value=False)
        self._handle_oa_approval_urge = MagicMock(return_value=False)
        self._apply_rule_result = MagicMock(return_value=False)
        # 被测路径桩
        self._process_llm_reply = MagicMock()
        self._mark_inbound_processed = MagicMock()
        self._cleanup_backoff = MagicMock()


def _msg():
    return SimpleNamespace(
        msg_type="text",
        content="hi",
        raw={},
        chat_id="c1",
        chat_name="peer",
        sender_name="peer",
        sender_id="peerId",
        msg_id=None,
        timestamp=None,
    )


def test_reply_already_sent_marks_processed():
    """「消息已有回复」分支：命中 _has_replied_after → 标记入站已处理，跳过 LLM。"""
    host = _TerminalMarkHost()
    host._has_replied_after = lambda m: True
    host._handle_message_with_rid(_msg(), "rid-replied")
    host._process_llm_reply.assert_not_called()
    host._mark_inbound_processed.assert_called_once()


def test_user_takeover_marks_processed():
    """「人工已接管」分支：命中 _has_user_taken_over → 标记入站已处理，跳过 LLM。"""
    host = _TerminalMarkHost()
    host._has_replied_after = lambda m: False
    host._has_user_taken_over = lambda m: True
    host._handle_message_with_rid(_msg(), "rid-takeover")
    host._process_llm_reply.assert_not_called()
    host._mark_inbound_processed.assert_called_once()


def test_owner_present_does_not_mark_processed():
    """「真人在场」分支：命中 _is_owner_present → 不标记入站已处理（保住离场后接管）。"""
    host = _TerminalMarkHost()
    host._has_replied_after = lambda m: False
    host._has_user_taken_over = lambda m: False
    host._is_owner_present = lambda m: True
    host._handle_message_with_rid(_msg(), "rid-present")
    host._process_llm_reply.assert_not_called()
    host._mark_inbound_processed.assert_not_called()
