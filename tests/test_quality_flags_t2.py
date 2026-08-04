"""T2 质量标记（handoff / rag_grounded / cited）落库与回填测试。

覆盖 Roadmap ③ 成本/质量看板的质量埋点：
- DecisionsRepo.record_decision 必须持久化 handoff / rag_grounded / cited 三列；
- DecisionTracker.record 必须把这三项从 kw 透传给 record_decision；
- mark_cited 能按 request_id（优先）或 (platform_id, conversation_id) 回退定位
  刚写入的决策行并原地 UPDATE cited。
"""
import tempfile

from src.memory.sqlite_store import SQLiteStore
from src.decision_tracker import DecisionTracker


def _make_store():
    d = tempfile.TemporaryDirectory()
    store = SQLiteStore(f"{d.name}/test.db")
    store.init_db()
    return d, store


def _quality_row(store):
    cur = store.conn.cursor()
    cur.execute("SELECT handoff, rag_grounded, cited FROM decisions")
    return tuple(cur.fetchone())


def test_record_decision_persists_quality_flags():
    d, store = _make_store()
    store._decisions_repo.record_decision(
        sender_id="u1", sender_name="A", conversation_id="c1",
        intent="business", action="llm",
        handoff=1, rag_grounded=1, cited=0,
    )
    assert _quality_row(store) == (1, 1, 0)
    d.cleanup()


def test_tracker_record_forwards_quality_flags():
    d, store = _make_store()
    t = DecisionTracker()
    t.set_sqlite_store(store)
    t.record(sender="A", chat="B", content="x", intent="business", action="llm",
             handoff=1, rag_grounded=1)
    # 未调用 mark_cited 时 cited 默认 0
    assert _quality_row(store) == (1, 1, 0)
    d.cleanup()


def test_tracker_record_defaults_quality_flags_zero():
    d, store = _make_store()
    t = DecisionTracker()
    t.set_sqlite_store(store)
    t.record(sender="A", chat="B", content="x", intent="business", action="skip")
    assert _quality_row(store) == (0, 0, 0)
    d.cleanup()


def test_mark_cited_by_request_id():
    d, store = _make_store()
    repo = store._decisions_repo
    repo.record_decision(sender_id="u1", conversation_id="c1",
                         intent="business", action="llm",
                         request_id="req-xyz", platform_id="dingtalk")
    n = repo.mark_cited(request_id="req-xyz", platform_id="dingtalk",
                        conversation_id="c1", cited=1)
    assert n == 1
    cur = store.conn.cursor()
    cur.execute("SELECT cited FROM decisions WHERE request_id=?", ("req-xyz",))
    assert cur.fetchone()[0] == 1
    d.cleanup()


def test_mark_cited_fallback_by_conversation():
    d, store = _make_store()
    repo = store._decisions_repo
    repo.record_decision(sender_id="u1", conversation_id="c9",
                         intent="business", action="llm", platform_id="feishu")
    n = repo.mark_cited(platform_id="feishu", conversation_id="c9", cited=1)
    assert n == 1
    cur = store.conn.cursor()
    cur.execute("SELECT cited FROM decisions WHERE conversation_id='c9'")
    assert cur.fetchone()[0] == 1
    d.cleanup()


def test_mark_cited_no_match_returns_zero():
    d, store = _make_store()
    repo = store._decisions_repo
    assert repo.mark_cited(request_id="nope", conversation_id="c", cited=1) == 0
    d.cleanup()
