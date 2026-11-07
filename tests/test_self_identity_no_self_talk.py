"""「自言自语」事故回归测试（2026-11-06）。

事故链路：本人消息（sender 是昵称、senderId 是 openDingTalkId）未被判成自我消息
→ 触发 LLM → 会话缓存的 peer_oid 被写成本人 ID → 单聊回复按人发送，全部落进
「我」窗口 → 回复被 list-all 回灌成 user 消息，看起来像 AI 自言自语。

本文件直接驱动生产方法（不是只测辅助函数），守护四层修复：
1. 启动期从单聊覆盖度反推自身 openDingTalkId + 昵称别名；
2. poller / runtime 用统一口径判定自我消息（含别名、防同名误杀）；
3. 发送前拒绝「入站消息来自自身」与「peer 命中自身 ID」；
4. 单聊优先按入站会话 ID 直通发送，失败才降级按人发送。

夹具一律使用虚构姓名，公开产物不得出现真实姓名。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.dws_adapter.chat import DwsAdapterChatMixin
from src.im_adapter.errors import IMAdapterError
from src.platform.runtime_dispatch import ReplyDispatchMixin
from src.platform.runtime_inbound import InboundMixin
from src.platform.runtime_setup import SetupMixin
from src.poller_core_dedup import DedupMixin
from src.self_identity import (
    collect_self_names,
    is_self_message,
    learn_self_from_single_chats,
)

# 虚构身份：正式姓名「钱多多」，钉钉昵称「多多」
OWNER_OID = "DDn1_OWNER_OID"
OWNER_UID = "owner-user-id"
OWNER_NAME = "钱多多"
OWNER_ALIAS = "多多"
PEER_OID = "DDn1_PEER_OID"
PEER_NAME = "王小明"


def _msg(*, sender_id, sender_name, raw=None, **extra):
    return SimpleNamespace(
        sender_id=sender_id,
        sender_name=sender_name,
        raw=raw or {},
        msg_type="text",
        chat_id="cid_peer_conversation",
        chat_name=PEER_NAME,
        content="今天能碰一下吗",
        msg_id="m1",
        **extra,
    )


# ─────────────────────────────── 1. 启动期身份学习 ───────────────────────────────

def _conversation(idx, messages):
    return {"openConversationId": f"cid_{idx}", "singleChat": True,
            "title": f"会话{idx}", "messages": messages}


def _message(sender_oid, sender_name, text="hi"):
    return {"senderOpenDingTalkId": sender_oid, "senderId": sender_oid,
            "sender": sender_name, "content": text, "singleChat": True}


def test_learn_self_from_single_chats_finds_oid_and_alias():
    """本人 oid 横跨所有单聊 → 学出 oid 与昵称（不依赖正式姓名 / 配置）。"""
    convs = [
        _conversation(i, [_message(OWNER_OID, OWNER_ALIAS),
                          _message(f"DDn1_OTHER_{i}", PEER_NAME)])
        for i in range(5)
    ]
    # 混入群聊，不应参与单聊覆盖度统计
    convs.append({"openConversationId": "cid_group", "singleChat": False,
                  "messages": [_message(f"DDn1_OTHER_{i}", PEER_NAME) for i in range(9)]})
    oid, aliases = learn_self_from_single_chats(
        convs, known_ids=[OWNER_UID], known_names=[OWNER_NAME]
    )
    assert oid == OWNER_OID
    assert OWNER_ALIAS in aliases
    assert OWNER_NAME not in aliases  # 已知正式姓名不重复回传


class _ResolveHost:
    """最小宿主：驱动真实的 SetupMixin._resolve_own_open_dingtalk_id。"""

    _resolve_own_open_dingtalk_id = SetupMixin._resolve_own_open_dingtalk_id
    _learn_self_identity_from_conversations = SetupMixin._learn_self_identity_from_conversations

    def __init__(self, conversations):
        self.current_user_id = OWNER_UID
        self.current_user_name = OWNER_NAME
        self.current_open_dingtalk_id = ""
        self._self_sender_aliases: set[str] = set()
        self.dws = MagicMock()
        self.dws.chat_message_list_all.return_value = {
            "conversationMessagesList": conversations,
        }
        self.store = MagicMock()
        # 默认让本地会话缓存无命中，确保下面两个用例真正驱动网络解析路径
        self.store.conv_conn.return_value.cursor.return_value.fetchall.return_value = []


def test_resolve_own_oid_prefers_local_conversation_cache():
    """本地「我」会话缓存命中时零网络解析，避免 60s 启动延迟与超时降级。"""
    host = _ResolveHost([])
    host.store.conv_conn.return_value.cursor.return_value.fetchall.return_value = [
        (OWNER_ALIAS, OWNER_OID),
    ]
    assert host._resolve_own_open_dingtalk_id() == OWNER_OID
    assert OWNER_ALIAS in host._self_sender_aliases
    host.dws.chat_message_list_all.assert_not_called()


def test_resolve_own_oid_matches_sender_id_against_user_id():
    """直接证据：senderId 是 userId、senderOpenDingTalkId 是 oid 时能解析出来。

    旧实现把两个字段用 ``or`` 合并成一个变量再比 userId，恒不相等 →
    openDingTalkId 长期为空（事故根因之一）。
    """
    convs = [_conversation(0, [{"senderId": OWNER_UID,
                                "senderOpenDingTalkId": OWNER_OID,
                                "sender": PEER_NAME, "singleChat": True}])]
    host = _ResolveHost(convs)
    assert host._resolve_own_open_dingtalk_id() == OWNER_OID


def test_resolve_own_oid_learns_nickname_via_single_chat_coverage():
    """无 userId / 正式姓名线索时，靠单聊覆盖度反推 oid 并学到昵称。"""
    convs = [
        _conversation(i, [_message(OWNER_OID, OWNER_ALIAS),
                          _message(f"DDn1_OTHER_{i}", PEER_NAME)])
        for i in range(5)
    ]
    host = _ResolveHost(convs)
    assert host._resolve_own_open_dingtalk_id() == OWNER_OID
    assert OWNER_ALIAS in host._self_sender_aliases


def test_learn_self_from_single_chats_needs_enough_evidence():
    """单聊太少 / 覆盖度不足时宁可返回空，绝不猜一个可能是同事的 ID。"""
    convs = [_conversation(i, [_message(f"DDn1_OTHER_{i}", PEER_NAME)]) for i in range(2)]
    assert learn_self_from_single_chats(convs) == ("", frozenset())

    convs = [_conversation(i, [_message(OWNER_OID, OWNER_ALIAS)]) for i in range(2)] + [
        _conversation(i + 2, [_message(f"DDn1_OTHER_{i}", PEER_NAME)]) for i in range(8)
    ]
    assert learn_self_from_single_chats(convs) == ("", frozenset())


# ─────────────────────────────── 2. 统一自我判定 ───────────────────────────────

def test_is_self_message_by_oid_even_with_nickname():
    ids = {OWNER_OID, OWNER_UID}
    names = collect_self_names(OWNER_NAME, [OWNER_ALIAS])
    assert is_self_message(sender_id=OWNER_OID, sender_name=OWNER_ALIAS,
                           self_ids=ids, self_names=names) is True
    # raw 兜底：sender_id 缺失但原始字段带自身 oid
    assert is_self_message(sender_id="", sender_name="",
                           raw={"senderOpenDingTalkId": OWNER_OID},
                           self_ids=ids, self_names=names) is True


def test_is_self_message_by_oid_without_any_name():
    """没有 sender_name 时，仅凭自身 ID 也必须判自我（回灌消息常缺姓名）。

    这条专门守住「ID 判定」分支本身：若只靠姓名判定，此用例会漏判并触发
    自言自语。变异测试 A 曾因缺少该场景而全绿。
    """
    ids = {OWNER_OID, OWNER_UID}
    names = collect_self_names(OWNER_NAME, [OWNER_ALIAS])
    assert is_self_message(sender_id=OWNER_OID, sender_name="",
                           self_ids=ids, self_names=names) is True
    assert is_self_message(sender_id=OWNER_UID, sender_name="",
                           self_ids=ids, self_names=names) is True
    assert is_self_message(sender_id=PEER_OID, sender_name="",
                           self_ids=ids, self_names=names) is False


def test_is_self_message_does_not_swallow_same_name_colleague():
    """同名但 ID 是他人 → 不能仅凭姓名判自我（防误杀真实消息）。"""
    ids = {OWNER_OID, OWNER_UID}
    names = collect_self_names(OWNER_NAME, [OWNER_ALIAS])
    assert is_self_message(sender_id=PEER_OID, sender_name=OWNER_NAME,
                           self_ids=ids, self_names=names) is False
    # 无 sender_id 时才允许纯姓名兜底
    assert is_self_message(sender_id="", sender_name=OWNER_ALIAS,
                           self_ids=ids, self_names=names) is True


class _PollerHost:
    """最小宿主：驱动真实的 DedupMixin._is_self_message。"""

    store = SimpleNamespace()  # 无 conv_conn → 不进入 DB 回灌兜底

    def __init__(self, aliases=(OWNER_ALIAS,)):
        self.current_user_id = OWNER_OID
        self.current_user_user_id = OWNER_UID
        self.current_user_name = OWNER_NAME
        self._self_ids = {OWNER_OID, OWNER_UID}
        self._self_names = collect_self_names(OWNER_NAME, aliases)

    _is_self_message = DedupMixin._is_self_message
    _is_self_sender = DedupMixin._is_self_sender


def test_poller_recognizes_owner_message_with_nickname_and_oid():
    host = _PollerHost()
    assert host._is_self_message(_msg(sender_id=OWNER_OID, sender_name=OWNER_ALIAS)) is True
    assert host._is_self_message(_msg(sender_id=OWNER_UID, sender_name=OWNER_NAME)) is True
    assert host._is_self_message(_msg(sender_id=OWNER_OID, sender_name="")) is True
    assert host._is_self_sender(OWNER_OID) is True
    assert host._is_self_message(_msg(sender_id=PEER_OID, sender_name=PEER_NAME)) is False


class _RuntimeHost:
    _is_message_from_self = InboundMixin._is_message_from_self

    def __init__(self, with_oid=True, aliases=(OWNER_ALIAS,)):
        self.current_open_dingtalk_id = OWNER_OID if with_oid else ""
        self.current_user_id = OWNER_UID
        self.current_user_name = OWNER_NAME
        self.self_sender_names = collect_self_names(OWNER_NAME, aliases)


def test_runtime_inbound_self_filter_with_alias():
    """即便 openDingTalkId 解析失败，正式姓名 + 昵称仍要能挡住回灌。"""
    host = _RuntimeHost()
    assert host._is_message_from_self(
        _msg(sender_id=OWNER_OID, sender_name=OWNER_ALIAS)) is True
    assert host._is_message_from_self(
        _msg(sender_id=PEER_OID, sender_name=PEER_NAME)) is False

    no_oid = _RuntimeHost(with_oid=False)
    # 无 oid 且 sender_id 是 userId 时，姓名命中也可判自我
    assert no_oid._is_message_from_self(
        _msg(sender_id=OWNER_UID, sender_name=OWNER_NAME)) is True


# ─────────────────────────────── 3/4. 发送护栏 ───────────────────────────────

class _DispatchHost:
    _send_single_chat_reply = ReplyDispatchMixin._send_single_chat_reply

    def __init__(self, *, conv=None):
        self.current_open_dingtalk_id = OWNER_OID
        self.current_user_id = OWNER_UID
        self.current_user_name = OWNER_NAME
        self.self_sender_names = collect_self_names(OWNER_NAME, [OWNER_ALIAS])
        self.store = MagicMock()
        self.store._conversation_repo.get_conversation.return_value = (
            conv if conv is not None else {"peer_user_id": "", "peer_open_dingtalk_id": PEER_OID}
        )
        self.store._external_friend_repo.get_external_friend_by_id.return_value = None
        self.dws = MagicMock()
        self.dws.supports_conversation_id_send = True
        self.dws.dry_run = False
        self.poller = MagicMock()
        self._send_possibly_sharded = MagicMock(return_value={"result": {"openTaskId": "t1"}})
        self._mark_inbound_processed = MagicMock()


def test_send_rejects_inbound_from_self():
    """入站消息来自自己 → 绝不外发（最后一道闸门）。"""
    host = _DispatchHost()
    sent, result = host._send_single_chat_reply(
        _msg(sender_id=OWNER_OID, sender_name=OWNER_ALIAS),
        "回复", "好的", "uuid-1",
    )
    assert (sent, result) == (False, None)
    host._send_possibly_sharded.assert_not_called()
    host._mark_inbound_processed.assert_called_once()


def test_send_rejects_peer_cache_polluted_with_own_id():
    """会话缓存 peer_oid 被污染成自身 ID → 拒绝发送，不投进「我」窗口。"""
    host = _DispatchHost(conv={"peer_user_id": "", "peer_open_dingtalk_id": OWNER_OID})
    sent, result = host._send_single_chat_reply(
        _msg(sender_id=PEER_OID, sender_name=PEER_NAME), "回复", "好的", "uuid-2",
    )
    assert (sent, result) == (False, None)
    host._send_possibly_sharded.assert_not_called()


def test_send_prefers_conversation_id_over_person_target():
    """正常单聊：按入站会话 ID 直通，不碰可能被污染的 peer_oid。"""
    host = _DispatchHost()
    msg = _msg(sender_id=PEER_OID, sender_name=PEER_NAME)
    sent, _ = host._send_single_chat_reply(msg, "回复", "好的", "uuid-3")
    assert sent is True
    kwargs = host._send_possibly_sharded.call_args.kwargs
    assert kwargs.get("conversation_id") == msg.chat_id
    assert "open_dingtalk_id" not in kwargs
    assert "group" not in kwargs


def test_send_falls_back_to_peer_when_conversation_id_fails():
    """dws 不支持/拒绝按会话 ID 时自动降级按人发送，回复不丢。"""
    host = _DispatchHost()

    def _side_effect(**kwargs):
        if "conversation_id" in kwargs:
            raise IMAdapterError("target_type_mismatch")
        return {"result": {"openTaskId": "t2"}}

    host._send_possibly_sharded.side_effect = _side_effect
    sent, _ = host._send_single_chat_reply(
        _msg(sender_id=PEER_OID, sender_name=PEER_NAME), "回复", "好的", "uuid-4",
    )
    assert sent is True
    calls = [c.kwargs for c in host._send_possibly_sharded.call_args_list]
    assert "conversation_id" in calls[0]
    assert calls[1].get("open_dingtalk_id") == PEER_OID


def test_send_corrects_polluted_peer_with_sender_id():
    """peer_oid 与 sender_id 不一致（钉钉 DDn1 前缀）→ 以 sender_id 为准。"""
    host = _DispatchHost(conv={"peer_user_id": "", "peer_open_dingtalk_id": "DDn1_STALE"})
    host.dws.supports_conversation_id_send = False  # 强制走按人链路
    host._send_single_chat_reply(
        _msg(sender_id=PEER_OID, sender_name=PEER_NAME), "回复", "好的", "uuid-5",
    )
    kwargs = host._send_possibly_sharded.call_args.kwargs
    assert kwargs.get("open_dingtalk_id") == PEER_OID
    host.store._conversation_repo.upsert_conversation.assert_called()


# ─────────────────────────────── CLI 目标构造 ───────────────────────────────

class _DwsSendHost:
    ai_tag_default = False
    supports_markdown_tables = False

    def __init__(self):
        self.args = None

    def run(self, args):
        self.args = args
        return {"ok": True}


def test_chat_message_send_builds_conversation_id_flag():
    host = _DwsSendHost()
    DwsAdapterChatMixin.chat_message_send(
        host, conversation_id="cid_peer_conversation", text="hello", uuid="u1",
    )
    assert host.args[:3] == ["chat", "message", "send"]
    assert host.args[3:5] == ["--conversation-id", "cid_peer_conversation"]
    assert "--open-dingtalk-id" not in host.args
    assert "--group" not in host.args


def test_chat_message_send_rejects_missing_target():
    with pytest.raises(ValueError):
        DwsAdapterChatMixin.chat_message_send(_DwsSendHost(), text="hello")
