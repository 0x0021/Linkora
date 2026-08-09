"""清理操作性能基准测试。

验证 P0-1 修复（SQLite 并发锁）对性能的影响。
"""
from __future__ import annotations

import time


def test_cleanup_performance_baseline(tmp_path):
    """测试清理操作的基线性能（无竞争）。"""
    from src.memory.sqlite_store import SQLiteStore

    db_path = tmp_path / "test_perf.db"
    store = SQLiteStore(db_path=str(db_path))
    store.init_db()

    # 基准测试清理性能（空数据）
    start = time.perf_counter()
    store._memory_repo.cleanup_old_memories(max_age_days=90)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"清理操作耗时过长: {elapsed:.3f}s"
    print(f"清理性能: {elapsed:.4f}s")


def test_concurrent_cleanup_safety(tmp_path):
    """测试并发清理的安全性。"""
    from src.memory.sqlite_store import SQLiteStore
    import threading

    db_path = tmp_path / "test_concurrent.db"
    store = SQLiteStore(db_path=str(db_path))
    store.init_db()

    errors = []

    def cleanup_worker(worker_id: int):
        try:
            for _ in range(5):
                store._memory_repo.cleanup_old_memories(max_age_days=0)
                store._message_repo.cleanup_old_messages(retention_days=0)
        except Exception as e:
            errors.append((worker_id, str(e)))

    threads = [threading.Thread(target=cleanup_worker, args=(i,)) for i in range(4)]

    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start

    assert not errors, f"并发清理失败: {errors}"
    print(f"并发清理 (4线程x5轮): {elapsed:.3f}s, 无错误")
