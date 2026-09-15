"""poller_core_access.AccessControlMixin 单元测试。

覆盖: _is_blocked, _classify_inaccessible_reason, _block_conversation, clear_cross_org_skips。
"""

import sqlite3
from unittest.mock import MagicMock


from src.poller_core_access import AccessControlMixin


class FakeAccess(AccessControlMixin):
    """最小 fake，提供 access mixin 所需的属性。"""

    def __init__(self):
        self.store = MagicMock()
        self._inaccessible_conversations = set()


# ============ _is_blocked ============

class TestIsBlocked:
    def test_blocked(self):
        fa = FakeAccess()
        fa._inaccessible_conversations = {"oc_abc"}
        assert fa._is_blocked("oc_abc") is True

    def test_not_blocked(self):
        fa = FakeAccess()
        fa._inaccessible_conversations = {"oc_abc"}
        assert fa._is_blocked("oc_xyz") is False

    def test_strips_trailing_equals(self):
        fa = FakeAccess()
        fa._inaccessible_conversations = {"oc_abc"}
        assert fa._is_blocked("oc_abc=") is True

    def test_empty_id(self):
        fa = FakeAccess()
        assert fa._is_blocked("") is False

    def test_none_id(self):
        fa = FakeAccess()
        assert fa._is_blocked(None) is False


# ============ _classify_inaccessible_reason ============

class TestClassifyInaccessibleReason:
    def setup_method(self):
        self.fa = FakeAccess()

    def test_not_in_conversation(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("130003 OpendId is not in conversation"))
        assert code == "not_in_conversation"

    def test_confidential(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("保密群 无权限访问"))
        assert code == "confidential"

    def test_auth_permission_denied(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("AUTH_PERMISSION_DENIED"))
        assert code == "no_permission"

    def test_org_cli_disabled(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("该组织尚未开启 CLI 数据访问权限"))
        assert code == "org_cli_disabled"

    def test_token_verified_failed(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("TOKEN_VERIFIED_FAILED"))
        assert code == "org_cli_disabled"

    def test_generic_no_permission(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("no permission to access"))
        assert code == "no_permission"

    def test_fallback_permission_denied(self):
        code, text = self.fa._classify_inaccessible_reason(
            RuntimeError("something else"))
        assert code == "permission_denied"


# ============ _block_conversation ============

class TestBlockConversation:
    def test_adds_to_inaccessible_set(self):
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_abc", "test群", "group",
                               RuntimeError("130003"), "runtime_error")
        assert "oc_abc" in fa._inaccessible_conversations

    def test_persists_to_blacklist_repo(self):
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_def", "群", "group",
                               RuntimeError("保密群"), "scan")
        fa.store._blacklist_repo.add_blocked_conversation.assert_called_once()

    def test_strips_trailing_equals(self):
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_ghi=", "群", "group", RuntimeError("130003"))
        assert "oc_ghi" in fa._inaccessible_conversations
        assert "oc_ghi=" not in fa._inaccessible_conversations

    def test_empty_id_skips(self):
        fa = FakeAccess()
        fa._block_conversation("", "群", "group", RuntimeError("130003"))
        assert len(fa._inaccessible_conversations) == 0

    def test_store_error_graceful(self):
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.side_effect = sqlite3.Error("db down")
        fa._block_conversation("oc_jkl", "群", "group", sqlite3.Error("130003"))
        assert "oc_jkl" in fa._inaccessible_conversations  # 内存仍写入

    def test_confidential_is_permanent(self):
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_conf", "保密群", "group",
                               RuntimeError("保密群 无权限"), "runtime_error")
        kwargs = fa.store._blacklist_repo.add_blocked_conversation.call_args.kwargs
        assert kwargs.get("permanent") is True

    def test_not_in_conversation_is_permanent(self):
        # 130003 / not in conversation = 已退群 / 被移出 / 删好友 → 永久不可恢复
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_x", "已退的群", "group",
                               RuntimeError("130003"), "runtime_error")
        kwargs = fa.store._blacklist_repo.add_blocked_conversation.call_args.kwargs
        assert kwargs.get("permanent") is True

    def test_no_permission_is_permanent(self):
        # AUTH_PERMISSION_DENIED = 离开组织或群 → 会员身份已丢失 → 永久
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_np", "无权限群", "group",
                               RuntimeError("AUTH_PERMISSION_DENIED"), "runtime_error")
        kwargs = fa.store._blacklist_repo.add_blocked_conversation.call_args.kwargs
        assert kwargs.get("permanent") is True

    def test_feishu_permission_source_is_permanent(self):
        # 飞书 list_all 返回的 blocked_chats（source=feishu_permission）为永久权限错误
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_fs", "飞书已退群", "group",
                               RuntimeError("permission denied"), "feishu_permission")
        kwargs = fa.store._blacklist_repo.add_blocked_conversation.call_args.kwargs
        assert kwargs.get("permanent") is True

    def test_org_cli_disabled_stays_temporary(self):
        # 跨组织未开启 CLI 可恢复（切组织/开启后恢复）→ 保持临时冷却，不走永久
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_org", "跨组织群", "group",
                               RuntimeError("该组织尚未开启 CLI 数据访问权限"),
                               "runtime_error")
        kwargs = fa.store._blacklist_repo.add_blocked_conversation.call_args.kwargs
        assert kwargs.get("permanent") is False

    def test_generic_runtime_permission_denied_stays_temporary(self):
        # 兜底 permission_denied（runtime_error 来源）保留临时冷却，避免全局鉴权抖动被永久误杀
        fa = FakeAccess()
        fa.store._blacklist_repo.add_blocked_conversation.return_value = True
        fa._block_conversation("oc_pd", "普通群", "group",
                               RuntimeError("some unknown permission error"),
                               "runtime_error")
        kwargs = fa.store._blacklist_repo.add_blocked_conversation.call_args.kwargs
        assert kwargs.get("permanent") is False


# ============ _reconcile_blocklist 永久黑名单保护 ============

class TestReconcilePermanent:
    """回归：保密群 / 永久黑名单即使出现在"可见会话列表"里也不得被自愈解除。"""

    def setup_method(self):
        self.fa = FakeAccess()
        self.fa.dws = MagicMock()
        self.fa.config = MagicMock()
        self.fa.config.reconcile_probe_batch_size = 5
        self.fa._perm_fail_streak = {}
        self.fa._reconcile_probe_idx = 0
        self.fa._inaccessible_conversations = {
            "oc_conf", "oc_perm", "oc_temp", "oc_exit", "oc_custom",
        }
        # 保密群/已退群你仍是成员或曾可见，会出现在可见列表 → 过去误判为"已恢复"而解除
        self.fa.dws.chat_list_top_conversations.return_value = [
            {"openConversationId": "oc_conf"},
            {"openConversationId": "oc_temp"},
            {"openConversationId": "oc_exit"},
            {"openConversationId": "oc_custom"},
        ]
        self.fa.dws.chat_conversation_info.return_value = {}  # 探测成功视为恢复
        self.rows = [
            {"chat_id": "oc_conf", "chat_name": "保密群A", "chat_type": "group",
             "reason": "confidential", "source": "runtime_error", "cooldown_until": None},
            {"chat_id": "oc_perm", "chat_name": "跨租户B", "chat_type": "single",
             "reason": "permission_denied", "source": "feishu_permission",
             "cooldown_until": None},
            {"chat_id": "oc_temp", "chat_name": "跨组织群C", "chat_type": "group",
             "reason": "org_cli_disabled", "source": "runtime_error",
             "cooldown_until": "2099-01-01T00:00:00"},
            # 历史遗留：已退群本应永久，却仍停留在 1h 临时冷却 → 对账应升级为永久而非解除
            {"chat_id": "oc_exit", "chat_name": "已退的群D", "chat_type": "group",
             "reason": "not_in_conversation", "source": "runtime_error",
             "cooldown_until": "2099-01-01T00:00:00"},
            # 自定义 reason 的永久条目（tools/chat.py 写入）→ cooldown NULL 一律保护，
            # 不因 reason 不在候选集合而被误解除（回归防护）
            {"chat_id": "oc_custom", "chat_name": "飞书跨租户E", "chat_type": "p2p",
             "reason": "飞书永久黑名单：跨租户p2p不可达",
             "source": "feishu_external_chat_unsendable", "cooldown_until": None},
        ]
        self.fa.store._blacklist_repo.load_blocked_conversations.return_value = self.rows
        self.removed = []
        self.fa.store._blacklist_repo.remove_blocked_conversation.side_effect = (
            lambda cid: self.removed.append(cid)
        )
        self.upgraded = []
        self.fa.store._blacklist_repo.force_permanent_preserve_reason.side_effect = (
            lambda cid: (self.upgraded.append(cid), True)[1]
        )

    def test_confidential_never_released(self):
        self.fa._reconcile_blocklist()
        assert "oc_conf" not in self.removed
        assert "oc_conf" in self.fa._inaccessible_conversations

    def test_permanent_never_released(self):
        self.fa._reconcile_blocklist()
        assert "oc_perm" not in self.removed

    def test_custom_permanent_never_released(self):
        # cooldown NULL 的永久条目（自定义 reason/source）即便出现在可见列表也不得解除
        self.fa._reconcile_blocklist()
        assert "oc_custom" not in self.removed
        assert "oc_custom" in self.fa._inaccessible_conversations

    def test_exited_group_upgraded_not_released(self):
        # 已退群（not_in_conversation）即便出现在可见列表也不得解除，且应升级为永久
        self.fa._reconcile_blocklist()
        assert "oc_exit" not in self.removed
        assert "oc_exit" in self.upgraded
        assert "oc_exit" in self.fa._inaccessible_conversations

    def test_recoverable_temp_still_unblocked(self):
        # 可恢复的临时冷却（跨组织未开 CLI）且探测成功的会话仍应自愈解除
        self.fa._reconcile_blocklist()
        assert "oc_temp" in self.removed
        assert "oc_temp" not in self.upgraded


# ============ clear_cross_org_skips ============

class TestClearCrossOrgSkips:
    def test_clears_inaccessible_set(self):
        fa = FakeAccess()
        fa._inaccessible_conversations = {"oc_a", "oc_b", "oc_c"}
        fa.store._blacklist_repo.clear_blocked_conversations.return_value = 0
        count = fa.clear_cross_org_skips()
        assert count == 3
        assert len(fa._inaccessible_conversations) == 0

    def test_empty_set_returns_zero(self):
        fa = FakeAccess()
        count = fa.clear_cross_org_skips()
        assert count == 0

    def test_db_clear_error_graceful(self):
        fa = FakeAccess()
        fa._inaccessible_conversations = {"oc_a"}
        fa.store._blacklist_repo.clear_blocked_conversations.side_effect = sqlite3.Error("db down")
        count = fa.clear_cross_org_skips()
        assert count == 1  # 内存仍然清空
        assert len(fa._inaccessible_conversations) == 0
