"""飞书/企微自身回复回声环修复回归测试。

根因：bot 持久化 assistant 行原本用本地 reply_uuid 作为 msg_id，而下一轮轮询
拉回自身消息的 msg_id 是平台真实 id（飞书 om_xxx / 钉钉 openMessageId / 企微 msgid），
二者永不相等 → self 检测只能退守 content+time 兜底（±120s），易漏判引发回声环。

修复：发送成功后归一化提取平台真实 msg_id（钉钉 openTaskId / 飞书 data.message_id /
企微 noop_uuid），用于持久化 assistant 行的 msg_id 与去重标记，使下一轮拉回自身
消息的 msg_id 直接命中 messages 表第一道防线（role='assistant'）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.platform.runtime import RuntimeMixin


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


class _RecordHost(RuntimeMixin):
    """最小宿主：驱动真实的 _record_reply_success，隔离 store/poller/dws。"""

    def __init__(self):
        self.dws = MagicMock()
        self.dws.dry_run = False
        self.poller = MagicMock()
        self.store = MagicMock()
        self.store._conversation_repo = MagicMock()
        self.store._message_repo = MagicMock()
        self.current_open_dingtalk_id = "ownerId"
        self.current_user_id = "userId"
        self.current_user_name = "AI助手"
        self.config = SimpleNamespace(poller=SimpleNamespace(reply_send_min_interval=0.0))


def _saved_msg_id(host):
    args, _ = host.store._message_repo.save_message.call_args
    return args[0].msg_id


def _marked_ids(host):
    return [c.args[0] for c in host.poller._mark_msg_processed.call_args_list]


def test_extract_platform_msg_id_feishu():
    host = _RecordHost()
    assert host._extract_platform_msg_id({"data": {"message_id": "om_abc"}}, "ru") == "om_abc"
    assert host._extract_platform_msg_id({"message_id": "om_flat"}, "ru") == "om_flat"


def test_extract_platform_msg_id_dingtalk():
    host = _RecordHost()
    assert host._extract_platform_msg_id({"result": {"openTaskId": "dt_xyz"}}, "ru") == "dt_xyz"


def test_extract_platform_msg_id_wecom_falls_back_to_uuid():
    host = _RecordHost()
    # 企微 CLI 不返回真实 id，回传调用方 uuid（=reply_uuid）
    assert host._extract_platform_msg_id({"errcode": 0, "noop_uuid": "ru"}, "ru") == "ru"


def test_extract_platform_msg_id_none_when_unknown():
    host = _RecordHost()
    assert host._extract_platform_msg_id({}, "ru") is None
    assert host._extract_platform_msg_id(None, "ru") is None


def test_record_reply_success_uses_feishu_platform_msg_id():
    """飞书回复：assistant 行 msg_id 应等于平台真 id，且去重标记用真 id。"""
    host = _RecordHost()
    msg = _Msg(chat_id="c1", chat_type="group", chat_name="g",
               sender_id="p", sender_name="peer", msg_id="m1", raw={})
    host._record_reply_success(msg, "AI 回复内容", "reply-uuid",
                               {"data": {"message_id": "om_abc"}})
    assert _saved_msg_id(host) == "om_abc"
    assert "om_abc" in _marked_ids(host)


def test_record_reply_success_uses_dingtalk_platform_msg_id():
    host = _RecordHost()
    msg = _Msg(chat_id="c1", chat_type="group", chat_name="g",
               sender_id="p", sender_name="peer", msg_id="m1", raw={})
    host._record_reply_success(msg, "AI 回复内容", "reply-uuid",
                               {"result": {"openTaskId": "dt_xyz"}})
    assert _saved_msg_id(host) == "dt_xyz"
    assert "dt_xyz" in _marked_ids(host)


def test_record_reply_success_wecom_falls_back_to_reply_uuid():
    """企微无真实平台 id：assistant 行 msg_id fallback 到 reply_uuid（保持现状）。"""
    host = _RecordHost()
    msg = _Msg(chat_id="c1", chat_type="group", chat_name="g",
               sender_id="p", sender_name="peer", msg_id="m1", raw={})
    host._record_reply_success(msg, "AI 回复内容", "reply-uuid",
                               {"errcode": 0, "noop_uuid": "reply-uuid"})
    assert _saved_msg_id(host) == "reply-uuid"
    assert "reply-uuid" in _marked_ids(host)
