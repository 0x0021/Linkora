"""DecisionsRepo 仓储层单测。

覆盖此前测试盲区（原覆盖率 52%）：过滤子句构造、分页查询、
按 request_id 追踪、统计聚合、筛选下拉、CSV 导出、留存清理、
质量标记回填与聚合。

统一用真实临时 SQLite 库（而非 mock cursor），确保 SQL 本身也被验证——
仓储层的价值恰恰在 SQL 正确性，mock 掉 cursor 等于什么都没测。
"""
from __future__ import annotations

import pytest

from src.memory.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_db_path):
    s = SQLiteStore(db_path=str(tmp_db_path))
    yield s
    try:
        s.close()
    except Exception:
        pass


@pytest.fixture
def repo(store):
    return store._decisions_repo


def _seed(repo, n=3, **overrides):
    """写入 n 条决策记录，返回 id 列表。"""
    ids = []
    for i in range(n):
        kw = {
            "sender_id": f"u{i}",
            "sender_name": f"用户{i}",
            "conversation_id": f"conv{i}",
            "conversation_name": f"会话{i}",
            "content_preview": f"内容{i}",
            "intent": "chat",
            "action": "reply",
        }
        kw.update(overrides)
        ids.append(repo.record_decision(**kw))
    return ids


# ── record_decision ──

class TestRecordDecision:
    def test_returns_rowid_and_persists(self, repo):
        rid = repo.record_decision(sender_id="u1", sender_name="张三", intent="chat")
        assert isinstance(rid, int) and rid > 0
        got = repo.get_decisions(page=1, page_size=10)
        assert got["total"] == 1
        assert got["items"][0]["sender_name"] == "张三"

    def test_routed_tools_list_serialized_to_json(self, repo):
        """routed_tools 传 list 时应 JSON 序列化落库，读回时还原为 list。"""
        repo.record_decision(sender_id="u1", routed_tools=["web_search", "weather"])
        item = repo.get_decisions()["items"][0]
        assert item["routed_tools"] == ["web_search", "weather"]

    def test_routed_tools_str_passthrough(self, repo):
        """已是字符串时原样落库；非法 JSON 读回降级为空列表而不是抛错。"""
        repo.record_decision(sender_id="u1", routed_tools="not-json")
        item = repo.get_decisions()["items"][0]
        assert item["routed_tools"] == []

    def test_none_fields_coerced_to_empty_and_zero(self, repo):
        """None 入参不应写入 NULL（列非空契约），而是归一为 "" / 0。"""
        repo.record_decision(sender_id="u1", sender_name=None, llm_calls=None,
                             tool_calls=None, handoff=None)
        item = repo.get_decisions()["items"][0]
        assert item["sender_name"] == ""

    def test_prune_triggered_every_200_inserts(self, repo, monkeypatch):
        calls = []
        monkeypatch.setattr(repo, "_prune_decisions", lambda: calls.append(1))
        for _ in range(200):
            repo.record_decision(sender_id="u")
        assert len(calls) == 1


# ── _build_filter_clause ──

class TestBuildFilterClause:
    def test_no_filter_returns_empty_where(self):
        from src.memory.decisions_repo import DecisionsRepo
        where, params = DecisionsRepo._build_filter_clause()
        assert where == ""
        assert params == []

    def test_each_field_appends_condition(self):
        from src.memory.decisions_repo import DecisionsRepo
        where, params = DecisionsRepo._build_filter_clause(
            sender_name="张三", conversation_id="c1", intent="chat", action="reply")
        assert where.startswith("WHERE ")
        assert where.count(" AND ") == 3
        assert params == ["张三", "c1", "chat", "reply"]

    def test_time_filter_today_uses_local_date(self):
        from src.memory.decisions_repo import DecisionsRepo
        where, params = DecisionsRepo._build_filter_clause(time_filter="today")
        assert "DATE(created_at) = DATE('now', 'localtime')" in where
        assert params == []  # 时间条件不带参数

    def test_time_filter_month_uses_strftime(self):
        from src.memory.decisions_repo import DecisionsRepo
        where, _ = DecisionsRepo._build_filter_clause(time_filter="month")
        assert "strftime('%Y-%m', created_at)" in where

    def test_unknown_time_filter_ignored(self):
        from src.memory.decisions_repo import DecisionsRepo
        where, _ = DecisionsRepo._build_filter_clause(time_filter="yesterday")
        assert where == ""


# ── get_decisions 分页与过滤 ──

class TestGetDecisions:
    def test_pagination_slices_correctly(self, repo):
        _seed(repo, 5)
        page1 = repo.get_decisions(page=1, page_size=2)
        page3 = repo.get_decisions(page=3, page_size=2)
        assert page1["total"] == 5 and len(page1["items"]) == 2
        assert len(page3["items"]) == 1
        assert page1["page"] == 1 and page1["page_size"] == 2

    def test_filter_by_sender_name(self, repo):
        _seed(repo, 3)
        got = repo.get_decisions(sender_name="用户1")
        assert got["total"] == 1
        assert got["items"][0]["sender_name"] == "用户1"

    def test_filter_by_intent_and_action(self, repo):
        repo.record_decision(sender_id="u1", intent="weather", action="tool")
        repo.record_decision(sender_id="u2", intent="chat", action="reply")
        assert repo.get_decisions(intent="weather")["total"] == 1
        assert repo.get_decisions(action="reply")["total"] == 1

    def test_empty_table_returns_zero_total(self, repo):
        got = repo.get_decisions()
        assert got == {"items": [], "total": 0, "page": 1, "page_size": 20}


# ── query_decisions_by_rid ──

class TestQueryByRequestId:
    def test_empty_rid_returns_empty_without_query(self, repo):
        assert repo.query_decisions_by_rid("") == []

    def test_returns_rows_in_ascending_id_order(self, repo):
        repo.record_decision(sender_id="u1", request_id="rid-1", content_preview="第一步")
        repo.record_decision(sender_id="u1", request_id="rid-1", content_preview="第二步")
        repo.record_decision(sender_id="u2", request_id="rid-2")
        rows = repo.query_decisions_by_rid("rid-1")
        assert len(rows) == 2
        assert [r["content_preview"] for r in rows] == ["第一步", "第二步"]
        assert rows[0]["id"] < rows[1]["id"]

    def test_limit_respected(self, repo):
        for _ in range(5):
            repo.record_decision(sender_id="u", request_id="rid-x")
        assert len(repo.query_decisions_by_rid("rid-x", limit=2)) == 2

    def test_malformed_routed_tools_degrades_to_empty_list(self, repo):
        repo.record_decision(sender_id="u", request_id="rid-b", routed_tools="{bad")
        assert repo.query_decisions_by_rid("rid-b")[0]["routed_tools"] == []


# ── 统计与筛选项 ──

class TestStats:
    def test_stats_group_by_intent_action_sender(self, repo):
        repo.record_decision(sender_id="u1", sender_name="张三", intent="chat", action="reply")
        repo.record_decision(sender_id="u1", sender_name="张三", intent="chat", action="reply")
        repo.record_decision(sender_id="u2", sender_name="李四", intent="weather", action="tool")
        s = repo.get_decisions_stats()
        assert s["total"] == 3
        assert s["by_intent"] == {"chat": 2, "weather": 1}
        assert s["by_action"] == {"reply": 2, "tool": 1}
        assert s["by_sender"]["张三"] == 2

    def test_empty_intent_labeled_none(self, repo):
        repo.record_decision(sender_id="u1", intent="")
        assert repo.get_decisions_stats()["by_intent"] == {"(none)": 1}

    def test_skill_stats_only_when_activated(self, repo):
        repo.record_decision(sender_id="u1")  # 无技能
        s = repo.get_decisions_stats()
        assert s["skill_activated"] == 0 and s["by_skill"] == {}
        repo.record_decision(sender_id="u2", skill_name="日报助手")
        s2 = repo.get_decisions_stats()
        assert s2["skill_activated"] == 1
        assert s2["by_skill"] == {"日报助手": 1}

    def test_filter_options_excludes_empty_and_dedups(self, repo):
        repo.record_decision(sender_id="u1", sender_name="张三", intent="chat", action="reply")
        repo.record_decision(sender_id="u2", sender_name="张三", intent="", action="reply")
        opts = repo.get_filter_options()
        assert opts["senders"] == ["张三"]
        assert opts["intents"] == ["chat"]
        assert opts["actions"] == ["reply"]


# ── 导出 ──

class TestExport:
    def test_export_columns_contract(self, repo):
        from src.memory.decisions_repo import DecisionsRepo
        repo.record_decision(sender_id="u1", sender_name="张三")
        rows = repo.export_decisions()
        assert len(rows) == 1
        assert tuple(rows[0].keys()) == DecisionsRepo.EXPORT_COLUMNS

    def test_export_shares_filter_with_pagination(self, repo):
        """导出与分页必须用同一套过滤条件，否则两处口径会漂移。"""
        _seed(repo, 3)
        paged = repo.get_decisions(sender_name="用户1")["total"]
        exported = len(repo.export_decisions(sender_name="用户1"))
        assert paged == exported == 1

    def test_export_limit(self, repo):
        _seed(repo, 5)
        assert len(repo.export_decisions(limit=2)) == 2


# ── 留存清理 ──

class TestCleanup:
    def test_set_retention_days(self, repo):
        repo.set_decisions_retention_days(7)
        assert repo.retention_days == 7
        assert repo._decisions_retention_days == 7

    def test_cleanup_deletes_records_older_than_retention(self, repo, store):
        repo.record_decision(sender_id="old")
        store.conn.execute(
            "UPDATE decisions SET created_at = datetime('now', '-100 days', 'localtime')")
        store.conn.commit()
        repo.record_decision(sender_id="new")
        res = repo.cleanup_old_records(decisions_retention_days=30)
        assert res["decisions_deleted"] == 1
        assert res["decisions_remaining"] == 1

    def test_cleanup_zero_days_skips_time_based_delete(self, repo, store):
        repo.record_decision(sender_id="old")
        store.conn.execute(
            "UPDATE decisions SET created_at = datetime('now', '-100 days', 'localtime')")
        store.conn.commit()
        res = repo.cleanup_old_records(decisions_retention_days=0)
        assert res["decisions_deleted"] == 0
        assert res["decisions_remaining"] == 1

    def test_hard_cap_trims_oldest_first(self, repo):
        repo._hard_cap = 3
        ids = _seed(repo, 5)
        res = repo.cleanup_old_records(decisions_retention_days=0)
        assert res["decisions_remaining"] == 3
        remaining = {i["id"] for i in repo.get_decisions(page_size=50)["items"]}
        assert ids[0] not in remaining and ids[1] not in remaining

    def test_cleanup_swallows_db_error(self, repo, store):
        store.conn.close()  # 制造异常
        res = repo.cleanup_old_records()
        assert res == {"decisions_deleted": 0, "decisions_remaining": 0}

    def test_prune_respects_hard_cap(self, repo):
        repo._hard_cap = 2
        _seed(repo, 4)
        repo._prune_decisions()
        assert repo.get_decisions(page_size=50)["total"] == 2


# ── 质量标记 ──

class TestQualityFlags:
    def test_mark_cited_by_request_id(self, repo):
        repo.record_decision(sender_id="u1", request_id="rid-1")
        assert repo.mark_cited(request_id="rid-1", cited=1) == 1
        assert repo.get_quality_stats()["cited_count"] == 1

    def test_mark_cited_fallback_to_conversation(self, repo):
        repo.record_decision(sender_id="u1", platform_id="dingtalk", conversation_id="c1")
        assert repo.mark_cited(platform_id="dingtalk", conversation_id="c1", cited=1) == 1
        assert repo.get_quality_stats()["cited_count"] == 1

    def test_mark_cited_without_keys_returns_zero(self, repo):
        assert repo.mark_cited(cited=1) == 0

    def test_mark_cited_targets_latest_row_only(self, repo):
        repo.record_decision(sender_id="u1", request_id="rid-1")
        repo.record_decision(sender_id="u1", request_id="rid-1")
        repo.mark_cited(request_id="rid-1", cited=1)
        assert repo.get_quality_stats()["cited_count"] == 1

    def test_mark_cited_swallows_error(self, repo, store):
        store.conn.close()
        assert repo.mark_cited(request_id="x", cited=1) == 0

    def test_quality_stats_rates(self, repo):
        repo.record_decision(sender_id="u1", handoff=1, rag_grounded=1, cited=1)
        repo.record_decision(sender_id="u2")
        s = repo.get_quality_stats()
        assert s["total"] == 2
        assert s["handoff_rate"] == 0.5
        assert s["rag_grounded_rate"] == 0.5
        assert s["cited_rate"] == 0.5

    def test_quality_stats_empty_table_all_zero(self, repo):
        s = repo.get_quality_stats()
        assert s["total"] == 0
        assert s["handoff_rate"] == 0.0  # 不能除零

    def test_quality_stats_time_range_filters(self, repo, store):
        repo.record_decision(sender_id="old", handoff=1)
        store.conn.execute(
            "UPDATE decisions SET created_at = datetime('now', '-10 days')")
        store.conn.commit()
        repo.record_decision(sender_id="new")
        assert repo.get_quality_stats(time_range_hours=24)["total"] == 1

    def test_quality_stats_swallows_error(self, repo, store):
        store.conn.close()
        assert repo.get_quality_stats()["total"] == 0

    def test_recent_cited_only_returns_cited(self, repo):
        repo.record_decision(sender_id="u1", request_id="r1", reply_preview="有引文")
        repo.record_decision(sender_id="u2", reply_preview="无引文")
        repo.mark_cited(request_id="r1", cited=1)
        rows = repo.get_recent_cited()
        assert len(rows) == 1
        assert rows[0]["reply_preview"] == "有引文"

    def test_recent_cited_swallows_error(self, repo, store):
        store.conn.close()
        assert repo.get_recent_cited() == []

    def test_daily_handoff_stats(self, repo, store):
        repo.record_decision(sender_id="u1", handoff=1)
        repo.record_decision(sender_id="u2", handoff=0)
        cur = store.conn.execute("SELECT DATE(created_at) AS d FROM decisions LIMIT 1")
        today = cur.fetchone()["d"]
        s = repo.get_daily_handoff_stats(today)
        assert s == {"total": 2, "handoff_count": 1}

    def test_daily_handoff_stats_unknown_day_is_zero(self, repo):
        assert repo.get_daily_handoff_stats("1999-01-01") == {"total": 0, "handoff_count": 0}

    def test_quality_flags_since_hours(self, repo):
        repo.record_decision(sender_id="u1", conversation_id="c1", handoff=1)
        rows = repo.get_quality_flags_since_hours(24)
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "c1"
        assert rows[0]["handoff"] == 1

    def test_quality_flags_excludes_out_of_window(self, repo, store):
        repo.record_decision(sender_id="u1")
        store.conn.execute(
            "UPDATE decisions SET created_at = datetime('now', 'localtime', '-48 hours')")
        store.conn.commit()
        assert repo.get_quality_flags_since_hours(24) == []


# ── 向后兼容属性 ──

class TestBackwardCompatProperties:
    def test_insert_count_property_roundtrip(self, repo):
        repo._decision_insert_count = 42
        assert repo._decision_insert_count == 42
        assert repo._insert_count == 42

    def test_hard_cap_property_coerces_int(self, repo):
        repo._decisions_hard_cap = "500"
        assert repo._decisions_hard_cap == 500

    def test_retention_days_property_coerces_int(self, repo):
        repo._decisions_retention_days = "15"
        assert repo._decisions_retention_days == 15
