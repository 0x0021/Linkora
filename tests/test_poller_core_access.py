"""poller_core_access.AccessControlMixin 单元测试。

覆盖: _is_blocked, _classify_inaccessible_reason, _block_conversation, clear_cross_org_skips。
"""

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
        fa.store._blacklist_repo.add_blocked_conversation.side_effect = RuntimeError("db down")
        fa._block_conversation("oc_jkl", "群", "group", RuntimeError("130003"))
        assert "oc_jkl" in fa._inaccessible_conversations  # 内存仍写入


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
        fa.store._blacklist_repo.clear_blocked_conversations.side_effect = RuntimeError("db down")
        count = fa.clear_cross_org_skips()
        assert count == 1  # 内存仍然清空
        assert len(fa._inaccessible_conversations) == 0
