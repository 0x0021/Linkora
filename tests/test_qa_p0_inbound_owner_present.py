"""QA 独立验证 · P0-1：入站门控终态标记 + 「真人在场」不掐死 AI 接管。

独立于提交自带的 test_inbound_gate_terminal_mark.py，本文件用**真实**的
_has_replied_after / _has_user_taken_over / _is_owner_present / _mark_inbound_processed
（来自 InboundMixin + ReplyGuardMixin）驱动整条 _handle_message_with_rid 路径，
以「带敌意」的方式证明：

1. 「真人在场」分支命中后**不标记**入站已处理；当在场窗口过期（模拟：近场真人活动清空）
   后，同一条消息再次进入，AI **能**回复（未被永久标记掐死）。
2. 「已回复」/「人工已接管」分支命中后**确实标记**；再次投递被去重、不会重复处理
   /重复入死信。
3. _mark_inbound_processed 内部**没有非预期副作用**：只写 last_replied_msg_id 与
   poller 去重集合，**不**误发已读回执（mark_read）、**不**记录回复内容。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.platform.runtime_inbound import InboundMixin
from src.platform.runtime_reply_guard import ReplyGuardMixin


class _FakeConversationRepo:
    """最小内存会话仓库：模拟「已回复消息 id」与「真人消息时间线」。"""

    def __init__(self):
        self._last_replied: dict[str, str] = {}
        # (timestamp, sender_id) 列表，模拟会话中的真人消息时间线
        self._user_messages: list[tuple[datetime, str]] = []

    def get_last_replied_msg_id(self, chat_id: str, platform: str = "") -> str | None:
        return self._last_replied.get(chat_id)

    def update_last_replied_msg_id(self, chat_id: str, msg_id: str) -> None:
        self._last_replied[chat_id] = msg_id

    def has_user_message_from(self, chat_id: str, since: str, sender_ids: list[str]) -> bool:
        try:
            since_dt = datetime.fromisoformat(since)
        except Exception:
            return False
        return any(ts >= since_dt and sid in sender_ids for ts, sid in self._user_messages)


class _InboundHost(InboundMixin, ReplyGuardMixin):
    """驱动真实 _handle_message_with_rid 前置过滤路径的最小宿主。"""

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
        self.current_open_dingtalk_id = "ownerDingId"
        self.current_user_id = "ownerUid"
        self.dws = MagicMock()
        self.rule_engine = MagicMock()
        self.rule_engine.check.return_value = SimpleNamespace(action="none")
        # 前置过滤桩（不影响主路径判定）
        self._is_message_from_self = lambda m: False
        self._should_skip_inbound = MagicMock(return_value=False)
        self._reply_cooldown_active = MagicMock(return_value=False)
        self._handle_oa_approval_urge = MagicMock(return_value=False)
        self._apply_rule_result = MagicMock(return_value=False)
        self._owner_conversation_is_read = lambda m: False
        self._handle_media_fallback = MagicMock()
        self._cleanup_backoff = MagicMock()
        # 被测路径桩：LLM 派发（命中门控放行时应当被调用）
        self._process_llm_reply = MagicMock()
        # 真实仓库 + 真实 _mark_inbound_processed（来自 ReplyGuardMixin）
        self._repo = _FakeConversationRepo()
        self.store = SimpleNamespace(_conversation_repo=self._repo)
        self.poller = SimpleNamespace(_mark_msg_processed=MagicMock())


def _msg(msg_id: str, ts: datetime):
    return SimpleNamespace(
        msg_type="text",
        content="hi",
        raw={},
        chat_id="c1",
        chat_name="peer",
        sender_name="peer",
        sender_id="peerId",
        msg_id=msg_id,
        timestamp=ts,
    )


def test_owner_present_suppresses_but_does_not_permanently_kill():
    """P0-1.1：真人在场抑制 → 不标记 → 在场窗口过期后 AI 能回复同一条消息。"""
    host = _InboundHost()
    now = datetime.now()
    # 阶段一：会话内有近场真人消息（now-10s，落在 600s 冷却窗内），但它在入站消息之前 →
    # taken_over 应为 False，owner_present 应为 True。
    host._repo._user_messages = [(now - timedelta(seconds=10), host.current_user_id)]
    m1 = _msg("m1", now)

    host._handle_message_with_rid(m1, "rid-1")
    # 真人在场分支：抑制，不标记、不派发 LLM
    host._process_llm_reply.assert_not_called()
    assert host._repo._last_replied.get("c1") is None  # 未标记
    host.poller._mark_msg_processed.assert_not_called()

    # 阶段二：真人在场窗口过期（模拟：近场真人活动清空，等价于冷却窗内无新真人消息）。
    host._repo._user_messages = []
    m1b = _msg("m1", now)  # 同一条消息再次进入
    host._handle_message_with_rid(m1b, "rid-2")
    # 关键断言：AI 最终能接管回复，未被永久标记掐死
    host._process_llm_reply.assert_called_once()


def test_replied_branch_marks_and_dedups_redelivery():
    """P0-1.2：已回复分支标记入站已处理；二次投递被去重、不重复处理。"""
    host = _InboundHost()
    now = datetime.now()
    # 预置「我已在 m1 之后回复过」→ _has_replied_after 立即为 True
    host._repo._last_replied = {"c1": "m1"}
    m1 = _msg("m1", now)

    host._handle_message_with_rid(m1, "rid-a")
    host._process_llm_reply.assert_not_called()
    # 终态标记确实发生（update_last_replied_msg_id + poller 去重）
    assert host._repo._last_replied.get("c1") == "m1"
    host.poller._mark_msg_processed.assert_called_with("m1", "c1")

    # 二次投递（轮询重复拉取同一消息）→ 仍命中已回复分支，不重复派发 LLM
    host._process_llm_reply.reset_mock()
    host.poller._mark_msg_processed.reset_mock()
    host._handle_message_with_rid(_msg("m1", now), "rid-b")
    host._process_llm_reply.assert_not_called()
    # 去重写入仍发生（幂等），但不触发 LLM
    host.poller._mark_msg_processed.assert_called_with("m1", "c1")


def test_taken_over_branch_marks_and_dedups_redelivery():
    """P0-1.2：人工已接管分支标记入站已处理；二次投递被去重。"""
    host = _InboundHost()
    now = datetime.now()
    # 真人在入站消息「之后」手动回复了 → taken_over 应为 True
    host._repo._user_messages = [(now, host.current_user_id)]
    m1 = _msg("m1", now - timedelta(seconds=10))

    host._handle_message_with_rid(m1, "rid-c")
    host._process_llm_reply.assert_not_called()
    assert host._repo._last_replied.get("c1") == "m1"  # 已标记
    host.poller._mark_msg_processed.assert_called_with("m1", "c1")

    # 二次投递 → 此时 _has_replied_after 已为 True（标记副作用），去重不重复处理
    host._process_llm_reply.reset_mock()
    host._handle_message_with_rid(_msg("m1", now - timedelta(seconds=10)), "rid-d")
    host._process_llm_reply.assert_not_called()


def test_mark_inbound_processed_has_no_unexpected_side_effects():
    """P0-1.3：_mark_inbound_processed 只写去重键，不误发已读回执、不记回复内容。"""
    host = _InboundHost()
    m = _msg("mX", datetime.now())
    host._mark_inbound_processed(m)

    # 仅两处写：last_replied_msg_id + poller 去重集合
    assert host._repo._last_replied.get("c1") == "mX"
    host.poller._mark_msg_processed.assert_called_once_with("mX", "c1")
    # 关键负向断言：绝不触发已读回执发送（早退路径误调会静默发 read 信号）
    host.dws.mark_read.assert_not_called()


def test_mark_inbound_processed_falls_back_to_alt_id():
    """P0-1.3：msg_id 为空但 raw.alt_id 存在时，用 alt_id 去重而非误写空键。"""
    host = _InboundHost()
    m = SimpleNamespace(msg_type="text", content="x", raw={"alt_id": "alt-9"},
                        chat_id="c2", chat_name="p", sender_name="p",
                        sender_id="p", msg_id=None, timestamp=datetime.now())
    host._mark_inbound_processed(m)
    assert host._repo._last_replied.get("c2") == "alt-9"
    host.poller._mark_msg_processed.assert_called_once_with("alt-9", "c2")
    host.dws.mark_read.assert_not_called()


def test_mark_inbound_processed_noop_when_no_key():
    """P0-1.3：msg_id 与 alt_id 皆空 → 不产生任何写入（防御）。"""
    host = _InboundHost()
    m = SimpleNamespace(msg_type="text", content="x", raw={}, chat_id="c3",
                        chat_name="p", sender_name="p", sender_id="p",
                        msg_id=None, timestamp=datetime.now())
    host._mark_inbound_processed(m)
    assert "c3" not in host._repo._last_replied
    host.poller._mark_msg_processed.assert_not_called()
    host.dws.mark_read.assert_not_called()
