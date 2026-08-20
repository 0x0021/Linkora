"""poller_core_discovery.DiscoveryMixin 单元测试。

覆盖: _get_recent_conversations_from_db 正向路径 + 边界条件 + 异常处理。
"""

from datetime import datetime
from unittest.mock import MagicMock

from src.models import Message
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
        fd.store._conversation_repo.get_recent_conversations.side_effect = __import__("sqlite3").Error("db down")
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


# ============ list-all 必须落库本人发出的消息（2026-08-10 真因回归） ============

OWNER_ID = "DDn1bYIeDbcGDY6HwqBjreyVz776bLniS4"


class TestFetchViaListAllStoresSelfMessage:
    """回归：list-all 路径曾直接丢弃本人发出的消息（_is_self_sender → continue），
    导致「我主动发给别人的消息」永远不进聊天记录（尤其对方尚未回复的新会话，
    如新同事入职首条消息：list-all 能拉到该会话但消息被丢弃 → 会话空有壳无消息）。

    修复后：本人消息走统一的 _store_self_message_if_new 落库（保留上下文），
    但不进入 new_messages（不触发 AI 回复）。本测试锁定该行为。
    """

    def _make_fd(self):
        fd = FakeDiscovery()
        # 真实 int 配置（避免 timedelta(days=MagicMock()) 报错）
        fd.config.list_all_first_run_minutes = 5
        fd.config.list_all_max_window_days = 14
        fd.config.list_all_max_pages = 50
        fd.config.empty_poll_protection_minutes = 5
        fd.config.list_all_full_scan_interval_minutes = 60
        fd.config.history_days = 30
        fd.config.merge_window_seconds = 60
        # 固定起点为今日 0 点，避免 list-all 时间窗（now-5min）因测试 now 略早而误杀
        # 本次探嗅的「刚刚收到」消息；本人消息分支在时间窗过滤前已落库，不受此影响。
        fd._last_list_all_time = datetime(2026, 8, 10, 0, 0, 0)
        # 通知签名层需要真实 list（MagicMock 会让 `x in mock` 恒为真，误杀消息）
        fd.config.skip_notification_patterns = []
        fd.config.skip_notification_sender_ids = []
        fd._last_full_scan_time = None
        fd._last_poll_time = {}
        # 本人身份（_is_self_message 以此判定）
        fd.current_user_id = OWNER_ID
        fd.current_user_user_id = OWNER_ID
        fd.current_user_name = "徐宇坤"
        fd.get_current_platform = MagicMock(return_value="dingtalk")
        # 基类里跨 mixin 方法是无实现桩（运行时返回 None），这里补真实行为
        fd._is_self_message = lambda m: bool(m.sender_id) and m.sender_id == OWNER_ID
        fd._is_self_sender = lambda sid: bool(sid) and sid == OWNER_ID
        # _store_self_message_if_new 真实实现（PollerStrategyMixin 提供，DiscoveryMixin 无）
        def _store_self(msg):
            is_bot = bool(fd._check_if_bot_message(msg))
            msg.is_bot = is_bot
            msg.role = "assistant" if is_bot else "user"
            if not fd._is_duplicate_self_message(msg):
                fd.store._message_repo.save_message(msg, msg.role)
        fd._store_self_message_if_new = _store_self
        fd._merge_consecutive_messages = lambda msgs, window_seconds=60: msgs
        # 仓库双写/去重桩
        fd.store._message_repo.is_message_processed.return_value = False
        fd.store._message_repo.mark_message_processed.return_value = True
        fd.store._conversation_repo.upsert_conversation.return_value = None
        fd._is_duplicate_self_message = MagicMock(return_value=False)
        # 循环内辅助方法桩
        fd._detect_chat_type = MagicMock(return_value="single")
        fd._feishu_correct_chat_type = MagicMock(side_effect=lambda cid, t, ct: ct)
        fd._is_blacklisted_conversation = MagicMock(return_value=False)
        fd._is_blocked = MagicMock(return_value=False)
        fd._effective_skip_types = MagicMock(return_value=set())
        fd._is_at_me = MagicMock(return_value=True)
        return fd

    def _self_msg(self):
        return Message(
            msg_id="self_RID1", chat_id="cidX", chat_type="single", chat_name="徐冰洁",
            sender_id=OWNER_ID, sender_name="徐宇坤", content="你好，欢迎加入",
            msg_type="text", timestamp=datetime(2026, 8, 10, 10, 0, 0), raw={},
        )

    def test_self_outgoing_is_stored_not_dropped(self):
        fd = self._make_fd()
        self_msg = self._self_msg()
        fd._raw_to_message = MagicMock(return_value=self_msg)
        fd.dws.chat_message_list_all.return_value = {
            "conversationMessagesList": [{
                "openConversationId": "cidX", "title": "徐冰洁",
                "chatType": "single",
                "messages": [{"openMessageId": "self_RID1", "senderId": OWNER_ID,
                              "content": "你好，欢迎加入", "msgType": "text",
                              "createTime": "2026-08-10 10:00:00"}],
            }],
            "hasMore": False, "nextCursor": "",
        }

        result = fd._fetch_messages_via_list_all()

        # 关键：本人发出的消息必须落库（修复前此处被 continue 静默丢弃）
        fd.store._message_repo.save_message.assert_called_once()
        saved_msg, saved_role = fd.store._message_repo.save_message.call_args.args
        assert saved_msg is self_msg
        assert saved_role == "user"  # 真人手动发出，非 bot
        # 关键：本人消息不得进入回复派发队列（不应触发 AI 自回）
        assert result == []

    def test_peer_incoming_still_routed_to_reply(self):
        """对照：他人发来的消息照常进入 new_messages 触发 AI 回复（行为不被破坏）。"""
        fd = self._make_fd()
        # 用当前时间，避免被 list-all 时间窗过滤（>start_time）丢弃
        now = datetime.now()
        peer_msg = Message(
            msg_id="peer_RID2", chat_id="cidX", chat_type="single", chat_name="徐冰洁",
            sender_id="other_id_xxx", sender_name="徐冰洁", content="谢谢老板",
            msg_type="text", timestamp=now, raw={},
        )
        fd._raw_to_message = MagicMock(return_value=peer_msg)
        fd.dws.chat_message_list_all.return_value = {
            "conversationMessagesList": [{
                "openConversationId": "cidX", "title": "徐冰洁",
                "chatType": "single",
                "messages": [{"openMessageId": "peer_RID2", "senderId": "other_id_xxx",
                              "content": "谢谢老板", "msgType": "text",
                              "createTime": now.strftime("%Y-%m-%d %H:%M:%S")}],
            }],
            "hasMore": False, "nextCursor": "",
        }

        result = fd._fetch_messages_via_list_all()

        # 他人消息：进入 new_messages 触发回复
        assert len(result) == 1
        assert result[0] is peer_msg
        # peer 消息不应被当作本人消息落库
        fd.store._message_repo.save_message.assert_not_called()
