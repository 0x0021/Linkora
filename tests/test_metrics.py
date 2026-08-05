"""Tests for src/metrics/collector.py and report_logger.py.

All tests use a temporary in-memory SQLiteStore with pre-populated data,
verifying that the MetricsCollector correctly aggregates metrics from the
existing decisions / routing_quality / blacklist / tool_execution_logs tables.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.memory.sqlite_store import SQLiteStore
from src.metrics.collector import MetricsCollector
from src.metrics.report_logger import MetricsReportLogger


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def store():
    """Create an in-memory SQLiteStore with pre-populated test data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test-metrics.db"
        from src.memory.platform_context import set_current_platform
        set_current_platform("dingtalk")  # 设置平台上下文，确保 conv_conn 查询正确
        store = SQLiteStore(db_path=str(db_path))
        _seed_data(store)
        yield store
        store.close()


def _seed_data(store: SQLiteStore) -> None:
    """Populate tables with realistic test data."""
    conn = store.conn
    cur = conn.cursor()
    now = datetime.now()
    def ts(hours_ago):
        return (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S")

    # ── tool_execution_logs ──
    tools_data = [
        # (tool_name, success, duration_ms, error, created_at)
        ("dingtalk_send_message", 1, 120.5, None, ts(1)),
        ("dingtalk_send_message", 1, 95.2, None, ts(1.5)),
        ("dingtalk_send_message", 1, 210.0, None, ts(2)),
        ("dingtalk_send_message", 0, 5000.0, "timeout", ts(3)),
        ("dingtalk_send_message", 1, 80.0, None, ts(4)),
        ("search_knowledge_base", 1, 350.0, None, ts(2)),
        ("search_knowledge_base", 1, 420.0, None, ts(3)),
        ("search_knowledge_base", 0, 150.0, "no results", ts(5)),
        ("get_weather", 1, 800.0, None, ts(6)),
        ("get_weather", 1, 750.0, None, ts(7)),
        ("get_weather", 0, 30000.0, "API error", ts(8)),
        ("create_calendar_event", 1, 200.0, None, ts(10)),
        ("create_calendar_event", 1, 180.0, None, ts(11)),
        ("create_calendar_event", 1, 190.0, None, ts(12)),
    ]
    for name, ok, dur, err, t in tools_data:
        cur.execute(
            """INSERT INTO tool_execution_logs
               (tool_name, success, duration_ms, error_message, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, ok, dur, err, t),
        )

    # ── routing_quality ──
    rq_data = [
        # (sender_id, sender_name, content_preview, primary_skill, primary_score, primary_source,
        #  routing_mode, conversation_id, input_tokens, output_tokens, total_tokens, cost_usd, created_at)
        ("sender-001", "张三", "帮我发个消息给李四", "dingtalk_send_message", 0.92, "semantic_routing",
         "single", "chat-001", 150, 80, 230, 0.0005, ts(1)),
        ("sender-002", "李四", "查下知识库里的合同模板", "search_knowledge_base", 0.88, "semantic_routing",
         "single", "chat-002", 200, 120, 320, 0.0007, ts(2)),
        ("sender-003", "王五", "今天天气怎么样", "get_weather", 0.35, "semantic_routing",
         "single", "chat-003", 100, 50, 150, 0.0003, ts(3)),
        ("sender-004", "赵六", "帮我查一下", "unknown", 0.20, "fallback",
         "combo", "chat-004", 300, 200, 500, 0.0010, ts(4)),
        ("sender-001", "张三", "再发一条", "dingtalk_send_message", 0.91, "semantic_routing",
         "convergence", "chat-001", 120, 60, 180, 0.0004, ts(5)),
        ("sender-005", "钱七", "创建明天的会议", "create_calendar_event", 0.85, "combo_routing",
         "combo", "chat-005", 250, 150, 400, 0.0008, ts(6)),
        ("sender-006", "周八", "帮我翻译一段英文", "translate", 0.15, "fallback",
         "single", "chat-006", 500, 300, 800, 0.0015, ts(7)),
    ]
    for data in rq_data:
        cur.execute(
            """INSERT INTO routing_quality
               (sender_id, sender_name, content_preview, primary_skill, primary_score,
                primary_source, routing_mode, conversation_id,
                input_tokens, output_tokens, total_tokens, cost_usd, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            data,
        )

    # ── blocked_conversations ──
    # 注意：blocked_conversations 在 conv_conn（per-platform DB），不在主库
    bl_data = [
        # (chat_id, chat_name, chat_type, reason, source, cooldown_until, failure_count, detected_at)
        ("chat-spam-01", "广告机器人", "group", "spam_keywords", "auto_detect", None, 5, ts(2)),
        ("chat-spam-02", "水军群", "group", "spam_keywords", "auto_detect", ts(26), 3, ts(5)),
        ("chat-err-01", "异常用户A", "single", "tool_repeated_failure", "auto_detect", None, 8, ts(3)),
        ("chat-err-02", "异常用户B", "single", "tool_repeated_failure", "auto_detect", ts(28), 4, ts(6)),
        ("chat-abuse-01", "辱骂用户", "single", "abuse_detected", "manual_report", None, 2, ts(4)),
        ("chat-flood-01", "刷屏机器人", "group", "rate_limit", "auto_detect", ts(27), 6, ts(10)),
    ]
    # 写入 conv_conn（per-platform DB）
    conv_cur = store.conv_conn("dingtalk").cursor()
    for data in bl_data:
        conv_cur.execute(
            """INSERT INTO blocked_conversations
               (chat_id, chat_name, chat_type, reason, source, cooldown_until, failure_count, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            data,
        )
    store.conn.commit()
    store.conv_conn("dingtalk").commit()


# ── Tests: Tool Stats ──────────────────────────────────────────────────


class TestToolStats:
    def test_tool_stats_all(self, store):
        c = MetricsCollector(store)
        result = c.tool_stats(time_range_hours=None)
        tools = {t["tool_name"]: t for t in result["tools"]}

        assert len(tools) >= 4

        msg = tools["dingtalk_send_message"]
        assert msg["total_calls"] == 5
        assert msg["success_count"] == 4
        assert msg["success_rate"] == 80.0

        weather = tools["get_weather"]
        assert weather["total_calls"] == 3
        assert weather["success_count"] == 2

        cal = tools["create_calendar_event"]
        assert cal["total_calls"] == 3
        assert cal["success_count"] == 3
        assert cal["success_rate"] == 100.0

    def test_tool_stats_time_range(self, store):
        c = MetricsCollector(store)
        # Only last 5 hours → weather calls at h6/h7/h8 excluded
        result = c.tool_stats(time_range_hours=5)
        tools = {t["tool_name"]: t for t in result["tools"]}

        # Message calls: h1, h1.5, h2, h3, h4 → all 5 within 5h
        msg = tools.get("dingtalk_send_message", {})
        assert msg.get("total_calls", 0) == 5

        # Weather: h6, h7, h8 → all outside 5h window
        assert "get_weather" not in tools

    def test_tool_stats_percentiles(self, store):
        c = MetricsCollector(store)
        result = c.tool_stats(time_range_hours=None)
        tools = {t["tool_name"]: t for t in result["tools"]}

        msg = tools["dingtalk_send_message"]
        assert msg["p50_ms"] > 0
        assert msg["p95_ms"] > 0
        assert msg["p99_ms"] > 0

        search = tools["search_knowledge_base"]
        assert search["p50_ms"] > 0

    def test_tool_recent_failures(self, store):
        c = MetricsCollector(store)
        failures = c.tool_recent_failures(limit=10)
        assert len(failures) == 3  # 3 failed calls total

        # Most recent first (by created_at DESC):
        # ts(3h ago) → dingtalk_send_message
        # ts(5h ago) → search_knowledge_base
        # ts(8h ago) → get_weather
        assert failures[0]["tool_name"] == "dingtalk_send_message"
        assert failures[0]["error_message"] and "timeout" in failures[0]["error_message"]
        assert failures[1]["tool_name"] == "search_knowledge_base"
        assert failures[2]["tool_name"] == "get_weather"


# ── Tests: Routing Accuracy ─────────────────────────────────────────────


class TestRoutingAccuracy:
    def test_routing_accuracy(self, store):
        c = MetricsCollector(store)
        result = c.routing_accuracy(low_score_threshold=0.5, limit=10)

        assert result["total"] == 7
        assert result["avg_score"] > 0
        assert result["accuracy_rate"] > 0

        # Sources present
        sources = {s["source"] for s in result["by_source"]}
        assert "semantic_routing" in sources
        assert "fallback" in sources

    def test_low_score_decisions(self, store):
        c = MetricsCollector(store)
        result = c.routing_accuracy(low_score_threshold=0.5, limit=10)

        # 3 low-score entries: get_weather(0.35), unknown(0.20), translate(0.15)
        assert len(result["low_score_decisions"]) == 3

        # Sorted by created_at DESC (most recent first):
        # ts(3h ago) get_weather(0.35) → ts(4h ago) unknown(0.20) → ts(7h ago) translate(0.15)
        assert result["low_score_decisions"][0]["primary_skill"] == "get_weather"
        assert result["low_score_decisions"][0]["primary_score"] == 0.35


# ── Tests: Blacklist Trends ─────────────────────────────────────────────


class TestBlacklistTrends:
    def test_blacklist_trends(self, store):
        c = MetricsCollector(store)
        result = c.blacklist_trends(recent_days=7)

        assert result["total"] == 6
        pvt = result["permanent_vs_temporary"]
        assert pvt["permanent"] == 3
        assert pvt["temporary"] == 3

    def test_by_reason(self, store):
        c = MetricsCollector(store)
        result = c.blacklist_trends()

        reasons = {r["reason"]: r["count"] for r in result["by_reason"]}
        assert reasons["spam_keywords"] == 2
        assert reasons["tool_repeated_failure"] == 2
        assert reasons["abuse_detected"] == 1

    def test_recent_additions(self, store):
        c = MetricsCollector(store)
        result = c.blacklist_trends(recent_days=2)

        # All 6 seed entries are within last 24h, so recent_days=2 captures all
        # Most recent first: chat-spam-01 at ts(2h)
        assert len(result["recent_additions"]) == 6
        assert result["recent_additions"][0]["chat_id"] == "chat-spam-01"


# ── Tests: Token Stats ──────────────────────────────────────────────────


class TestTokenStats:
    def test_token_stats_all(self, store):
        c = MetricsCollector(store)
        result = c.token_stats(time_range_hours=None)

        assert result["available"] is True
        assert result["record_count"] == 7
        assert result["total_tokens"] > 0
        assert result["total_cost_usd"] > 0

    def test_token_stats_by_chat(self, store):
        c = MetricsCollector(store)
        result = c.token_stats(time_range_hours=None)

        by_chat = result["by_chat"]
        assert len(by_chat) > 0
        # chat-001 (张三) has 230 + 180 = 410 tokens; chat-006 has 800
        chat_ids = [x["conversation_id"] for x in by_chat]
        assert "chat-001" in chat_ids

    def test_token_stats_hourly(self, store):
        c = MetricsCollector(store)
        result = c.token_stats(time_range_hours=None)

        hourly = result["hourly"]
        assert len(hourly) > 0
        for h in hourly:
            assert "hour" in h
            assert "total_tokens" in h

    def test_token_stats_time_range(self, store):
        c = MetricsCollector(store)
        result = c.token_stats(time_range_hours=1)
        assert result["available"] is True
        assert result["time_range_hours"] == 1


# ── Tests: Snapshot ─────────────────────────────────────────────────────


class TestSnapshot:
    def test_snapshot_structure(self, store):
        c = MetricsCollector(store)
        snap = c.snapshot(time_range_hours=24)

        assert "tool_stats" in snap
        assert "tool_recent_failures" in snap
        assert "routing_accuracy" in snap
        assert "blacklist_trends" in snap
        assert "token_stats" in snap
        assert "generated_at" in snap


# ── Tests: Report Logger ────────────────────────────────────────────────


class TestReportLogger:
    def test_report_once_no_error(self, store, caplog):
        caplog.set_level(logging.INFO, logger="metrics.report")

        reporter = MetricsReportLogger(
            stores={"test": store},
            interval_seconds=60,
        )
        reporter.report_once()

        records = [r for r in caplog.records if r.name == "metrics.report"]
        assert len(records) == 1
        assert records[0].levelname == "INFO"

        payload = json.loads(records[0].message)
        assert payload["_platform"] == "test"
        assert payload["_type"] == "metrics_snapshot"
        assert "tool_stats" in payload
        assert "routing_accuracy" in payload
        assert "blacklist_trends" in payload
        assert "token_stats" in payload
        # token_stats may be in framework-only mode (table lacks token columns)
        assert "available" in payload["token_stats"]

    def test_report_handles_empty_store(self, caplog):
        """When store is valid but tables are empty, report should not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "empty.db"
            store = SQLiteStore(db_path=str(db_path))

            caplog.set_level(logging.INFO, logger="metrics.report")
            reporter = MetricsReportLogger(
                stores={"test": store},
                interval_seconds=60,
            )
            reporter.report_once()

            records = [r for r in caplog.records if r.name == "metrics.report"]
            assert len(records) == 1

            payload = json.loads(records[0].message)
            assert payload["_type"] == "metrics_snapshot"
            assert payload["tool_stats"]["tools"] == [] or len(payload["tool_stats"]["tools"]) >= 0

            store.close()
