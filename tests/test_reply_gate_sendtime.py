"""发送前最后一刻门控复核（核心门控修复）单元测试。

覆盖：
1. _should_reply_now / _reply_gate_reason 的组合逻辑（自身/接管/在场/已读 四道闸的 OR 语义）。
2. _owner_conversation_is_read（DWS 未读列表判定 + 缓存 + 异常保守）。
3. 行为级·发送前复核：人工在 LLM 生成期间回复（接管），_send_reply 在发送前复核未通过，
   放弃发送、标记已处理、绝不调用 DWS 实际发送——这正是“我已经回复过的你别再回”。
4. 行为级·前置过滤（双重校验第一道）：进入 LLM 前命中门控即跳过 _process_llm_reply、
   标记入站已处理，避免无效 Token 消耗；门控放行则正常进入 LLM。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.platform.runtime import RuntimeMixin
from src.platform.runtime_inbound import InboundMixin


class _Msg(SimpleNamespace):
    msg_type = "text"
    content = "hi"
    raw = {}
    chat_id = "c1"
    chat_name = "peer"
    sender_name = "peer"
    sender_id = "peerId"
    msg_id = None
    timestamp = None


def _inbound(present=False, taken=False, self_msg=False, read=False,
             suppress_read=True):
    """构造最小 InboundMixin 宿主，用 lambda 隔离各子闸，专注测试组合逻辑。"""
    inst = InboundMixin()
    inst._has_user_taken_over = lambda m: taken
    inst._is_owner_present = lambda m: present
    inst._is_message_from_self = lambda m: self_msg
    inst._owner_conversation_is_read = lambda m: read
    inst.config = SimpleNamespace(poller=SimpleNamespace(
        suppress_when_owner_read=suppress_read))
    return inst


def test_should_reply_now_allows_when_no_gate():
    inst = _inbound()
    assert inst._should_reply_now(_Msg()) is True


def test_should_reply_now_blocks_on_self_message():
    inst = _inbound(self_msg=True)
    assert inst._should_reply_now(_Msg()) is False


def test_should_reply_now_blocks_on_takeover():
    inst = _inbound(taken=True)
    assert inst._should_reply_now(_Msg()) is False


def test_should_reply_now_blocks_on_owner_present():
    inst = _inbound(present=True)
    assert inst._should_reply_now(_Msg()) is False


def test_should_reply_now_blocks_on_read_gate():
    # 已读闸门仅在 suppress_when_owner_read=True 且 DWS 判定已读时抑制
    inst = _inbound(read=True, suppress_read=True)
    assert inst._should_reply_now(_Msg()) is False


def test_should_reply_now_read_gate_disabled():
    inst = _inbound(read=True, suppress_read=False)
    assert inst._should_reply_now(_Msg()) is True


# === _reply_gate_reason（前置过滤与发送前复核共用的原因判定）===
def test_reply_gate_reason_none_when_no_gate():
    inst = _inbound()
    assert inst._reply_gate_reason(_Msg()) is None


def test_reply_gate_reason_self():
    inst = _inbound(self_msg=True)
    assert inst._reply_gate_reason(_Msg()) == "消息来自自身"


def test_reply_gate_reason_takeover():
    inst = _inbound(taken=True)
    assert inst._reply_gate_reason(_Msg()) == "人工已接管（消息后已手动回复）"


def test_reply_gate_reason_present():
    inst = _inbound(present=True)
    assert inst._reply_gate_reason(_Msg()) == "真人当前在场"


def test_reply_gate_reason_read():
    inst = _inbound(read=True)
    assert inst._reply_gate_reason(_Msg()) == "DWS 判定会话已读"


def test_reply_gate_reason_read_disabled():
    inst = _inbound(read=True, suppress_read=False)
    assert inst._reply_gate_reason(_Msg()) is None


def test_owner_conversation_is_read_true_when_absent_from_unread():
    inst = InboundMixin()
    inst.dws = MagicMock()
    inst.dws.chat_message_list_unread_conversations.return_value = [
        {"openConversationId": "other", "unreadCount": 2}
    ]
    inst.config = SimpleNamespace(poller=SimpleNamespace(unread_conversation_count=20))
    # c1 不在未读列表 → 视为已读
    assert inst._owner_conversation_is_read(_Msg(chat_id="c1")) is True


def test_owner_conversation_is_read_false_when_unread():
    inst = InboundMixin()
    inst.dws = MagicMock()
    inst.dws.chat_message_list_unread_conversations.return_value = [
        {"openConversationId": "c1", "unreadCount": 1}
    ]
    inst.config = SimpleNamespace(poller=SimpleNamespace(unread_conversation_count=20))
    assert inst._owner_conversation_is_read(_Msg(chat_id="c1")) is False


def test_owner_conversation_is_read_conservative_on_error():
    inst = InboundMixin()
    inst.dws = MagicMock()
    inst.dws.chat_message_list_unread_conversations.side_effect = RuntimeError("boom")
    inst.config = SimpleNamespace(poller=SimpleNamespace(unread_conversation_count=20))
    # 异常时保守放行（不抑制）
    assert inst._owner_conversation_is_read(_Msg(chat_id="c1")) is False


class _FakeRuntime(RuntimeMixin):
    """最小宿主，只喂 _send_reply 被测路径需要的属性。"""

    def __init__(self, should_reply: bool):
        self.config = SimpleNamespace(poller=SimpleNamespace(
            reply_send_min_interval=0.0,
            reply_send_rate_limit_backoff_seconds=60.0,
            reply_shard_limit=4000,
            suppress_when_owner_read=True,
        ))
        self.dws = MagicMock()
        self.dws.dry_run = False
        self.dws.chat_message_send.side_effect = lambda **kw: {
            "result": {"openTaskId": f"task_{kw.get('uuid')}"}
        }
        self.poller = MagicMock()
        self._last_reply_send_ts = 0.0
        self._reply_send_throttle_lock = __import__("threading").Lock()
        self._reply_rate_limited_until = 0.0
        self._send_backoff_until = {}
        # 被测路径桩
        self._reply_cooldown_active = lambda m: False
        self._filter_sensitive_words = lambda t: t
        self._prepare_outgoing_text = lambda f, m: ("title", f)
        self._mark_read_before_reply = MagicMock()
        self._dispatch_reply_send = MagicMock(return_value=(True, {"result": {}}))
        self._record_reply_success = MagicMock()
        self._mark_inbound_processed = MagicMock()
        self._should_reply_now = lambda m: should_reply


def test_send_reply_aborts_when_human_took_over_at_send_time():
    """核心修复：入站时人工未回复（门通过），但 LLM 生成期间人工回复（接管），
    发送前复核应放弃发送、标记已处理、绝不实际发送。"""
    rt = _FakeRuntime(should_reply=False)
    msg = _Msg()
    result = rt._send_reply(msg, "AI 的回复")
    assert result is False
    rt._mark_inbound_processed.assert_called_once_with(msg)
    rt._dispatch_reply_send.assert_not_called()
    rt._mark_read_before_reply.assert_not_called()


def test_send_reply_proceeds_when_gate_passes():
    rt = _FakeRuntime(should_reply=True)
    msg = _Msg()
    result = rt._send_reply(msg, "AI 的回复")
    assert result is True
    rt._dispatch_reply_send.assert_called_once()
    rt._mark_inbound_processed.assert_not_called()


# === 前置过滤（双重校验·第一道）：进入 LLM 前拦截 ===
class _PrefilterHost(InboundMixin):
    """最小宿主：驱动真实的 _handle_message_with_rid 前置过滤路径。

    前置过滤调用真实的 _reply_gate_reason（共用逻辑），其底层子闸用 lambda 隔离，
    仅保留 read 闸门可控，从而验证「门控命中 → 跳过 LLM、标记已处理」的真实接线。
    """
    # _active_ctx 在基类中是只读 property，子类用同名 property 覆盖以提供测试桩
    @property
    def _active_ctx(self):
        return self._active_ctx_ns

    def __init__(self, read: bool = False):
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
        # 子闸：除 read 外全部放行，read 由构造参数控制
        self._is_message_from_self = lambda m: False
        self._has_user_taken_over = lambda m: False
        self._is_owner_present = lambda m: False
        self._owner_conversation_is_read = lambda m: read
        # 入站链路其余分支桩
        self._should_skip_inbound = MagicMock(return_value=False)
        self._reply_cooldown_active = MagicMock(return_value=False)
        self._has_replied_after = MagicMock(return_value=False)
        self._handle_oa_approval_urge = MagicMock(return_value=False)
        self._apply_rule_result = MagicMock(return_value=False)
        # 被测路径桩
        self._process_llm_reply = MagicMock()
        self._mark_inbound_processed = MagicMock()
        self._cleanup_backoff = MagicMock()


def test_prefilter_skips_llm_when_gate_blocks():
    """前置过滤：进入 LLM 前会话已读（DWS 闸门命中）→ 跳过 _process_llm_reply、
    标记入站已处理，避免无效 Token 消耗。这正是用户日志中“吃完 7704 token 才被拦”的反面。"""
    host = _PrefilterHost(read=True)
    msg = _Msg()
    host._handle_message_with_rid(msg, "rid-1")
    host._process_llm_reply.assert_not_called()
    host._mark_inbound_processed.assert_called_once_with(msg)


def test_prefilter_proceeds_to_llm_when_gate_passes():
    """前置过滤放行（会话未读）→ 正常进入 LLM 处理；不提前标记已处理。"""
    host = _PrefilterHost(read=False)
    msg = _Msg()
    host._handle_message_with_rid(msg, "rid-1")
    host._process_llm_reply.assert_called_once()
    host._mark_inbound_processed.assert_not_called()
