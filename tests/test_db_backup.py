"""测试 db_backup.py — 数据库自动备份器"""
import time

import pytest
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.db_backup import DatabaseBackup, DatabaseBackupCoordinator


# ============================================================================
# __init__
# ============================================================================
class TestInit:
    def test_default_parameters(self, tmp_path):
        db = str(tmp_path / "test.db")
        backup = DatabaseBackup(db_path=db)
        assert backup.db_path == db
        assert backup.backup_dir.name == "backups"
        assert backup.interval_seconds == 24 * 3600
        assert backup.max_backups == 7
        assert backup.backup_on_start is True
        assert backup._running is False
        assert backup._thread is None

    def test_custom_parameters(self, tmp_path):
        backup_dir = tmp_path / "my_backups"
        backup = DatabaseBackup(
            db_path=str(tmp_path / "db.sqlite"),
            backup_dir=str(backup_dir),
            interval_hours=6,
            max_backups=3,
            backup_on_start=False,
        )
        assert backup.interval_seconds == 6 * 3600
        assert backup.max_backups == 3
        assert backup.backup_on_start is False
        assert backup.backup_dir == backup_dir
        assert backup_dir.exists()

    def test_backup_dir_created(self, tmp_path):
        backup_dir = tmp_path / "nested" / "backups"
        DatabaseBackup(
            db_path=str(tmp_path / "db.sqlite"),
            backup_dir=str(backup_dir),
        )
        assert backup_dir.exists()


# ============================================================================
# start / stop
# ============================================================================
class TestStartStop:
    @pytest.fixture
    def backup(self, tmp_path):
        return DatabaseBackup(
            db_path=str(tmp_path / "test.db"),
            backup_dir=str(tmp_path / "backups"),
            backup_on_start=False,
        )

    def test_start_creates_thread(self, backup):
        backup.start()
        assert backup._running is True
        assert backup._thread is not None
        assert backup._thread.daemon is True
        backup.stop()

    def test_double_start_ignored(self, backup):
        backup.start()
        thread1 = backup._thread
        backup.start()  # second start should be no-op
        assert backup._thread is thread1
        backup.stop()

    def test_stop_sets_running_false(self, backup):
        backup.start()
        backup.stop()
        assert backup._running is False

    def test_stop_when_not_running(self, backup):
        backup.stop()  # should not raise
        assert backup._running is False
        assert backup._thread is None

    def test_start_stop_lifecycle(self, backup):
        for _ in range(3):
            backup.start()
            assert backup._running is True
            backup.stop()
            assert backup._running is False
            assert backup._thread is None


# ============================================================================
# backup_now
# ============================================================================
class TestBackupNow:
    @pytest.fixture
    def backup(self, tmp_path):
        db_path = tmp_path / "source.db"
        db_path.write_text("")
        backup_dir = tmp_path / "backups"
        return DatabaseBackup(
            db_path=str(db_path),
            backup_dir=str(backup_dir),
            max_backups=2,
            backup_on_start=False,
        )

    def test_file_not_exists(self, backup, tmp_path):
        backup.db_path = str(tmp_path / "nonexistent.db")
        result = backup.backup_now()
        assert result is None

    @patch("src.db_backup.sqlite3")
    @patch("pathlib.Path.stat")
    def test_successful_backup(self, mock_stat, mock_sqlite3, backup):
        mock_stat.return_value = MagicMock(st_size=2048)
        mock_src = MagicMock()
        mock_dst = MagicMock()
        mock_sqlite3.connect.side_effect = [mock_src, mock_dst]

        result = backup.backup_now()

        assert result is not None
        assert "source_" in result
        assert result.endswith(".db")
        mock_src.backup.assert_called_once_with(mock_dst)
        mock_src.close.assert_called_once()
        mock_dst.close.assert_called_once()

    @patch("src.db_backup.sqlite3")
    def test_closes_connections_on_error(self, mock_sqlite3, backup):
        mock_src = MagicMock()
        mock_src.backup.side_effect = sqlite3.OperationalError("disk full")
        mock_dst = MagicMock()
        mock_sqlite3.connect.side_effect = [mock_src, mock_dst]

        result = backup.backup_now()

        assert result is None
        mock_src.close.assert_called_once()
        mock_dst.close.assert_called_once()

    @patch("src.db_backup.sqlite3")
    def test_closes_src_on_dst_error(self, mock_sqlite3, backup):
        mock_src = MagicMock()
        mock_sqlite3.connect.side_effect = [mock_src, sqlite3.OperationalError]
        result = backup.backup_now()
        assert result is None
        mock_src.close.assert_called_once()

    @patch("src.db_backup.sqlite3")
    @patch("pathlib.Path.stat")
    def test_cleanup_called_after_backup(self, mock_stat, mock_sqlite3, backup):
        mock_stat.return_value = MagicMock(st_size=1024)
        mock_src = MagicMock()
        mock_dst = MagicMock()
        mock_sqlite3.connect.side_effect = [mock_src, mock_dst]

        with patch.object(backup, "_cleanup_old_backups") as mock_cleanup:
            backup.backup_now()
            mock_cleanup.assert_called_once()


# ============================================================================
# _cleanup_old_backups
# ============================================================================
class TestCleanupOldBackups:
    @pytest.fixture
    def backup(self, tmp_path):
        db_path = tmp_path / "source.db"
        db_path.write_text("")
        backup_dir = tmp_path / "backups"
        return DatabaseBackup(
            db_path=str(db_path),
            backup_dir=str(backup_dir),
            max_backups=3,
            backup_on_start=False,
        )

    def test_no_backups_to_clean(self, backup):
        backup._cleanup_old_backups()  # should not raise

    def test_under_limit_no_cleanup(self, backup, tmp_path):
        files = []
        for i in range(3):
            f = backup.backup_dir / f"source_20260711_00000{i}.db"
            f.write_text(f"backup{i}")
            files.append(f)
        backup._cleanup_old_backups()
        for f in files:
            assert f.exists()

    def test_over_limit_removes_oldest(self, backup, tmp_path):
        # Create 5 backup files with staggered mtimes (0=oldest, 4=newest)
        files = []
        for i in range(5):
            f = backup.backup_dir / f"source_20260711_00000{i}.db"
            f.write_text(f"backup{i}")
            files.append(f)

        # Set real mtimes via os.utime: file[0]=1000 → file[4]=5000
        base_time = 1000.0
        import os as _os
        for i, f in enumerate(files):
            _os.utime(str(f), (base_time + i * 1000, base_time + i * 1000))

        backup._cleanup_old_backups()

        # Keep 3 newest: files[2], files[3], files[4] → files[0] and files[1] removed
        assert not files[0].exists()
        assert not files[1].exists()
        assert files[2].exists()
        assert files[3].exists()
        assert files[4].exists()

    def test_cleanup_handles_errors_gracefully(self, backup):
        with patch.object(Path, "glob", side_effect=OSError("permission denied")):
            backup._cleanup_old_backups()  # should not raise


# ============================================================================
# _backup_loop
# ============================================================================
class TestBackupLoop:
    """_backup_loop 用 threading.Event.wait 替代 time.sleep（stop() 可及时唤醒）。

    测试用极小 interval_seconds + 非阻塞 _stop_event(mock，wait 立即返回 False)
    快速驱动循环，避免真实等待默认的 24h 间隔。
    """

    @pytest.fixture
    def backup(self, tmp_path):
        db_path = tmp_path / "source.db"
        db_path.write_text("")
        b = DatabaseBackup(
            db_path=str(db_path),
            backup_dir=str(tmp_path / "backups"),
            backup_on_start=False,
        )
        b.interval_seconds = 0.01  # 极小间隔，配合非阻塞 wait 快速循环
        b._stop_event = MagicMock()  # wait() 立即返回 False，不阻塞
        b._stop_event.wait.return_value = False
        return b

    def test_backup_on_start(self, backup):
        backup.backup_on_start = True
        backup._running = True
        call_count = [0]
        original = backup.backup_now

        def once_then_stop():
            call_count[0] += 1
            backup._running = False
            return original()

        backup.backup_now = once_then_stop
        backup._backup_loop()
        # backup_on_start=True 时启动即备份一次
        assert call_count[0] >= 1

    def test_no_backup_on_start_when_disabled(self, backup):
        backup.backup_on_start = False
        backup._running = True
        call_count = [0]
        original = backup.backup_now

        def once_then_stop():
            call_count[0] += 1
            backup._running = False
            return original()

        backup.backup_now = once_then_stop
        backup._backup_loop()
        # backup_on_start=False：启动不备份；循环内恰好触发一次后退出（非启动双调）
        assert call_count[0] == 1

    def test_backup_on_start_failure_handled(self, backup):
        """首次备份（backup_on_start）失败时记录错误但不崩溃，循环继续。"""
        backup.backup_on_start = True
        backup._running = True
        call_count = [0]

        def fail_first_then_ok():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("first backup failed")
            backup._running = False
            return "/fake/backup.db"

        backup.backup_now = fail_first_then_ok
        backup._backup_loop()
        assert call_count[0] >= 2  # 第一次失败(backup_on_start)，第二次成功(循环)

    def test_loop_exits_when_stopped(self, backup):
        backup.backup_on_start = False
        backup._running = True
        original_backup_now = backup.backup_now

        def stop_after_first():
            backup._running = False
            return original_backup_now()

        backup.backup_now = stop_after_first
        backup._backup_loop()
        assert backup._running is False

    def test_loop_handles_backup_error(self, backup):
        backup.backup_on_start = False
        backup._running = True
        call_count = [0]

        def fail_then_stop():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("backup failed")
            backup._running = False
            return "/fake/backup.db"

        backup.backup_now = fail_then_stop
        backup._backup_loop()
        # 首次备份抛错被循环捕获，第二次成功并退出
        assert call_count[0] >= 2


# ============================================================================
# 周期校验（0/负数禁用 + 浮点取整）—— 防止 Event.wait(0) 无限备份 / float 崩溃
# ============================================================================
class TestIntervalValidation:
    def test_zero_interval_disables_scheduler(self, tmp_path):
        backup = DatabaseBackup(
            db_path=str(tmp_path / "test.db"),
            backup_dir=str(tmp_path / "backups"),
            interval_hours=0,
            backup_on_start=False,
        )
        assert backup.interval_seconds == 0
        backup.start()
        # 间隔<=0 不应启动后台线程（避免 Event.wait(0) 忙循环无限备份）
        assert backup._running is False
        assert backup._thread is None
        backup.stop()

    def test_negative_interval_disables_scheduler(self, tmp_path):
        backup = DatabaseBackup(
            db_path=str(tmp_path / "test.db"),
            backup_dir=str(tmp_path / "backups"),
            interval_hours=-1,
            backup_on_start=False,
        )
        # 负小时 -> int(round(-3600)) == -3600，start() 仍以 <=0 禁用定时备份
        assert backup.interval_seconds == -3600
        backup.start()
        assert backup._running is False
        backup.stop()

    def test_float_interval_is_rounded(self, tmp_path):
        backup = DatabaseBackup(
            db_path=str(tmp_path / "test.db"),
            backup_dir=str(tmp_path / "backups"),
            interval_hours=1.5,
            backup_on_start=False,
        )
        # 浮点配置应被取整为 int，避免 Event.wait() 接收 float 的隐患
        assert isinstance(backup.interval_seconds, int)
        assert backup.interval_seconds == 5400
        backup.start()
        assert backup._running is True
        backup.stop()


# ============================================================================
# 跨进程锁按数据库文件派生独立锁名（不同库不再互相排斥）
# ============================================================================
class TestCrossLockName:
    def test_lock_name_derived_from_db_path(self, tmp_path):
        b = DatabaseBackup(db_path=str(tmp_path / "linkora.db"), backup_on_start=False)
        assert b._cross_lock_name() == "db-backup-linkora"

    def test_different_dbs_get_different_lock_names(self, tmp_path):
        a = DatabaseBackup(db_path=str(tmp_path / "linkora.db"), backup_on_start=False)
        f = DatabaseBackup(db_path=str(tmp_path / "feishu.db"), backup_on_start=False)
        assert a._cross_lock_name() != f._cross_lock_name()


# ============================================================================
# 备份协调器：异步 + 不同平台排队串行 + 不阻塞启动
# ============================================================================
class _FakeBackup:
    def __init__(self, name: str):
        self.db_path = name
        self.calls = []

    def backup_now(self):
        self.calls.append(1)
        return f"/bak/{self.db_path}"


class TestBackupCoordinator:
    def test_start_is_nonblocking_and_thread_alive(self, tmp_path):
        coord = DatabaseBackupCoordinator(
            backups=[_FakeBackup("a")], interval_hours=24,
            backup_on_start=True, stagger_seconds=0,
        )
        coord.start()
        try:
            assert coord._thread is not None and coord._thread.is_alive()
            # 启动后立即返回，未被备份阻塞
            assert coord._running is True
            thread = coord._thread
        finally:
            coord.stop()
            assert not thread.is_alive()

    def test_runs_all_platform_backups_once(self, tmp_path):
        a, b = _FakeBackup("a"), _FakeBackup("b")
        coord = DatabaseBackupCoordinator(
            backups=[a, b], interval_hours=24,
            backup_on_start=True, stagger_seconds=0,
        )
        coord.start()
        deadline = time.time() + 2
        while (not a.calls or not b.calls) and time.time() < deadline:
            time.sleep(0.02)
        coord.stop()
        assert a.calls == [1], a.calls
        assert b.calls == [1], b.calls

    def test_preserves_registration_order(self, tmp_path):
        order = []
        class _OrderFB:
            def __init__(self, n): self.db_path = n
            def backup_now(self):
                order.append(self.db_path)
                return "x"
        backs = [_OrderFB("a"), _OrderFB("b"), _OrderFB("c")]
        coord = DatabaseBackupCoordinator(
            backups=backs, interval_hours=24, backup_on_start=True, stagger_seconds=0,
        )
        coord.start()
        deadline = time.time() + 2
        while len(order) < 3 and time.time() < deadline:
            time.sleep(0.02)
        coord.stop()
        assert order == ["a", "b", "c"]

    def test_no_startup_backup_when_disabled(self, tmp_path):
        a = _FakeBackup("a")
        coord = DatabaseBackupCoordinator(
            backups=[a], interval_hours=24, backup_on_start=False, stagger_seconds=0,
        )
        coord.start()
        time.sleep(0.2)
        coord.stop()
        assert a.calls == []

    def test_no_backups_does_not_start(self, tmp_path):
        coord = DatabaseBackupCoordinator(backups=[], interval_hours=24, backup_on_start=True)
        coord.start()
        assert coord._running is False
        assert coord._thread is None
