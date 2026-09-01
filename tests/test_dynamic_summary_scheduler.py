"""动态（信号驱动）摘要调度器 + message_repo 收集逻辑的回归测试。

覆盖：
- get_chats_needing_dynamic_summary 的三类触发信号（静默/体量/陈旧）与排除条件；
- collect_dynamic_summary_messages 的「自 last_summary_at 起增量」收集（含无起点回退）；
- DynamicSummaryScheduler 的 per-chat 去重与写回（CAS + last_summary_at 更新）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.memory.sqlite_store import SQLiteStore
from src.llm.dynamic_summary_scheduler import DynamicSummaryJob, DynamicSummaryScheduler


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_store(tmp_path):
    return SQLiteStore(db_path=str(tmp_path / "linkora.db"))


def _insert_conversation(cur, chat_id, chat_name, message_count, last_summary_at, updated_at):
    cur.execute(
        """INSERT OR REPLACE INTO conversations
           (chat_id, chat_name, chat_type, message_count, last_summary_at, created_at, updated_at)
           VALUES (?, ?, 'single', ?, ?, ?, ?)""",
        (chat_id, chat_name, message_count, last_summary_at, _iso(datetime.now(timezone.utc)), updated_at),
    )


def _insert_message(cur, chat_id, msg_id, content, ts, is_archived=0):
    cur.execute(
        """INSERT OR REPLACE INTO messages
           (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, timestamp, role, is_archived, created_at)
           VALUES (?, 'single', ?, 'u1', 'user', ?, 'text', ?, 'user', ?, ?)""",
        (chat_id, msg_id, content, ts, is_archived, _iso(datetime.now(timezone.utc))),
    )


def test_get_chats_needs_dynamic_signal_filtering(tmp_path):
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    # A: 静默+新内容（last_summary_at=None，最后一条 20 分钟前，5 条未摘要）→ 应命中
    _insert_conversation(cur, "A", "chatA", 5, None, _iso(now))
    for i in range(5):
        _insert_message(cur, "A", f"a{i}", f"a{i}", _iso(now - timedelta(minutes=20) + timedelta(seconds=i)))
    # B: 已摘要且新增不足 min_messages（last_summary_at=now，仅 1 条新）→ 排除
    _insert_conversation(cur, "B", "chatB", 5, _iso(now), _iso(now))
    for i in range(5):
        _insert_message(cur, "B", f"b{i}", f"b{i}", _iso(now - timedelta(minutes=30) + timedelta(seconds=i)))
    _insert_message(cur, "B", "b_new", "b_new", _iso(now - timedelta(seconds=10)))  # 仅 1 条新
    # C: 消息总数不足 min_messages → 排除
    _insert_conversation(cur, "C", "chatC", 2, None, _iso(now))
    _insert_message(cur, "C", "c0", "c0", _iso(now - timedelta(minutes=20)))
    _insert_message(cur, "C", "c1", "c1", _iso(now - timedelta(minutes=19)))
    # D: 体量触发（150 条未摘要，活跃中也命中）
    _insert_conversation(cur, "D", "chatD", 150, None, _iso(now))
    for i in range(150):
        _insert_message(cur, "D", f"d{i}", f"d{i}", _iso(now - timedelta(seconds=i)))
    store._message_repo._cc().commit()

    chats = store._message_repo.get_chats_needing_dynamic_summary(
        quiet_minutes=10, min_messages=3, max_messages_per_chat=100, max_age_hours=24,
    )
    ids = {c["chat_id"] for c in chats}
    assert "A" in ids, "静默+新内容应命中"
    assert "D" in ids, "体量超限应命中"
    assert "B" not in ids, "已摘要且新增不足应排除"
    assert "C" not in ids, "消息数不足阈值应排除"


def test_collect_dynamic_since_last_summary(tmp_path):
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    boundary = now - timedelta(minutes=15)
    _insert_conversation(cur, "X", "chatX", 5, _iso(boundary), _iso(now))
    # 3 条早于边界（应排除）+ 2 条晚于边界（应收集）
    for i in range(3):
        _insert_message(cur, "X", f"x_old{i}", f"old{i}", _iso(boundary - timedelta(minutes=1) - timedelta(seconds=i)))
    for i in range(2):
        _insert_message(cur, "X", f"x_new{i}", f"new{i}", _iso(boundary + timedelta(minutes=1) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    msgs = store._message_repo.collect_dynamic_summary_messages("X", max_messages=100)
    assert [m.content for m in msgs] == ["new0", "new1"], "应只收集 last_summary_at 之后的增量"


def test_collect_dynamic_fallback_without_boundary(tmp_path):
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "Y", "chatY", 3, None, _iso(now))
    for i in range(3):
        _insert_message(cur, "Y", f"y{i}", f"y{i}", _iso(now - timedelta(minutes=5) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    msgs = store._message_repo.collect_dynamic_summary_messages("Y", max_messages=100)
    assert len(msgs) == 3, "无起点时回退收集最近 N 条"


class _FakeAgent:
    def __init__(self, summary="SUMMARY_TEXT"):
        self.summary = summary
        self.calls = 0

    def summarize_conversation(self, messages, max_messages=0):
        self.calls += 1
        return self.summary


def test_scheduler_process_job_writes_back_and_marks(tmp_path):
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "Z", "chatZ", 5, None, _iso(now))
    for i in range(5):
        _insert_message(cur, "Z", f"z{i}", f"z{i}", _iso(now - timedelta(minutes=20) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    agent = _FakeAgent()
    sched = DynamicSummaryScheduler(agent=agent, store=store, platform="")
    sched._process_job_inner(DynamicSummaryJob(chat_id="Z"))

    row = store._conversation_repo.get_conversation_summary("Z")
    assert row is not None
    assert row.summary_text == "SUMMARY_TEXT"
    assert row.covered_count == 5

    # mark_conversation_summarized 应更新 last_summary_at，避免下一轮重复
    cur.execute("SELECT last_summary_at FROM conversations WHERE chat_id = ?", ("Z",))
    assert cur.fetchone()["last_summary_at"] is not None
    assert agent.calls == 1


def test_scheduler_pending_dedup(tmp_path):
    store = _make_store(tmp_path)
    sched = DynamicSummaryScheduler(agent=_FakeAgent(), store=store, platform="")
    sched._enqueue("dup", "signal")
    sched._enqueue("dup", "signal")  # 同 chat 在途，应被去重
    # 取队列验证仅 1 个 job
    jobs = []
    while True:
        try:
            item = sched._queue.get_nowait()
        except Exception:
            break
        if item is not None:
            jobs.append(item)
    assert len(jobs) == 1, "per-chat pending 去重应只入队一次"
