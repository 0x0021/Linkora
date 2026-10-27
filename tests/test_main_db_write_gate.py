"""回归测试：主库高频写路径必须经由 write_main_with_retry 收口（P0-1）。

确保 upsert_summary / cleanup_old_memories / _supersede_similar_memories 等主库业务写
都经过进程内写闸门（write_main_with_retry），而非裸 ``self.store.conn.commit()``，
避免与已收口方法（add_pending_fact 等）不一致、缺失遇锁（database is locked）自愈重试。

不依赖真实 SQLiteStore / torch / faiss：用轻量 FakeStore 记录 write_main_with_retry
调用次数即可断言「写路径已收口」。
"""
from __future__ import annotations

import json

from src.memory.memory_repo import MemoryRepo


class _FakeCursor:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.rowcount = 0
        self.lastrowid = 1

    def execute(self, sql, params=None):
        return self

    def executemany(self, sql, seq):
        self.rowcount = len(seq) if seq else 0
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass

    def execute(self, sql, params=None):
        return self


class _GateRecordingStore:
    """记录 write_main_with_retry 调用次数，并把 _do 跑在假 conn 上。"""

    def __init__(self):
        self.gate_calls = 0
        self.conn = _FakeConn()

    def write_main_with_retry(self, fn, *, max_attempts=3):
        self.gate_calls += 1
        return fn(self.conn)


def test_upsert_summary_goes_through_gate():
    store = _GateRecordingStore()
    repo = MemoryRepo(store)
    repo.upsert_summary(scope="personal", group_key="g1", content="c")
    assert store.gate_calls == 1


def test_cleanup_old_memories_goes_through_gate():
    store = _GateRecordingStore()
    repo = MemoryRepo(store)
    assert repo.cleanup_old_memories(max_age_days=90) == 0
    assert store.gate_calls == 1


def test_supersede_goes_through_gate_when_matched():
    store = _GateRecordingStore()
    repo = MemoryRepo(store)
    # 让查询返回一条与传入 embedding 完全相同（cosine=1）的记忆，触发 superseded
    emb = [0.1, 0.2, 0.3]
    rows = [{"id": 7, "embedding": json.dumps(emb)}]
    store.conn.cursor = lambda: _FakeCursor(rows)
    n = repo._supersede_similar_memories(
        new_id=8, embedding=emb, scope="personal", sender_id="u1"
    )
    assert n == 1
    assert store.gate_calls == 1
