"""SQLiteStore 孤儿 WAL/SHM 自动清理测试。

背景：Finder 复制/移动 .db 时自动命名（linkora 2.db），副本主文件被删/移后
-wal/-shm 残留（曾出现 31 个孤儿 4.7MB）。程序此前无自动清理，SQLite 也不会管
（-wal/-shm 只在主库存在时有意义）。init_db 首跑时清理「无对应 .db」的孤儿。
"""

from __future__ import annotations

import pytest

from src.memory.sqlite_store import SQLiteStore


class TestOrphanWalShmCleanup:
    def _make_store(self, tmp_path):
        return SQLiteStore(db_path=str(tmp_path / "main.db"))

    def test_cleans_orphan_wal_shm(self, tmp_path):
        """主目录下的孤儿 -wal/-shm（无 .db 主库）被清理。"""
        store = self._make_store(tmp_path)
        orphan_wal = tmp_path / "main 2.db-wal"
        orphan_shm = tmp_path / "main 2.db-shm"
        orphan_wal.write_bytes(b"x")
        orphan_shm.write_bytes(b"x")
        store.init_db()
        assert not orphan_wal.exists(), "孤儿 -wal 未清理"
        assert not orphan_shm.exists(), "孤儿 -shm 未清理"

    def test_keeps_active_db_untouched(self, tmp_path):
        """活动主库及其 -wal/-shm 不受影响。"""
        store = self._make_store(tmp_path)
        # 模拟活动库的 wal/shm（有主库 main.db → 不清理）
        active_wal = tmp_path / "main.db-wal"
        active_shm = tmp_path / "main.db-shm"
        active_wal.write_bytes(b"w")
        active_shm.write_bytes(b"s")
        store.init_db()
        assert active_wal.exists(), "活动库 -wal 被误删"
        assert active_shm.exists(), "活动库 -shm 被误删"
        assert (tmp_path / "main.db").exists(), "活动主库被误删"

    def test_cleans_conv_root_orphans(self, tmp_path):
        """会话库目录（conversations/）下的孤儿同样清理。"""
        store = self._make_store(tmp_path)
        conv = tmp_path / "conversations"
        conv.mkdir(exist_ok=True)
        orphan = conv / "dingtalk__abc123 2.db-wal"
        orphan.write_bytes(b"x")
        # 正常会话库（有主库）保留
        live = conv / "dingtalk__live.db"
        live.write_bytes(b"db")
        live_wal = conv / "dingtalk__live.db-wal"
        live_wal.write_bytes(b"w")
        store.init_db()
        assert not orphan.exists(), "conversations 孤儿未清理"
        assert live.exists() and live_wal.exists(), "conversations 活动库被误删"

    def test_cleanup_runs_once_per_path(self, tmp_path):
        """同 db_path 只清理一次（类属性去重），二次 init_db 不再扫。"""
        store = self._make_store(tmp_path)
        orphan = tmp_path / "main 2.db-wal"
        orphan.write_bytes(b"x")
        store.init_db()
        assert not orphan.exists()
        # 再次放孤儿并重复 init_db → 不再清理（去重生效，文件保留）
        orphan.write_bytes(b"y")
        store.init_db()
        assert orphan.exists(), "去重失效：二次 init_db 又清理了"
