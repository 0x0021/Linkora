"""展示用全量摘要调度器 + conversation_display_summaries 仓库方法回归测试。

覆盖：
- repo 层：upsert/get/list 展示摘要（独立表，不与 H2-A conversation_summaries 串台）；
- DisplaySummaryScheduler 收集「近期全量窗口」并写回展示表（解耦、覆盖整段对话，
  而非仅 older 段），且新鲜度护栏命中时跳过重复摘要；
- 删除会话时联动清理展示摘要行。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from src.memory.sqlite_store import SQLiteStore
from src.llm.display_summary_scheduler import DisplaySummaryJob, DisplaySummaryScheduler


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


def _insert_message(cur, chat_id, msg_id, content, ts):
    cur.execute(
        """INSERT OR REPLACE INTO messages
           (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, timestamp, role, is_archived, created_at)
           VALUES (?, 'single', ?, 'u1', 'user', ?, 'text', ?, 'user', 0, ?)""",
        (chat_id, msg_id, content, ts, _iso(datetime.now(timezone.utc))),
    )


class _FakeAgent:
    def __init__(self, summary="DISPLAY_SUMMARY"):
        self.summary = summary
        self.calls = 0

    def summarize_conversation(self, messages, max_messages=0):
        self.calls += 1
        # 验证：传入的是「近期全量窗口」而非 older 段（消息顺序应保持）
        return self.summary + f"[{len(messages)}]"


def test_display_summary_repo_upsert_and_get(tmp_path):
    store = _make_store(tmp_path)
    repo = store._conversation_repo
    ok = repo.upsert_display_summary("C1", "摘要A", "m5", 5)
    assert ok is True
    row = repo.get_display_summary("C1")
    assert row is not None
    assert row.summary_text == "摘要A"
    assert row.covered_count == 5

    # 更新（读取当前代际作 CAS 期望值，自增代际）
    gen = repo.get_display_summary("C1").generation
    ok2 = repo.upsert_display_summary("C1", "摘要B", "m9", 9, expected_generation=gen)
    assert ok2 is True
    row2 = repo.get_display_summary("C1")
    assert row2.summary_text == "摘要B"
    assert row2.covered_count == 9
    assert row2.generation == 2


def test_display_summary_isolated_from_h2a_table(tmp_path):
    store = _make_store(tmp_path)
    repo = store._conversation_repo
    # 同一 chat 在两张表各有不同摘要，互不影响
    repo.upsert_conversation_summary("C2", "context-only", "mx", 3)
    repo.upsert_display_summary("C2", "display-only", "my", 12)
    disp = repo.get_display_summary("C2")
    ctx = repo.get_conversation_summary("C2")
    assert disp.summary_text == "display-only" and disp.covered_count == 12
    assert ctx.summary_text == "context-only" and ctx.covered_count == 3


def test_display_summary_list_recent(tmp_path):
    store = _make_store(tmp_path)
    repo = store._conversation_repo
    for i, (cid, _name) in enumerate((("X1", "a"), ("X2", "b"))):
        repo.upsert_display_summary(cid, f"sum{i}", f"m{i}", i + 1)
    rows = repo.list_recent_display_summaries(limit=10, platform="")
    assert {r["chat_id"] for r in rows} == {"X1", "X2"}


def test_scheduler_collects_full_recent_window(tmp_path):
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    # 11 条消息（超过默认 older 段，远多于 H2-A 的 4 条），模拟「整段对话」
    _insert_conversation(cur, "Z", "chatZ", 11, None, _iso(now))
    for i in range(11):
        _insert_message(cur, "Z", f"z{i}", f"msg{i}", _iso(now - timedelta(minutes=30) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    agent = _FakeAgent()
    sched = DisplaySummaryScheduler(agent=agent, store=store, platform="", display_limit=40)
    sched._process_job_inner(DisplaySummaryJob(chat_id="Z"))

    row = store._conversation_repo.get_display_summary("Z")
    assert row is not None
    # 覆盖全部 11 条（而非仅 older 段），修掉「只取一部分」的问题
    assert row.covered_count == 11
    assert "DISPLAY_SUMMARY[11]" == row.summary_text
    assert agent.calls == 1


def test_scheduler_skips_when_fresh(tmp_path):
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "Z", "chatZ", 5, None, _iso(now))
    for i in range(5):
        _insert_message(cur, "Z", f"z{i}", f"z{i}", _iso(now - timedelta(minutes=20) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    agent = _FakeAgent()
    sched = DisplaySummaryScheduler(agent=agent, store=store, platform="", freshness_seconds=9999)
    # 先生成一次
    sched._process_job_inner(DisplaySummaryJob(chat_id="Z"))
    assert agent.calls == 1
    # 紧接再跑：已新鲜且覆盖充分 → 不应再次调 LLM
    sched._process_job_inner(DisplaySummaryJob(chat_id="Z"))
    assert agent.calls == 1


def test_scheduler_skips_idle_chat_even_if_stale(tmp_path):
    """无新消息时必须跳过——不能因「摘要超过 freshness_seconds」就周期性重摘要。

    P0 回归：旧逻辑 `covered>=total and age<=freshness` 会在摘要过期后重算，
    导致全部空闲会话每 30 分钟被重新摘要一次（实测 924 个会话 → 持续的 LLM 调用
    与写库洪峰，抢占轮询器写锁）。
    """
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "I", "chatI", 5, None, _iso(now))
    for i in range(5):
        _insert_message(cur, "I", f"i{i}", f"i{i}", _iso(now - timedelta(minutes=30) + timedelta(seconds=i)))
    store._conversation_repo.upsert_display_summary("I", "旧摘要", "i4", 5)
    # 把 updated_at 改到 2 天前（远超 freshness_seconds=60），仍应跳过
    cur.execute(
        "UPDATE conversation_display_summaries SET updated_at = ? WHERE chat_id = 'I'",
        (_iso(now - timedelta(days=2)),),
    )
    store._message_repo._cc().commit()

    agent = _FakeAgent()
    sched = DisplaySummaryScheduler(agent=agent, store=store, platform="", freshness_seconds=60)
    sched._process_job_inner(DisplaySummaryJob(chat_id="I"))
    assert agent.calls == 0, "空闲会话（无新消息）不应被周期性重摘要"


def test_scheduler_resummarizes_when_new_messages(tmp_path):
    """有新消息时必须重摘要（覆盖范围变大）。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "J", "chatJ", 6, None, _iso(now))
    for i in range(6):
        _insert_message(cur, "J", f"j{i}", f"j{i}", _iso(now - timedelta(minutes=30) + timedelta(seconds=i)))
    store._message_repo._cc().commit()
    # 已有摘要只覆盖 5 条（第 6 条是新消息）
    store._conversation_repo.upsert_display_summary("J", "旧摘要", "j4", 5)

    agent = _FakeAgent()
    sched = DisplaySummaryScheduler(agent=agent, store=store, platform="", display_limit=40)
    sched._process_job_inner(DisplaySummaryJob(chat_id="J"))
    assert agent.calls == 1, "有新消息应重摘要"
    assert store._conversation_repo.get_display_summary("J").covered_count == 6


def test_delete_conversation_cleans_display_summary(tmp_path):
    store = _make_store(tmp_path)
    repo = store._conversation_repo
    repo.upsert_display_summary("D1", "d", "m1", 1)
    assert repo.get_display_summary("D1") is not None
    repo.delete_conversations(["D1"], platform="")
    assert repo.get_display_summary("D1") is None


def test_list_recent_filters_by_activity_not_summary_time(tmp_path):
    """「今天」筛选器必须按会话活动时间(boundary_ts)过滤，而非摘要生成时间(updated_at)。

    复现根因：展示调度器会把 updated_at 批量刷成「今天」，若按 updated_at 过滤，
    则「今天」会返回全部会话、筛选器形同虚设。本测试断言：两会话的 updated_at 都被刷成今天，
    但只有活动时间落在今天的会话才会出现在 window=today 的结果里。
    """
    from src.memory.conversation_repo import _since_iso

    store = _make_store(tmp_path)
    repo = store._conversation_repo
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    today = now.replace(hour=10, minute=0, second=0, microsecond=0)
    five_days_ago = now - timedelta(days=5)

    # C1：今天有活动
    _insert_conversation(cur, "C1", "today-chat", 3, None, _iso(now))
    for i in range(3):
        _insert_message(cur, "C1", f"c1-{i}", f"m{i}", _iso(today + timedelta(seconds=i)))
    # C2：活动停在 5 天前，但 updated_at 被调度器刷成「今天」（模拟批量刷新）
    _insert_conversation(cur, "C2", "old-chat", 3, None, _iso(now))
    for i in range(3):
        _insert_message(cur, "C2", f"c2-{i}", f"m{i}", _iso(five_days_ago + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    # 两个会话的 updated_at 都是「今天」（调度器行为），boundary_ts 分别为今天/5天前
    repo.upsert_display_summary("C1", "sum-c1", "c1-2", 3, boundary_ts=_iso(today))
    repo.upsert_display_summary("C2", "sum-c2", "c2-2", 3, boundary_ts=_iso(five_days_ago))

    since_today = _since_iso("today")
    rows = repo.list_recent_display_summaries(limit=50, platform="", since=since_today)
    got = {r["chat_id"] for r in rows}
    # C2 活动时间不在今天 → 被过滤掉，即使其 updated_at 是今天
    assert "C1" in got
    assert "C2" not in got

    # 7 天窗口两者都应出现（C2 活动在 5 天前，落在区间内）
    since_7d = _since_iso("7days")
    rows7 = repo.list_recent_display_summaries(limit=50, platform="", since=since_7d)
    assert {"C1", "C2"} <= {r["chat_id"] for r in rows7}


# ── 旧库迁移回归（P0：曾导致全网 500）────────────────────────────────────
# 根因：CREATE TABLE IF NOT EXISTS 对「已存在但无 boundary_ts 的旧表」是空操作，
# 若在同批次/之前就执行 CREATE INDEX ...(boundary_ts)，会 "no such column: boundary_ts"
# 让 init_schema 整体抛错。而 Web 每次请求都调 init_db()→init_schema，于是所有
# /api/messages、/api/dashboard/stream-data 全部 500。
# 这些用例专门用「缺列旧表」启动，锁死「先补列、后建索引」的顺序。


def _make_legacy_db(path: str) -> None:
    """造一个旧版库：conversation_display_summaries 无 boundary_ts（模拟升级前的真实库）。"""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE conversations (
            chat_id TEXT PRIMARY KEY, chat_name TEXT, chat_type TEXT NOT NULL,
            peer_user_id TEXT, peer_open_dingtalk_id TEXT, last_message_time TEXT,
            message_count INTEGER DEFAULT 0, last_reply_time TEXT,
            last_replied_msg_id TEXT, last_summary_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            chat_type TEXT, msg_id TEXT UNIQUE, sender_id TEXT, sender_name TEXT,
            content TEXT, msg_type TEXT, timestamp TEXT, role TEXT
        );
        CREATE TABLE conversation_summaries (
            chat_id TEXT PRIMARY KEY, summary_text TEXT NOT NULL,
            older_boundary_msg_id TEXT NOT NULL, covered_count INTEGER NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE conversation_display_summaries (
            chat_id TEXT PRIMARY KEY, summary_text TEXT NOT NULL,
            boundary_msg_id TEXT NOT NULL, covered_count INTEGER NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    con.execute(
        "INSERT INTO conversation_display_summaries VALUES "
        "('L1','legacy summary','m1',3,1,'2026-09-18T10:00:00','2026-09-18T10:00:00')"
    )
    con.execute(
        "INSERT INTO messages (chat_id, msg_id, content, timestamp, role) "
        "VALUES ('L1','m1','hi','2026-09-18T09:00:00','user')"
    )
    con.commit()
    con.close()


def _assert_migrated(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cur.execute("PRAGMA table_info(conversation_display_summaries)")
    cols = {r[1] for r in cur.fetchall()}
    assert "boundary_ts" in cols, f"boundary_ts 未补齐: {cols}"
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_cds_boundary'"
    )
    assert cur.fetchone() is not None, "idx_cds_boundary 未创建"
    # 回填：取该会话最新消息时间（而非 updated_at）
    cur.execute("SELECT boundary_ts FROM conversation_display_summaries WHERE chat_id='L1'")
    assert cur.fetchone()[0] == "2026-09-18T09:00:00"


def test_init_conv_schema_migrates_legacy_db(tmp_path):
    """旧库走 init_conv_schema（会话库路径）：必须补列+建索引+回填，且不抛异常。"""
    from src.memory.schema import init_conv_schema

    db = str(tmp_path / "legacy_conv.db")
    _make_legacy_db(db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        init_conv_schema(con, db)  # 修复前此处抛 no such column: boundary_ts
        _assert_migrated(con)
    finally:
        con.close()


def test_init_schema_migrates_legacy_db(tmp_path):
    """旧库走 init_schema（主库路径）：同样必须幂等迁移成功。"""
    from src.memory.schema import init_schema

    db = str(tmp_path / "legacy_main.db")
    _make_legacy_db(db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        init_schema(con, db)  # 修复前此处抛 no such column: boundary_ts
        _assert_migrated(con)
        # 幂等：再跑一次不应报错
        init_schema(con, db)
    finally:
        con.close()


# ── 游标泄漏回归（P0：曾导致展示摘要 100% 写回失败 database is locked）──────
# 根因：调度器 worker 线程的连接长期复用；未关闭的游标会残留 WAL 读快照，此后该
# 连接写库稳定报 "database is locked"（SQLITE_BUSY_SNAPSHOT，busy_timeout 与
# rollback()/commit() 均无效——见 tests 下方实验结论）。故断言：摘要链路的读写
# 方法必须关闭自己创建的游标，一个都不能漏。

class _SpyCursor:
    """游标代理：close() 时从 registry 移除，用于探测「未关闭游标」。"""

    def __init__(self, real, registry):
        self._real = real
        self._registry = registry
        registry.append(self)

    def close(self):
        if self in self._registry:
            self._registry.remove(self)
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _SpyConn:
    """连接代理：暴露 pending（仍未关闭的游标）。"""

    def __init__(self, real):
        self._real = real
        self.pending: list = []

    def cursor(self):
        return _SpyCursor(self._real.cursor(), self.pending)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_display_summary_read_write_closes_cursors(tmp_path):
    """get_display_summary / upsert_display_summary 必须关闭游标（否则连接被污染）。"""
    store = _make_store(tmp_path)
    repo = store._conversation_repo
    spy = _SpyConn(repo._cc(""))  # 包真实解析出的连接，避免指向别的库
    repo._cc = lambda platform="": spy  # type: ignore[assignment]

    assert repo.get_display_summary("NOPE") is None
    assert spy.pending == [], f"get_display_summary 泄漏游标: {len(spy.pending)} 个未关闭"

    repo.upsert_display_summary("C1", "摘要A", "m1", 1)
    assert spy.pending == [], f"upsert_display_summary 泄漏游标: {len(spy.pending)} 个未关闭"

    repo.get_display_summary("C1")
    repo.list_recent_display_summaries(limit=10)
    assert spy.pending == [], f"读路径泄漏游标: {len(spy.pending)} 个未关闭"


def test_conv_schema_init_runs_once_per_path(tmp_path, monkeypatch):
    """会话库 schema 初始化必须「每个物理文件、每进程仅一次」。

    否则每次新建连接都要跑 DDL + boundary_ts 回填 UPDATE（各取一次写锁），
    与轮询器并发抓取的写入互相挤锁 → poller upsert_conversation 报 database is locked。
    """
    import src.memory.sqlite_store_conn as ssc

    calls = {"n": 0}
    real_init = ssc.init_conv_schema

    def counting_init(conn, path):
        calls["n"] += 1
        return real_init(conn, path)

    monkeypatch.setattr(ssc, "init_conv_schema", counting_init)
    # 从干净状态开始（类级去重集合是进程级的）
    SQLiteStore._conv_schema_initialized_paths = set()  # type: ignore[attr-defined]

    store = SQLiteStore(db_path=str(tmp_path / "linkora.db"))
    conv_path = store.conv_db_path("dingtalk")
    SQLiteStore._conv_schema_initialized_paths.discard(conv_path)  # type: ignore[attr-defined]

    store.conv_conn("dingtalk")
    assert calls["n"] == 1, "首个连接应完成一次会话库初始化"

    # 另一个线程 → 新连接（同物理文件）：不应再次初始化
    def _again():
        store.conv_conn("dingtalk")

    t = threading.Thread(target=_again)
    t.start()
    t.join()
    assert calls["n"] == 1, f"同一物理文件被重复初始化 {calls['n']} 次（写锁churn）"


def test_conv_conn_migrates_legacy_db(tmp_path):
    """旧会话库经 conv_conn 打开时（生产真实路径）必须补齐 boundary_ts 且不报错。"""
    store = SQLiteStore(db_path=str(tmp_path / "linkora.db"))
    conv_path = store.conv_db_path("dingtalk")
    os.makedirs(os.path.dirname(conv_path), exist_ok=True)
    _make_legacy_db(conv_path)  # 旧版表：无 boundary_ts
    SQLiteStore._conv_schema_initialized_paths = set()  # type: ignore[attr-defined]

    con = store.conv_conn("dingtalk")  # 修复前此处抛 no such column: boundary_ts
    _assert_migrated(con)


def test_recent_messages_closes_cursor(tmp_path):
    """get_recent_unarchived_messages（调度器取材入口）必须关闭游标。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "Z", "chatZ", 3, None, _iso(now))
    for i in range(3):
        _insert_message(cur, "Z", f"z{i}", f"m{i}", _iso(now - timedelta(minutes=5) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    spy = _SpyConn(store._message_repo._cc())  # 包真实解析出的连接
    store._message_repo._cc = lambda: spy  # type: ignore[assignment]
    msgs = store._message_repo.get_recent_unarchived_messages("Z", limit=40)
    assert len(msgs) == 3
    assert spy.pending == [], f"get_recent_unarchived_messages 泄漏游标: {len(spy.pending)} 个未关闭"


def test_scheduler_self_heals_on_locked_write(tmp_path, monkeypatch):
    """写回遇 database is locked 时：丢弃被污染连接并重试，第二次成功。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "H", "chatH", 5, None, _iso(now))
    for i in range(5):
        _insert_message(cur, "H", f"h{i}", f"m{i}", _iso(now - timedelta(minutes=10) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    repo = store._conversation_repo
    real_upsert = repo.upsert_display_summary
    calls = {"n": 0}

    def flaky_upsert(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_upsert(*a, **kw)

    monkeypatch.setattr(repo, "upsert_display_summary", flaky_upsert)
    discarded = {"n": 0}
    monkeypatch.setattr(
        store, "discard_conv_conn",
        lambda platform="": discarded.__setitem__("n", discarded["n"] + 1),
    )

    agent = _FakeAgent()
    sched = DisplaySummaryScheduler(agent=agent, store=store, platform="", display_limit=40)
    sched._process_job_inner(DisplaySummaryJob(chat_id="H"))

    assert calls["n"] == 2, "应在锁失败后重试一次"
    assert discarded["n"] == 1, "应丢弃被污染的连接以自愈"
    assert repo.get_display_summary("H") is not None, "重试后摘要应成功落库"
