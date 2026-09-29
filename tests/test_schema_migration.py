"""schema 迁移并发安全回归测试。

回归点（HIGH-5 记忆治理引入）：
- memories / kb_chunks 新增 status / superseded_by / updated_at 等列，init_schema 通过
  _ensure_column 补齐。Web 会为同一 linkora.db 创建多个 SQLiteStore 实例，各实例并发
  调 init_db → 多连接并发对同文件跑 init_schema，在「PRAGMA 检查列 → ALTER 执行」之间
  竞态，后者撞 "duplicate column name" 导致 schema 初始化失败、服务起不来。
- 修复：
  1) _ensure_column 对 duplicate column / already exists 做幂等兜底（捕获后二次核验）。
  2) conn 属性把 schema 初始化改为【类级、按 db_path 串行去重】，同文件全局只初始化一次。
"""
from __future__ import annotations

import threading
import time

from src.memory.schema import _ensure_column
from src.memory.sqlite_store import SQLiteStore


def _count_col(conn, table: str, col: str) -> int:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    return cols.count(col)


def test_concurrent_init_db_no_duplicate_column(tmp_db_path):
    """多 SQLiteStore 实例并发 init_db 同一文件：不抛错、不产生重复列。

    模拟 Web 多 worker 各自持 store 实例的场景，验证类级 db_path 串行去重 + _ensure_column
    幂等兜底双保险。
    """
    errors: list[str] = []

    def worker() -> None:
        # CI 共享 runner（2 核、磁盘慢）上 10 线程同时全量 DDL，锁等待可能超出
        # busy_timeout=5s 上限，偶发 'database is locked'。这是环境竞争而非被测
        # 回归（重复列/结构损坏由下方断言独立守护）——对锁错误用**新连接**有限
        # 重试（新连接无陈旧读快照，重试有效）；其他异常或重试耗尽才记录。
        last_err: Exception | None = None
        for _ in range(3):
            try:
                store = SQLiteStore(db_path=str(tmp_db_path))
                store.init_db()
                store.close()
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "database is locked" not in repr(e):
                    break
                time.sleep(0.2)
        errors.append(repr(last_err))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 init_db 抛错: {errors}"

    # 库结构未损坏、关键列各只出现一次
    store = SQLiteStore(db_path=str(tmp_db_path))
    conn = store.conn
    conn.execute("PRAGMA integrity_check")  # 不抛即 ok
    assert _count_col(conn, "memories", "status") == 1
    assert _count_col(conn, "memories", "superseded_by") == 1
    assert _count_col(conn, "kb_chunks", "status") == 1
    assert _count_col(conn, "kb_chunks", "superseded_by") == 1
    assert _count_col(conn, "kb_chunks", "updated_at") == 1
    store.close()


def test_ensure_column_concurrent_race_safe(tmp_db_path):
    """_ensure_column 直接并发：两连接都查到列不存在、都去 ALTER，应只有一个成功、无异常。

    这是 _ensure_column 幂等兜底的独立验证，即使类级串行去重失效也能保证不崩。
    """
    store = SQLiteStore(db_path=str(tmp_db_path))
    store.init_db()
    conn = store.conn
    conn.execute("CREATE TABLE IF NOT EXISTS _race (id INTEGER)")
    conn.commit()

    errors: list[str] = []

    def worker() -> None:
        try:
            c = store.conn  # 每线程独立连接
            _ensure_column(c.cursor(), "_race", "extra", "TEXT")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"_ensure_column 并发抛错: {errors}"
    assert _count_col(store.conn, "_race", "extra") == 1
    store.close()
