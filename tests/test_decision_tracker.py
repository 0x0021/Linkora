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


class TestRefreshFromSqlite:
    def test_refreshes_when_db_newer_than_memory(self):
        """内存只有启动快照（旧，UTC 格式），DB 有更新的本地时间记录时，recent() 应刷新并合并。

        回归：修复前因「空格 vs T」「有无 +00:00」字符串字典序比较，DB 永被判为更旧，
        刷新分支永不触发，多进程下 Web 面板的决策追踪冻结在启动快照上。
        """
        mock_store = MagicMock()
        mock_store._decisions_repo.get_decisions.return_value = {
            "items": [{
                "created_at": "2026-08-15 13:19:07",  # 本地时间，明显晚于内存快照
                "sender_name": "新用户",
                "conversation_name": "新群",
                "content_preview": "新消息内容",
                "intent": "business",
                "action": "llm",
                "sender_id": "su_new",
                "routing_mode": "smart",
                "routed_tools": ["tool_a"],
                "skill_name": "sk",
                "skill_source": "intent",
                "reply_preview": "回复",
                "platform_id": "dingtalk",
            }],
        }
        dt = DecisionTracker(maxlen=50)
        dt.set_sqlite_store(mock_store)
        # 内存里只有一条更早的启动快照（UTC 格式）
        dt._records.append(DecisionRecord(
            ts="2026-08-14T01:00:00+00:00",
            sender="旧用户", chat="旧群", content="旧消息", intent="social", action="skip",
            platform_id="dingtalk",
        ))
        recs = dt.recent(50, "dingtalk")
        # 应合并：旧快照 + 新 DB 记录
        assert len(recs) == 2
        # 最新一条应是 DB 的新记录
        assert recs[-1]["sender"] == "新用户"
        assert recs[-1]["content"] == "新消息内容"

    def test_refreshes_only_requested_platform(self):
        """DB 刷新应按传入 platform_id 过滤，避免多平台数据串台。"""
        mock_store = MagicMock()
        captured = {}

        def fake_get(page_size=20, platform_id=None, **_kw):
            captured["platform_id"] = platform_id
            return {"items": []}

        mock_store._decisions_repo.get_decisions.side_effect = fake_get
        dt = DecisionTracker(maxlen=50)
        dt.set_sqlite_store(mock_store)
        dt._records.append(DecisionRecord(
            ts="2026-08-14T01:00:00+00:00", sender="X", chat="C",
            content="m", intent="social", action="skip", platform_id="feishu",
        ))
        dt.recent(50, "dingtalk")
        assert captured["platform_id"] == "dingtalk"

    def test_normalize_dt_compares_local_and_utc_same_instant(self):
        """同一时刻的本地时间(无时区) 与 UTC(带+00:00) 应被归一化为相等，去重才不会误判。"""
        from src.decision_tracker import _normalize_dt
        # 本地 13:19:07 (UTC+8) 与 UTC 05:19:07 是同一时刻
        a = _normalize_dt("2026-08-14 13:19:07")
        b = _normalize_dt("2026-08-14T05:19:07+00:00")
        assert a == b
        # 方向：DB 本地 8/15 09:00（=UTC 8/15 01:00）晚于 内存 UTC 8/15 00:00
        assert _normalize_dt("2026-08-15 09:00:00") > _normalize_dt("2026-08-15T00:00:00+00:00")
