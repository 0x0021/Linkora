"""poller_core_dedup.DedupMixin 单元测试。

覆盖: _is_self_sender, _is_self_message, _is_msg_processed, _mark_msg_processed,
_check_if_bot_message 正向路径 + 边界条件 + 异常处理。
"""

from collections import OrderedDict
from unittest.mock import MagicMock


from src.poller_core_dedup import DedupMixin


class FakeDedup(DedupMixin):
    """最小 fake，只提供 mixin 依赖的属性。"""

    def __init__(self, current_user_id="uid_001", current_user_user_id="uu_001",
                 current_user_name="张三"):
        self.current_user_id = current_user_id
        self.current_user_user_id = current_user_user_id
        self.current_user_name = current_user_name
        self._processed_msg_ids = OrderedDict()


# ============ _is_self_sender ============

class TestIsSelfSender:
    def test_exact_match_user_id(self):
        dup = FakeDedup(current_user_id="uid_001")
        assert dup._is_self_sender("uid_001") is True

    def test_exact_match_user_user_id(self):
        dup = FakeDedup(current_user_user_id="uu_001")
        assert dup._is_self_sender("uu_001") is True

    def test_not_self(self):
        dup = FakeDedup(current_user_id="uid_001")
        assert dup._is_self_sender("uid_999") is False

    def test_empty_sender(self):
        dup = FakeDedup()
        assert dup._is_self_sender("") is False

    def test_none_sender(self):
        dup = FakeDedup()
        assert dup._is_self_sender(None) is False


# ============ _is_self_message ============

def _make_msg(msg_id, sender_id="", sender_name="", content="hello", raw=None, **kw):
    from datetime import datetime
    from src.models import Message
    return Message(msg_id=msg_id, chat_id="c1", chat_type="group", chat_name="群",
                   sender_id=sender_id, sender_name=sender_name, content=content,
                   msg_type="text", timestamp=datetime.now(), raw=raw or {}, **kw)


class TestIsSelfMessage:
    def test_sender_id_match(self):
        dup = FakeDedup(current_user_id="uid_001")
        msg = _make_msg("m1", sender_id="uid_001")
        assert dup._is_self_message(msg) is True

    def test_sender_name_match(self):
        """姓名兜底：sender_id 为空时，按姓名匹配。"""
        dup = FakeDedup(current_user_name="张三")
        msg = _make_msg("m1", sender_id="", sender_name="张三")
        assert dup._is_self_message(msg) is True

    def test_sender_name_strip_match(self):
        """姓名兜底（strip）：sender_id 为空时，按 strip 后姓名匹配。"""
        dup = FakeDedup(current_user_name="张三")
        msg = _make_msg("m1", sender_id="", sender_name=" 张三 ")
        assert dup._is_self_message(msg) is True

    def test_ai_assistant(self):
        dup = FakeDedup()
        msg = _make_msg("m1", sender_id="ou_1", sender_name="AI助手")
        assert dup._is_self_message(msg) is True

    def test_ai_sender_id(self):
        dup = FakeDedup()
        msg = _make_msg("m1", sender_id="ai")
        assert dup._is_self_message(msg) is True

    def test_not_self(self):
        dup = FakeDedup()
        msg = _make_msg("m1", sender_id="uid_999", sender_name="李四")
        assert dup._is_self_message(msg) is False

    def test_raw_sender_match(self):
        dup = FakeDedup(current_user_id="uid_001")
        msg = _make_msg("m1", sender_id="ou_1", sender_name="张三",
                        raw={"senderOpenDingTalkId": "uid_001"})
        assert dup._is_self_message(msg) is True


# ============ _is_msg_processed ============

class TestIsMsgProcessed:
    def test_in_memory_hit_fast_path(self):
        dup = FakeDedup()
        dup._processed_msg_ids["msg_123"] = True
        # 不设 store，应走内存快速路径
        assert dup._is_msg_processed("msg_123") is True

    def test_memory_miss_db_hit(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.store._message_repo.is_message_processed.return_value = True
        assert dup._is_msg_processed("msg_456") is True
        # 应同步到内存
        assert "msg_456" in dup._processed_msg_ids

    def test_both_miss(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.store._message_repo.is_message_processed.return_value = False
        assert dup._is_msg_processed("msg_789") is False

    def test_db_error_graceful(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.store._message_repo.is_message_processed.side_effect = __import__("sqlite3").Error("db down")
        # 无内存记录 + DB 异常 → False，不抛出
        assert dup._is_msg_processed("msg_err") is False

    def test_lru_move_to_end_on_hit(self):
        dup = FakeDedup()
        dup._processed_msg_ids["a"] = True
        dup._processed_msg_ids["b"] = True
        keys_before = list(dup._processed_msg_ids.keys())
        dup._is_msg_processed("a")
        keys_after = list(dup._processed_msg_ids.keys())
        assert keys_before[0] == "a"
        assert keys_after[-1] == "a"  # 移到最后


# ============ _mark_msg_processed ============

class TestMarkMsgProcessed:
    def test_basic(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.config = MagicMock()
        dup.config.max_processed_msg_ids = 1000
        dup._mark_msg_processed("m1", "c1")
        assert "m1" in dup._processed_msg_ids
        dup.store._message_repo.mark_message_processed.assert_called_with("m1", "c1")

    def test_store_error_graceful(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.store._message_repo.mark_message_processed.side_effect = __import__("sqlite3").Error("db down")
        dup.config = MagicMock()
        dup.config.max_processed_msg_ids = 1000
        dup._mark_msg_processed("m1", "c1")
        assert "m1" in dup._processed_msg_ids  # 内存仍写入

    def test_lru_eviction(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.config = MagicMock()
        dup.config.max_processed_msg_ids = 3
        dup._mark_msg_processed("a", "c1")
        dup._mark_msg_processed("b", "c1")
        dup._mark_msg_processed("c", "c1")
        dup._mark_msg_processed("d", "c1")  # 应淘汰最旧的 a
        assert "a" not in dup._processed_msg_ids
        assert "d" in dup._processed_msg_ids

    def test_merged_original_ids(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.config = MagicMock()
        dup.config.max_processed_msg_ids = 1000
        msg = _make_msg("m1", raw={"merged_original_ids": ["orig_a", "orig_b"]})
        dup._mark_msg_processed("m1", "c1", msg=msg)
        assert "orig_a" in dup._processed_msg_ids
        assert "orig_b" in dup._processed_msg_ids


# ============ _check_if_bot_message ============

class TestCheckIfBotMessage:
    def test_db_role_assistant(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {"role": "assistant", "is_bot": 0}
        dup.store.conv_conn.return_value.cursor.return_value = cur
        msg = _make_msg("m1")
        assert dup._check_if_bot_message(msg) is True

    def test_db_is_bot_flag(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = {"role": "user", "is_bot": 1}
        dup.store.conv_conn.return_value.cursor.return_value = cur
        msg = _make_msg("m1")
        assert dup._check_if_bot_message(msg) is True

    def test_db_not_found(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = None
        dup.store.conv_conn.return_value.cursor.return_value = cur
        msg = _make_msg("m1")
        assert dup._check_if_bot_message(msg) is False

    def test_db_error_returns_false(self):
        dup = FakeDedup()
        dup.store = MagicMock()
        dup.store.conv_conn.return_value.cursor.side_effect = __import__("sqlite3").Error("db down")
        msg = _make_msg("m1")
        assert dup._check_if_bot_message(msg) is False


# ============ _max_new_message_age_days（新消息年龄门槛） ============

class FakeAgeDedup(DedupMixin):
    """带 config 的最小 fake。config 用 SimpleNamespace 以便精确控制两个阈值。"""

    def __init__(self, history_days=7, poll_max_age_hours=24):
        from types import SimpleNamespace
        self.config = SimpleNamespace(
            history_days=history_days,
            poll_new_message_max_age_hours=poll_max_age_hours,
        )


class TestMaxNewMessageAgeDays:
    """2026-08-31 事故回归：新消息门槛必须与 history_days 解耦。

    history_days 语义是「取多少天历史当上下文」，7 天合理；但若直接拿它判定
    「这条消息够不够新」，去重一旦漏标（handler 抛错不标记 / 服务中断），
    list-all 会把几天前的老消息重放并与当前新消息合并投喂。
    事故：8-25 截图在 8-31 重放，AI 对当天「VDI 更新后黑屏」回「收到，问题已解决就好。」
    """

    def test_takes_stricter_of_two_limits(self):
        # history_days=7，但 poll 门槛 24h → 取 1 天
        assert FakeAgeDedup(history_days=7, poll_max_age_hours=24)._max_new_message_age_days() == 1.0

    def test_history_days_wins_when_stricter(self):
        # history_days=1 天，poll 门槛 72h → 取 1 天
        assert FakeAgeDedup(history_days=1, poll_max_age_hours=72)._max_new_message_age_days() == 1.0

    def test_six_day_old_message_now_excluded(self):
        """事故核心：6 天前的消息必须被门槛挡住（旧逻辑 history_days=7 会放行）。"""
        limit = FakeAgeDedup(history_days=7, poll_max_age_hours=24)._max_new_message_age_days()
        assert 5.99 > limit  # 6 天老消息超阈值 → 跳过

    def test_recent_message_still_allowed(self):
        """服务离线一夜（<24h）后重启，仍应能补回消息，不能因收紧而漏回。"""
        limit = FakeAgeDedup(history_days=7, poll_max_age_hours=24)._max_new_message_age_days()
        assert 0.5 < limit  # 12 小时前的消息仍在阈值内

    def test_zero_cap_falls_back_to_history_days(self):
        assert FakeAgeDedup(history_days=3, poll_max_age_hours=0)._max_new_message_age_days() == 3.0

    def test_both_disabled_returns_inf(self):
        assert FakeAgeDedup(history_days=0, poll_max_age_hours=0)._max_new_message_age_days() == float("inf")

    def test_missing_attrs_do_not_crash(self):
        """旧配置没有新字段时，getattr 兜底必须不炸。"""
        from types import SimpleNamespace
        dup = DedupMixin()
        dup.config = SimpleNamespace(history_days=7)  # 缺 poll_new_message_max_age_hours
        assert dup._max_new_message_age_days() == 7.0
