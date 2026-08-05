"""poller_core_discovery.DiscoveryMixin 单元测试。

覆盖: _get_recent_conversations_from_db 正向路径 + 边界条件 + 异常处理。
"""

from unittest.mock import MagicMock


from src.poller_core_discovery import DiscoveryMixin


class FakeDiscovery(DiscoveryMixin):
    """最小 fake，只提供 mixin 依赖的属性。"""

    def __init__(self):
        self.store = MagicMock()
        self.config = MagicMock()
        self.dws = MagicMock()
        self._inaccessible_conversations = set()
        self._last_list_all_time = None


# ============ _get_recent_conversations_from_db ============

class TestGetRecentConversationsFromDb:
    def test_returns_valid_oc_conversations(self):
        fd = FakeDiscovery()
        fd.store._conversation_repo.get_recent_conversations.return_value = [
            {"chat_id": "oc_abc123", "chat_type": "single", "title": "张三"},
            {"chat_id": "oc_def456", "chat_type": "group", "title": "项目群"},
        ]
        result = fd._get_recent_conversations_from_db()
        assert len(result) == 2
        assert result[0]["openConversationId"] == "oc_abc123"
        assert result[0]["singleChat"] is True
        assert result[1]["openConversationId"] == "oc_def456"
        assert result[1]["singleChat"] is False

    def test_filters_non_oc_prefix(self):
        fd = FakeDiscovery()
        fd.store._conversation_repo.get_recent_conversations.return_value = [
            {"chat_id": "oc_ok", "chat_type": "single", "title": "A"},
            {"chat_id": "ou_bad", "chat_type": "single", "title": "B"},
            {"chat_id": "cid_bad", "chat_type": "group", "title": "C"},
        ]
        result = fd._get_recent_conversations_from_db()
        # 现在放行 oc_ 和 cid* 前缀（钉钉兼容），仅过滤 ou_ 等非会话级 ID
        assert len(result) == 2
        assert result[0]["openConversationId"] == "oc_ok"
        assert result[1]["openConversationId"] == "cid_bad"

    def test_empty_result(self):
        fd = FakeDiscovery()
        fd.store._conversation_repo.get_recent_conversations.return_value = []
        assert fd._get_recent_conversations_from_db() == []

    def test_db_error_graceful(self):
        fd = FakeDiscovery()
        fd.store._conversation_repo.get_recent_conversations.side_effect = RuntimeError("db down")
        assert fd._get_recent_conversations_from_db() == []

    def test_missing_chat_id(self):
        fd = FakeDiscovery()
        fd.store._conversation_repo.get_recent_conversations.return_value = [
            {"chat_type": "single", "title": "无ID"},
        ]
        result = fd._get_recent_conversations_from_db()
        assert result == []


# ============ list-all 时间窗钳制 ============

class TestFetchViaListAllWindowClamp:
    """验证 _fetch_messages_via_list_all 把超宽时间窗钳制到最近 N 天，
    避免实时轮询循环每轮重扫全部历史、永远撞分页上限刷警告。"""

    def _make_fd(self):
        from datetime import datetime
        fd = FakeDiscovery()
        # 提供必要的真实 int 配置（MagicMock 会让 timedelta(days=MagicMock()) 报错）
        fd.config.list_all_first_run_minutes = 5
        fd.config.list_all_max_window_days = 14
        fd.config.list_all_max_pages = 50
        fd.config.empty_poll_protection_minutes = 5
        fd.config.list_all_full_scan_interval_minutes = 60
        fd._last_list_all_time = datetime(2026, 7, 10, 9, 56, 25)  # 卡死的旧游标
        fd.store._conversation_repo.get_recent_conversations.return_value = []
        fd.dws.chat_message_list_all.return_value = {
            "conversationMessagesList": [], "hasMore": False, "nextCursor": "",
        }
        return fd

    def test_clamps_overwide_window(self):
        from datetime import datetime, timedelta
        fd = self._make_fd()
        captured = {}

        def spy(start, end, limit=100, max_pages=None, chat_ids=None, chat_meta=None):
            captured["start"] = start
            captured["max_pages"] = max_pages
            return {"conversationMessagesList": [], "hasMore": False, "nextCursor": ""}

        fd.dws.chat_message_list_all.side_effect = spy

        fd._fetch_messages_via_list_all()

        # 起点应被钳制到最近 14 天，而非卡死的 2026-07-10
        clamped_floor = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
        assert captured["start"] is not None
        assert captured["start"] != "2026-07-10 09:56:25"
        assert captured["start"] >= clamped_floor, captured["start"]
        assert captured["max_pages"] == 50

    def test_no_clamp_for_recent_window(self):
        """窗口本就较窄时不钳制（增量游标接近 now）。"""
        from datetime import datetime, timedelta
        fd = self._make_fd()
        fd._last_list_all_time = datetime.now() - timedelta(minutes=30)
        captured = {}

        def spy(start, end, limit=100, max_pages=None, chat_ids=None, chat_meta=None):
            captured["start"] = start
            return {"conversationMessagesList": [], "hasMore": False, "nextCursor": ""}

        fd.dws.chat_message_list_all.side_effect = spy

        fd._fetch_messages_via_list_all()

        expected = (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        assert captured["start"] == expected
