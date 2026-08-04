"""T3 成本/质量看板测试（Roadmap ③）。

覆盖：
- DecisionsRepo.get_quality_stats：handoff/rag_grounded/cited 计数与率
- cost_quality 路由内部聚合函数：_confidence_hist / _feedback_useful_rate / _citations_recent
- T1 ¥ 换算：MetricsCollector.token_stats 的 total_cost_cny = cost_usd × 汇率
- _work_summary 端到端聚合（monkeypatch app 实例，验证 totals + ¥ 换算 + 空状态）
"""
import tempfile
from types import SimpleNamespace

from src.memory.sqlite_store import SQLiteStore
from src.metrics.collector import MetricsCollector, USD_CNY_RATE
from web.routers import cost_quality as cq


def _make_store():
    d = tempfile.TemporaryDirectory()
    store = SQLiteStore(f"{d.name}/test.db")
    store.init_db()
    return d, store


def _seed(store):
    cur = store.conn.cursor()
    # decisions：4 行总处理；1 handoff / 2 rag_grounded / 1 cited
    rows = [
        ("u1", "c1", "business", "llm", 1, 1, 0),
        ("u2", "c1", "business", "llm", 0, 1, 1),
        ("u3", "c2", "social", "skip", 0, 0, 0),
        ("u4", "c2", "business", "llm", 0, 0, 0),
    ]
    for sender, conv, intent, action, handoff, rag, cited in rows:
        cur.execute(
            "INSERT INTO decisions (sender_id, conversation_id, intent, action, handoff, rag_grounded, cited) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sender, conv, intent, action, handoff, rag, cited),
        )
    # routing_quality：置信度分布（0.05/0.15/0.25/0.95/None）
    for s in [0.05, 0.15, 0.25, 0.95, None]:
        cur.execute(
            "INSERT INTO routing_quality (sender_id, primary_score, input_tokens, output_tokens, total_tokens, cost_usd) "
            "VALUES ('seed', ?, 100, 50, 150, 1.0)",
            (s,),
        )
    # feedback：3 有用 / 1 无用
    for r in (1, 1, 1, -1):
        cur.execute("INSERT INTO feedback (rating) VALUES (?)", (r,))
    store.conn.commit()


# ── T2 聚合（repo 层） ──────────────────────────────────────────────

def test_get_quality_stats_counts_and_rates():
    d, store = _make_store()
    _seed(store)
    q = store._decisions_repo.get_quality_stats()
    assert q["total"] == 4
    assert q["handoff_count"] == 1
    assert q["rag_grounded_count"] == 2
    assert q["cited_count"] == 1
    assert abs(q["handoff_rate"] - 0.25) < 1e-6
    assert abs(q["rag_grounded_rate"] - 0.5) < 1e-6
    assert abs(q["cited_rate"] - 0.25) < 1e-6
    d.cleanup()


def test_get_quality_stats_empty():
    d, store = _make_store()
    q = store._decisions_repo.get_quality_stats()
    assert q["total"] == 0
    assert q["handoff_rate"] == 0.0
    assert q["cited_rate"] == 0.0
    d.cleanup()


# ── 路由内部聚合函数 ───────────────────────────────────────────────

def test_confidence_hist_buckets():
    d, store = _make_store()
    _seed(store)
    hist = cq._confidence_hist(store, 24)
    assert len(hist) == 10
    # 0.05→桶0 / 0.15→桶1 / 0.25→桶2 / 0.95→桶9 / None 忽略
    assert hist[0]["count"] == 1
    assert hist[1]["count"] == 1
    assert hist[2]["count"] == 1
    assert hist[9]["count"] == 1
    assert sum(h["count"] for h in hist) == 4
    d.cleanup()


def test_feedback_useful_rate():
    # 原 cq._feedback_useful_rate 已下沉为 FeedbackRepo.get_useful_rate（行为不变）
    d, store = _make_store()
    _seed(store)
    fb = store._feedback_repo.get_useful_rate()
    assert fb["total"] == 4
    assert fb["useful_count"] == 3
    assert abs(fb["useful_rate"] - 0.75) < 1e-6
    d.cleanup()


def test_citations_recent():
    d, store = _make_store()
    _seed(store)
    res = cq._citations_recent(store, limit=20)
    assert res["total"] == 1
    assert "id" in res["items"][0]
    d.cleanup()


# ── T1 ¥ 换算（collector 层） ──────────────────────────────────────

def test_token_stats_cny_conversion():
    d, store = _make_store()
    cur = store.conn.cursor()
    cur.execute(
        "INSERT INTO routing_quality (sender_id, primary_score, input_tokens, output_tokens, total_tokens, cost_usd) "
        "VALUES ('seed', 0.8, 100, 50, 150, 2.0)"
    )
    store.conn.commit()
    ts = MetricsCollector(store).token_stats(time_range_hours=24)
    assert ts["total_cost_usd"] == 2.0
    assert abs(ts["total_cost_cny"] - round(2.0 * USD_CNY_RATE, 4)) < 1e-6
    d.cleanup()


# ── 端到端 _work_summary（monkeypatch app 实例） ───────────────────

def test_work_summary_aggregation(monkeypatch):
    d, store = _make_store()
    _seed(store)
    fake_app = SimpleNamespace(platforms={"test": SimpleNamespace(store=store)})
    monkeypatch.setattr(cq, "get_app_instance", lambda: fake_app)

    summary = cq._work_summary(24)
    assert summary["available"] is True
    t = summary["totals"]
    # ¥ 换算：routing_quality 5 行 × cost_usd 1.0 = 5.0 USD → × 汇率
    assert abs(t["total_cost_usd"] - 5.0) < 1e-6
    assert abs(t["total_cost_cny"] - round(5.0 * USD_CNY_RATE, 4)) < 1e-6
    # 质量率
    assert t["handoff_count"] == 1
    assert t["rag_grounded_count"] == 2
    assert t["cited_count"] == 1
    assert abs(t["handoff_rate"] - 0.25) < 1e-6
    assert abs(t["rag_grounded_rate"] - 0.5) < 1e-6
    assert abs(t["cited_rate"] - 0.25) < 1e-6
    assert abs(t["feedback_useful_rate"] - 0.75) < 1e-6
    # 置信度分布 10 桶，合计 4（None 不计入）
    assert len(summary["confidence_hist"]) == 10
    assert sum(h["count"] for h in summary["confidence_hist"]) == 4
    d.cleanup()


def test_work_summary_empty(monkeypatch):
    d, store = _make_store()
    fake_app = SimpleNamespace(platforms={"test": SimpleNamespace(store=store)})
    monkeypatch.setattr(cq, "get_app_instance", lambda: fake_app)
    summary = cq._work_summary(24)
    assert summary["available"] is True
    t = summary["totals"]
    assert t["total_cost_cny"] == 0.0
    assert t["handoff_rate"] == 0.0
    assert t["feedback_useful_rate"] == 0.0
    d.cleanup()
