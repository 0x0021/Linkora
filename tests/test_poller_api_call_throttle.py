"""DWS 调用降频缓存单测（未读列表 TTL 缓存 + 单聊对方解析失败负缓存）。

背景：钉钉管理后台（安全 > AI安全 > 调用管理）显示 list_conversation_message_v2 /
unread_message_conversation_list / get_conversation_info 等接口以秒级频率被轮询打爆。
本轮改动在不丢消息的前提下降低 DWS 调用频率：
- ``_fetch_unread_conversations``：TTL 内复用上次未读列表（新消息仍由 list-all
  主通道每轮全量拉取，未读列表只决定「实时优先 forced_ids」）。
- ``_resolve_single_chat_peer``：chat_conversation_info / contact_user_search
  失败后进入 TTL 负缓存，TTL 内不再重打元数据接口（对方 ID 由消息 sender 兜底）。

覆盖：
- 未读列表：TTL 命中/过期/关闭/失败不污染缓存/返回副本
- 对方解析：失败负缓存命中/过期重试/成功清缓存/关闭退化为旧行为/
  DB 缓存不受负缓存影响/全局权限错误仍抛出且不进负缓存
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.dws_adapter import DwsPermissionError
from src.poller_core_access import AccessControlMixin
from src.poller_core_dedup import DedupMixin
from src.poller_strategy import PollerStrategyMixin


# ============ 未读会话列表 TTL 缓存 ============

class FakeUnreadPoller(PollerStrategyMixin, AccessControlMixin):
    """最小 fake：只提供 _fetch_unread_conversations 依赖的属性。"""

    def __init__(self, ttl: int = 30):
        self.config = SimpleNamespace(
            unread_convs_cache_ttl_seconds=ttl,
            unread_conversation_count=20,
        )
        self._unread_convs_cache: list[dict] | None = None
        self._unread_convs_cache_ts: float = 0.0
        self._poll_shared_lock = threading.Lock()
        self._perm_warned: set[str] = set()
        self.dws = MagicMock()
        self.dws.chat_message_list_unread_conversations.return_value = [
            {"openConversationId": "oc_1"},
        ]


class TestUnreadConvsCache:
    def test_ttl_hit_skips_dws_call(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeUnreadPoller(ttl=30)
        first = p._fetch_unread_conversations()
        # TTL 内第二次：命中缓存，不再打 DWS
        monkeypatch.setattr("time.time", lambda: 1_029.0)
        second = p._fetch_unread_conversations()
        assert p.dws.chat_message_list_unread_conversations.call_count == 1
        assert first == second == [{"openConversationId": "oc_1"}]

    def test_ttl_expiry_refetches(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeUnreadPoller(ttl=30)
        p._fetch_unread_conversations()
        monkeypatch.setattr("time.time", lambda: 1_031.0)
        p._fetch_unread_conversations()
        assert p.dws.chat_message_list_unread_conversations.call_count == 2

    def test_ttl_zero_keeps_legacy_behavior(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeUnreadPoller(ttl=0)
        p._fetch_unread_conversations()
        p._fetch_unread_conversations()
        assert p.dws.chat_message_list_unread_conversations.call_count == 2

    def test_failure_does_not_poison_cache(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeUnreadPoller(ttl=30)
        p.dws.chat_message_list_unread_conversations.side_effect = RuntimeError("CLI 瞬断")
        assert p._fetch_unread_conversations() == []
        # 失败不写缓存：下一轮立即重试（不被空结果钉死）
        p.dws.chat_message_list_unread_conversations.side_effect = None
        again = p._fetch_unread_conversations()
        assert again == [{"openConversationId": "oc_1"}]
        assert p.dws.chat_message_list_unread_conversations.call_count == 2

    def test_permission_error_does_not_poison_cache(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeUnreadPoller(ttl=30)
        p.dws.chat_message_list_unread_conversations.side_effect = DwsPermissionError("无权限")
        assert p._fetch_unread_conversations() == []
        p.dws.chat_message_list_unread_conversations.side_effect = None
        p._fetch_unread_conversations()
        assert p.dws.chat_message_list_unread_conversations.call_count == 2

    def test_returns_copy_not_internal_cache(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakeUnreadPoller(ttl=30)
        first = p._fetch_unread_conversations()
        first.append({"openConversationId": "oc_mutated"})
        monkeypatch.setattr("time.time", lambda: 1_010.0)
        second = p._fetch_unread_conversations()
        assert second == [{"openConversationId": "oc_1"}]


# ============ 单聊对方解析失败负缓存 ============

class FakePeerPoller(DedupMixin, AccessControlMixin):
    """最小 fake：只提供 _resolve_single_chat_peer 依赖的属性。"""

    def __init__(self, ttl: int = 300):
        self.config = SimpleNamespace(conv_info_negative_cache_ttl_seconds=ttl)
        self._peer_resolve_cooldown_until: dict[str, float] = {}
        self._metadata_unavailable: set[str] = set()
        self._poll_shared_lock = threading.Lock()
        self.store = MagicMock()
        self.store._conversation_repo.get_conversation.return_value = None
        self.store._external_friend_repo.get_external_friend_by_name.return_value = None
        self.dws = MagicMock()


class TestPeerResolveNegativeCache:
    def test_failure_then_ttl_hit_skips_metadata_calls(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakePeerPoller(ttl=300)
        p.dws.chat_conversation_info.side_effect = RuntimeError("dws 瞬断 EOF")
        p.dws.contact_user_search.return_value = []
        # 第一次：两个元数据接口各打一次，均失败 → 进负缓存
        assert p._resolve_single_chat_peer("oc_x", "张三") == {"user_id": "", "open_dingtalk_id": ""}
        assert p.dws.chat_conversation_info.call_count == 1
        assert p.dws.contact_user_search.call_count == 1
        # TTL 内第二次：直接跳过，不再打任何元数据接口
        monkeypatch.setattr("time.time", lambda: 1_100.0)
        assert p._resolve_single_chat_peer("oc_x", "张三") == {"user_id": "", "open_dingtalk_id": ""}
        assert p.dws.chat_conversation_info.call_count == 1
        assert p.dws.contact_user_search.call_count == 1

    def test_ttl_expiry_retries(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakePeerPoller(ttl=300)
        p.dws.chat_conversation_info.side_effect = RuntimeError("dws 瞬断 EOF")
        p.dws.contact_user_search.return_value = []
        p._resolve_single_chat_peer("oc_x", "张三")
        monkeypatch.setattr("time.time", lambda: 1_301.0)
        p._resolve_single_chat_peer("oc_x", "张三")
        assert p.dws.chat_conversation_info.call_count == 2

    def test_success_clears_stale_cooldown(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakePeerPoller(ttl=300)
        # 预置一条已过期的陈旧负缓存条目（更早失败留下的，时间戳早于当前）
        p._peer_resolve_cooldown_until["oc_x"] = 500.0
        p.dws.chat_conversation_info.return_value = {
            "title": "李四", "openDingTalkId": "ou_peer",
        }
        monkeypatch.setattr("time.time", lambda: 1_200.0)
        peer = p._resolve_single_chat_peer("oc_x", "李四")
        assert peer == {"user_id": "", "open_dingtalk_id": "ou_peer"}
        # 解析成功：负缓存被清掉，不影响后续轮次
        assert "oc_x" not in p._peer_resolve_cooldown_until

    def test_ttl_zero_keeps_legacy_retry(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakePeerPoller(ttl=0)
        p.dws.chat_conversation_info.side_effect = RuntimeError("dws 瞬断 EOF")
        p.dws.contact_user_search.return_value = []
        p._resolve_single_chat_peer("oc_x", "张三")
        p._resolve_single_chat_peer("oc_x", "张三")
        # 旧行为：每轮都重试
        assert p.dws.chat_conversation_info.call_count == 2
        assert p.dws.contact_user_search.call_count == 2

    def test_db_cache_not_blocked_by_cooldown(self, monkeypatch):
        """DB/外部好友快路径是纯本地查询，绝不能被失败负缓存挡住。"""
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakePeerPoller(ttl=300)
        p._peer_resolve_cooldown_until["oc_x"] = 2_000.0  # 负缓存仍有效
        p.store._conversation_repo.get_conversation.return_value = {
            "peer_user_id": "ou_db", "peer_open_dingtalk_id": "ou_db",
        }
        peer = p._resolve_single_chat_peer("oc_x", "王五")
        assert peer == {"user_id": "ou_db", "open_dingtalk_id": "ou_db"}
        p.dws.chat_conversation_info.assert_not_called()
        p.dws.contact_user_search.assert_not_called()

    def test_global_permission_error_reraises_without_cooldown(self, monkeypatch):
        monkeypatch.setattr("time.time", lambda: 1_000.0)
        p = FakePeerPoller(ttl=300)
        p.dws.chat_conversation_info.side_effect = RuntimeError(
            "TOKEN_VERIFIED_FAILED: 该组织尚未开启 CLI 数据访问权限"
        )
        with pytest.raises(RuntimeError):
            p._resolve_single_chat_peer("oc_x", "跨组织会话")
        # 全局权限错误由主循环持久化跳过，不进负缓存（进了也不会错，但语义上不归它管）
        assert "oc_x" not in p._peer_resolve_cooldown_until
        # 也没有退化去打联系人搜索
        p.dws.contact_user_search.assert_not_called()
