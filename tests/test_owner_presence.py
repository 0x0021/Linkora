"""真人在场冷却闸门（human-in-the-loop）单元测试。

验证 `_is_owner_present`：本会话窗口内出现真人手动消息时抑制 AI 回复，
关闭（cooldown<=0）或被 mock/非法值时放行。
"""
from __future__ import annotations

import datetime

from src.platform.runtime_inbound import InboundMixin


class _FakeRepo:
    def __init__(self, present: bool):
        self._present = present
        self.calls = []

    def has_user_message_from(self, chat_id, since_iso_ts, sender_ids,
                              max_age_days=30, platform=""):
        self.calls.append((chat_id, since_iso_ts, sender_ids))
        return self._present


class _FakeStore:
    def __init__(self, present: bool):
        self._conversation_repo = _FakeRepo(present)


class _Cfg:
    def __init__(self, cooldown: int):
        class Poller:
            owner_present_cooldown_seconds = cooldown
        self.poller = Poller()


def _make(cooldown: int, present: bool) -> InboundMixin:
    inst = InboundMixin()
    inst.store = _FakeStore(present)
    inst.config = _Cfg(cooldown)
    inst.current_open_dingtalk_id = "oidX"
    inst.current_user_id = "uidX"
    inst.current_user_name = "owner"
    return inst


class _Msg:
    chat_id = "c1"
    timestamp = datetime.datetime.now()
    sender_name = "peer"


def test_owner_present_suppresses_reply():
    assert _make(cooldown=600, present=True)._is_owner_present(_Msg()) is True


def test_no_recent_owner_message_allows_reply():
    assert _make(cooldown=600, present=False)._is_owner_present(_Msg()) is False


def test_disabled_when_cooldown_zero():
    assert _make(cooldown=0, present=True)._is_owner_present(_Msg()) is False


def test_uses_current_user_sender_ids():
    inst = _make(cooldown=600, present=True)
    inst._is_owner_present(_Msg())
    called_ids = inst.store._conversation_repo.calls[-1][2]
    assert "oidX" in called_ids and "uidX" in called_ids
