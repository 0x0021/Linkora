"""DecisionTracker 测试：记录 / 恢复 / 持久化 / 清空。"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.decision_tracker import DecisionRecord, DecisionTracker, tracker


class TestDecisionTracker:
    def test_record_minimal(self):
        dt = DecisionTracker(maxlen=5)
        dt.record(sender="张三", chat="测试群", content="你好", intent="social.greeting", action="llm")
        recs = dt.recent()
        assert len(recs) == 1
        assert recs[0]["sender"] == "张三"

    def test_record_assigns_ts_default(self):
        dt = DecisionTracker()
        dt.record(sender="李四", chat="群", content="test", intent="business", action="reply-rule")
        rec = dt.recent(1)[0]
        assert "T" in rec["ts"]  # ISO format

    def test_record_filter_unknown_fields(self):
        """未知字段被过滤，不写入 DecisionRecord（不掉这行）。"""
        dt = DecisionTracker()
        dt.record(sender="A", chat="X", content="hi", intent="social", action="llm",
                  unknown_field="should_be_ignored")
        rec = dt.recent(1)[0]
        assert "unknown_field" not in rec

    def test_recent_maxlen_enforced(self):
        dt = DecisionTracker(maxlen=3)
        for i in range(5):
            dt.record(sender="T", chat="C", content=f"m{i}", intent="x", action="skip")
        recs = dt.recent()
        assert len(recs) == 3

    def test_clear(self):
        dt = DecisionTracker(maxlen=5)
        dt.record(sender="X", chat="Y", content="z", intent="t", action="skip")
        dt.clear()
        assert dt.recent() == []

    def test_record_with_sqlite_store(self):
        """录制时同时持久化到 SQLite。"""
        mock_store = MagicMock()
        dt = DecisionTracker(maxlen=5)
        dt.set_sqlite_store(mock_store)
        dt.record(sender="A", chat="B", content="test", intent="business", action="llm",
                  sender_id="u1", routing_mode="smart", routed_tools=["tool_a", "tool_b"],
                  skill_name="my-skill", skill_source="intent", reply_preview="你好",
                  conversation_id="conv1")
        mock_store._decisions_repo.record_decision.assert_called_once()
        call_args = mock_store._decisions_repo.record_decision.call_args[1]
        assert call_args["routed_tools"] == ["tool_a", "tool_b"]

    def test_record_sqlite_exception_silenced(self):
        """持久化失败不抛出异常。"""
        mock_store = MagicMock()
        mock_store._decisions_repo.record_decision.side_effect = RuntimeError("DB down")
        dt = DecisionTracker(maxlen=5)
        dt.set_sqlite_store(mock_store)
        # 不抛异常
        dt.record(sender="A", chat="B", content="c", intent="x", action="skip")
        recs = dt.recent()
        assert len(recs) == 1

    def test_recent_fallback_sqlite(self):
        """内存为空时回退 SQLite 恢复记录。"""
        mock_store = MagicMock()
        mock_store._decisions_repo.get_decisions.return_value = {
            "items": [{
                "created_at": "2026-07-13T10:00:00",
                "sender_name": "历史用户",
                "conversation_name": "历史群",
                "content_preview": "历史消息",
                "intent": "business",
                "action": "llm",
                "sender_id": "su1",
                "routing_mode": "all",
                "routed_tools": ["tool_x"],
                "skill_name": "sk",
                "skill_source": "explicit",
                "reply_preview": "回复预览",
            }],
        }
        dt = DecisionTracker(maxlen=50)
        dt.set_sqlite_store(mock_store)
        recs = dt.recent()
        assert len(recs) == 1
        assert recs[0]["sender"] == "历史用户"

    def test_recent_sqlite_exception_silenced(self):
        """SQLite 回退异常返回空列表。"""
        mock_store = MagicMock()
        mock_store._decisions_repo.get_decisions.side_effect = RuntimeError("DB error")
        dt = DecisionTracker(maxlen=5)
        dt.set_sqlite_store(mock_store)
        assert dt.recent() == []


class TestSingletonTracker:
    def test_tracker_is_decision_tracker(self):
        assert isinstance(tracker, DecisionTracker)
        assert tracker.recent() == []  # 空记录不影响


class TestDecisionRecord:
    def test_defaults(self):
        record = DecisionRecord(
            ts="2026-07-13T00:00:00",
            sender="S",
            chat="C",
            content="text",
            intent="social",
            action="skip",
        )
        assert record.sender_id == ""
        assert record.routing_mode is None
        assert record.routed_tools is None
        assert record.skill_name is None
