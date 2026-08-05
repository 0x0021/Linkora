"""poller_core_history.HistorySyncMixin 单元测试。

覆盖: _handle_edit_message, _handle_recall_message 正向路径 + 边界条件 + 异常处理。
"""

from datetime import datetime
from unittest.mock import MagicMock


from src.models import Message
from src.poller_core_history import HistorySyncMixin


def _mk(msg_id, content="", raw=None, chat_id="c1"):
    return Message(msg_id=msg_id, chat_id=chat_id, chat_type="group", chat_name="群",
                   sender_id="s1", sender_name="张三", content=content,
                   msg_type="text", timestamp=datetime.now(), raw=raw or {})


class FakeHistory(HistorySyncMixin):
    def __init__(self):
        self.store = MagicMock()
        self.config = MagicMock()
        self._processed_msg_ids = {}


class TestHandleEditMessage:
    def test_edit_with_original_msg_id(self):
        fh = FakeHistory()
        fh.store._message_repo.update_message.return_value = True
        msg = _mk("edit_1", raw={"originalMsgId": "orig_1", "newContent": "修改后的内容"})
        fh._handle_edit_message(msg)
        fh.store._message_repo.update_message.assert_called_with("orig_1", "修改后的内容")

    def test_edit_with_target_msg_id(self):
        fh = FakeHistory()
        fh.store._message_repo.update_message.return_value = True
        msg = _mk("edit_2", raw={"targetMsgId": "target_1", "newContent": "新内容"})
        fh._handle_edit_message(msg)
        fh.store._message_repo.update_message.assert_called_with("target_1", "新内容")

    def test_edit_fallback_to_msg_id(self):
        fh = FakeHistory()
        fh.store._message_repo.update_message.return_value = True
        msg = _mk("edit_3", content="直接内容", raw={"newContent": "直接内容"})
        fh._handle_edit_message(msg)
        fh.store._message_repo.update_message.assert_called_with("edit_3", "直接内容")

    def test_edit_update_failed(self):
        fh = FakeHistory()
        fh.store._message_repo.update_message.return_value = False
        _mk("edit_4", raw={"originalMsgId": "orig_4", "newContent": "c"})
        # 不抛异常即可

    def test_edit_no_content_skips(self):
        fh = FakeHistory()
        msg = _mk("edit_5", raw={"originalMsgId": "orig_5"})
        fh._handle_edit_message(msg)
        fh.store._message_repo.update_message.assert_not_called()


class TestHandleRecallMessage:
    def test_recall_with_recalled_msg_id(self):
        fh = FakeHistory()
        fh.store._message_repo.delete_message.return_value = True
        msg = _mk("recall_1", raw={"recalledMsgId": "orig_recall_1"})
        fh._handle_recall_message(msg)
        fh.store._message_repo.delete_message.assert_called_with("orig_recall_1")

    def test_recall_fallback_to_target_msg_id(self):
        fh = FakeHistory()
        fh.store._message_repo.delete_message.return_value = True
        msg = _mk("recall_2", raw={"targetMsgId": "targ_2"})
        fh._handle_recall_message(msg)
        fh.store._message_repo.delete_message.assert_called_with("targ_2")

    def test_recall_fallback_to_msg_id(self):
        fh = FakeHistory()
        fh.store._message_repo.delete_message.return_value = True
        msg = _mk("recall_3")
        fh._handle_recall_message(msg)
        fh.store._message_repo.delete_message.assert_called_with("recall_3")

    def test_recall_delete_failed(self):
        fh = FakeHistory()
        fh.store._message_repo.delete_message.return_value = False
        _mk("recall_4", raw={"recalledMsgId": "r4"})
        # 不抛异常即可

    def test_recall_marks_raw_id_processed(self):
        fh = FakeHistory()
        fh.store._message_repo.delete_message.return_value = True
        fh.store._message_repo.mark_message_processed.return_value = True
        msg = _mk("recall_5", chat_id="chat_1",
                  raw={"recalledMsgId": "r5", "openMessageId": "open_r5"})
        fh._handle_recall_message(msg)
        fh.store._message_repo.mark_message_processed.assert_called_with("open_r5", "chat_1")


class TestBuildSyncWindows:
    """sync_history 分窗：宽窗口必须拆窗，避免单窗触顶 list-all 50 页上限截断。"""

    NOW = datetime(2026, 8, 3, 15, 9, 10)

    def _make(self):
        return HistorySyncMixin()

    def test_range_wide_window_split(self):
        """range days=24 应拆成多个 ≤7 天窗（旧实现单窗直拉，活跃组织触顶漏消息）。"""
        h = self._make()
        w = h._build_sync_windows(self.NOW, 24)
        assert len(w) == 4  # 24 天 / 7 天窗，向上取整
        for s, e in w:
            d = (datetime.fromisoformat(e) - datetime.fromisoformat(s)).days
            assert d <= h.SYNC_WINDOW_DAYS, f"窗长超限: {d}"

    def test_range_small_window_single(self):
        """days ≤ SYNC_WINDOW_DAYS 保持单窗（不无谓拆碎）。"""
        h = self._make()
        w = h._build_sync_windows(self.NOW, 3)
        assert len(w) == 1
        assert w[0][1] == self.NOW.strftime("%Y-%m-%d %H:%M:%S")

    def test_windows_cover_full_span_contiguously(self):
        """多窗首尾相接、无缝隙无重叠，整体覆盖 [now-days, now]。"""
        h = self._make()
        days = 24
        w = h._build_sync_windows(self.NOW, days)
        # 首窗终点 = now
        assert w[0][1] == self.NOW.strftime("%Y-%m-%d %H:%M:%S")
        # 相邻窗首尾相接
        for i in range(len(w) - 1):
            assert w[i][0] == w[i + 1][1], f"窗 {i} 与 {i+1} 不连续"
        # 末窗起点 ≈ now - days
        last_start = datetime.fromisoformat(w[-1][0])
        diff = (self.NOW - last_start).days
        assert days - 1 <= diff <= days, f"覆盖天数 {diff} ≠ {days}"

    def test_full_mode_split(self):
        """full 模式按 SYNC_WINDOW_DAYS 逐窗直到 SYNC_FULL_LOOKBACK_DAYS。"""
        h = self._make()
        w = h._build_sync_windows(self.NOW, None)
        expect = -(-h.SYNC_FULL_LOOKBACK_DAYS // h.SYNC_WINDOW_DAYS)  # 向上取整
        assert len(w) == expect
        assert w[-1][0].startswith("2024-08")  # 730 天前
