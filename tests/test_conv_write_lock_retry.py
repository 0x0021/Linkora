"""会话库锁失败自愈的回归测试（2026-10-05 线上事故）。

事故：展示摘要调度器发现 942 个待摘要会话后高频写库，轮询器 4 个 worker 的
``upsert_conversation`` 在 **3ms 内同时**报 "database is locked"，每 5 秒一轮、
持续 155 次，波及 poller / 消息清理 / 摘要调度器三条链路。

判别（实测复现，见 test_busy_snapshot_is_immediate 固化）：
  - 跨连接真锁争用 → 等满 busy_timeout（5.17s）才失败，busy_timeout 生效
  - SQLITE_BUSY_SNAPSHOT（陈旧读快照）→ **0.0000s 立即失败**，busy_timeout 无效，
    rollback/commit 都清不掉，唯一可靠恢复是「关掉连接重建」

事故根因：``discard_conn``（丢弃被污染连接）当时**只有** display_summary_scheduler
一处在用，轮询器等高频写点全都没有自愈，于是连接一旦被污染就永久失效。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.memory.sqlite_store import SQLiteStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(db_path=str(tmp_path / "linkora.db"))
    yield s
    try:
        s.close()
    except Exception:  # noqa: BLE001
        pass


class TestBusySnapshotIsImmediate:
    """先固化判别依据——否则无法证明线上是快照而非真锁。"""

    def test_busy_snapshot_fails_immediately(self, tmp_path):
        """陈旧读快照 → 立即失败（busy_timeout 不生效）。"""
        p = tmp_path / "snap.db"
        setup = sqlite3.connect(str(p), isolation_level=None)
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE t(a)")
        for i in range(30):
            setup.execute(f"INSERT INTO t VALUES ({i})")
        setup.close()

        c = sqlite3.connect(str(p), isolation_level=None)
        c.execute("PRAGMA busy_timeout=5000")
        c.execute("BEGIN")
        cur = c.cursor()
        cur.execute("SELECT * FROM t")
        cur.fetchone()      # 悬在结果集 → 读事务保持

        o = sqlite3.connect(str(p), isolation_level=None)
        o.execute("INSERT INTO t VALUES (999)")
        o.commit()
        o.close()

        t0 = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            c.execute("INSERT INTO t VALUES (1000)")
        dt = time.perf_counter() - t0
        assert dt < 0.5, f"应为立即失败，实际耗时 {dt:.2f}s（若 ≥5s 说明是真锁等待）"
        cur.close()
        c.rollback()
        c.close()

    def test_real_lock_waits_full_timeout(self, tmp_path):
        """跨连接真锁争用 → 等满 busy_timeout（对照组，证明判别依据有效）。"""
        p = tmp_path / "real.db"
        setup = sqlite3.connect(str(p), isolation_level=None)
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE t(a)")
        setup.execute("INSERT INTO t VALUES (1)")
        setup.close()

        holder = sqlite3.connect(str(p), isolation_level=None)
        holder.execute("PRAGMA busy_timeout=5000")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO t VALUES (2)")

        other = sqlite3.connect(str(p), isolation_level=None)
        other.execute("PRAGMA busy_timeout=5000")
        t0 = time.perf_counter()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("INSERT INTO t VALUES (3)")
        dt = time.perf_counter() - t0
        assert dt >= 1.0, f"真锁应等待 busy_timeout，实际仅 {dt:.2f}s"
        holder.rollback()
        holder.close()
        other.close()


class TestWriteWithRetry:
    def test_write_succeeds_normally(self, store):
        """正常路径：一次成功，不触发重试。"""
        repo = store._conversation_repo
        repo.upsert_conversation("C1", "会话1", "single")
        assert repo.get_conversation("C1") is not None

    def test_locked_write_recovers_by_discarding_connection(self, store, monkeypatch):
        """核心回归：写库抛 locked 时自动丢弃连接重试并最终成功。"""
        plat = "dingtalk"
        store.conv_conn(plat)  # 预热连接
        calls = {"n": 0}
        real = store.conv_conn

        def flaky(platform=""):
            """第一次返回的连接执行写会失败，之后恢复正常。"""
            return real(platform)

        def _boom(conn):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT OR REPLACE INTO conversations"
                    " (chat_id, chat_type, created_at, updated_at)"
                    " VALUES ('C2', 'single', datetime('now'), datetime('now'))"
                )
            finally:
                cur.close()
            conn.commit()

        monkeypatch.setattr(store, "conv_conn", flaky)
        store.write_with_retry(_boom, plat)
        assert calls["n"] == 2, f"应重试一次后成功，实际调用 {calls['n']} 次"

    def test_non_locked_error_raises_immediately(self, store):
        """非锁异常（如字段错误）必须直接抛出，不能被重试掩盖。"""
        def _bad(conn):
            raise sqlite3.OperationalError("no such column: x")

        with pytest.raises(sqlite3.OperationalError, match="no such column"):
            store.write_with_retry(_bad, "dingtalk")

    def test_locked_error_gives_up_after_max_attempts(self, store):
        """持续锁失败时按 max_attempts 放弃并抛出，不无限重试。"""
        calls = {"n": 0}

        def _always_locked(conn):
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            store.write_with_retry(_always_locked, "dingtalk", max_attempts=3)
        assert calls["n"] == 3


class TestConversationRepoWrites:
    def test_upsert_conversation_uses_retry_path(self, store, monkeypatch):
        """upsert_conversation 必须走自愈路径（事故点）。"""
        seen = {"retry": 0}
        real_retry = store.write_with_retry

        def spy(fn, platform="", **kw):
            seen["retry"] += 1
            return real_retry(fn, platform, **kw)

        monkeypatch.setattr(store, "write_with_retry", spy)
        store._conversation_repo.upsert_conversation("C3", "会话3", "single")
        assert seen["retry"] == 1, "upsert_conversation 未走 write_with_retry"

    def test_update_last_reply_time_uses_retry_path(self, store, monkeypatch):
        seen = {"retry": 0}
        real_retry = store.write_with_retry

        def spy(fn, platform="", **kw):
            seen["retry"] += 1
            return real_retry(fn, platform, **kw)

        monkeypatch.setattr(store, "write_with_retry", spy)
        store._conversation_repo.update_last_reply_time("C3", "single")
        assert seen["retry"] == 1, "update_last_reply_time 未走 write_with_retry"

    def test_update_last_replied_msg_id_uses_retry_path(self, store, monkeypatch):
        seen = {"retry": 0}
        real_retry = store.write_with_retry

        def spy(fn, platform="", **kw):
            seen["retry"] += 1
            return real_retry(fn, platform, **kw)

        monkeypatch.setattr(store, "write_with_retry", spy)
        store._conversation_repo.update_last_replied_msg_id("C3", "m1", "single")
        assert seen["retry"] == 1, "update_last_replied_msg_id 未走 write_with_retry"

    def test_behaviour_unchanged_after_retry(self, store):
        """自愈包装不得改变业务语义：upsert 的字段与冲突更新保持原样。"""
        repo = store._conversation_repo
        repo.upsert_conversation("C9", "旧名", "single")
        repo.upsert_conversation("C9", "新名", "group", last_message_time="2026-01-01T00:00:00")
        row = repo.get_conversation("C9")
        assert row["chat_name"] == "新名"
        assert row["chat_type"] == "group"
        assert row["last_message_time"] == "2026-01-01T00:00:00"

    def test_invalid_chat_id_still_rejected(self, store):
        """跨平台防护不受包装影响（ou_ 前缀仍拒绝）。"""
        repo = store._conversation_repo
        repo.upsert_conversation("ou_abc", "飞书用户", "single")
        assert repo.get_conversation("ou_abc") is None


class TestWriteGate:
    """进程内写闸门（P0 2026-10-05 事故的核心修复）。

    事故根因（git 溯源）：`e2997cb`(2026-09-18) 之前**从不报** database is locked，
    因为读游标从不关闭 → 悬着的 WAL 读事务**意外地把写入挡在门外**。该提交修好了
    游标泄漏（本身正确），等于**拆掉了这个天然闸门**，而写入侧限流/自愈未同步，
    跨进程争用立即爆发。铁证：`logs/linkora.log.1 5`（09-06，21747 行、已有展示摘要）
    锁错误 0 条。

    本组用例锁住「用显式串行化替代靠 bug 挡写入」这个闸门本身。
    """

    def test_gate_is_reentrant(self, store):
        """嵌套进入不死锁（write_with_retry 可能互相包装）。"""
        with store._write_gate():
            with store._write_gate():
                pass  # 内层正常退出

    def test_gate_released_after_exit(self, store):
        """退出后必须释放锁（否则第二次进入会等到超时）。"""
        with store._write_gate():
            pass
        with store._write_gate(timeout=1.0):
            pass  # 能立即进入即说明已释放

    def test_gate_released_on_exception(self, store):
        """异常时也必须释放锁，否则闸门永久锁死。"""
        with pytest.raises(ValueError, match="boom"):
            with store._write_gate():
                raise ValueError("boom")
        with store._write_gate(timeout=1.0):
            pass

    def test_gate_serializes_threads(self, store):
        """多线程进入闸门必须串行（SQLite 同时只允许一个写者）。"""
        import threading as _th

        order: list[tuple[str, int]] = []
        lock = _th.Lock()

        def writer(i: int) -> None:
            with store._write_gate():
                with lock:
                    order.append(("in", i))
                time.sleep(0.02)
                with lock:
                    order.append(("out", i))

        threads = [_th.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        depth = 0
        max_depth = 0
        for kind, _i in order:
            if kind == "in":
                depth += 1
                max_depth = max(max_depth, depth)
            else:
                depth -= 1
        assert depth == 0, "闸门未平衡退出"
        assert max_depth == 1, f"闸门未串行化，最大并发深度={max_depth}"

    def test_write_with_retry_uses_gate(self, store, monkeypatch):
        """write_with_retry 必须走闸门（否则本次修复形同虚设）。"""
        entered = {"n": 0, "inside": False, "exited": False}
        real_gate = type(store)._write_gate

        def spy_gate(timeout=60.0):
            cm = real_gate(timeout)
            entered["n"] += 1

            class _Wrapped:
                def __enter__(self_inner):
                    cm.__enter__()
                    return None

                def __exit__(self_inner, *a):
                    entered["exited"] = True
                    return cm.__exit__(*a)

            return _Wrapped()

        monkeypatch.setattr(type(store), "_write_gate", staticmethod(spy_gate))

        def _write(conn):
            entered["inside"] = True

        store.write_with_retry(_write, "dingtalk")
        assert entered["n"] >= 1, "write_with_retry 未经过写闸门"
        assert entered["inside"] and entered["exited"], "写入未在闸门内完成"
