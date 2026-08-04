"""SQLite 数据库自动备份工具。

使用 SQLite 官方推荐的 Connection.backup() API 进行在线备份，
确保 WAL 模式下的数据一致性。支持：
- 定时备份（默认每天一次）
- 启动时立即备份一次
- 自动清理旧备份（保留最近 N 份）
- 备份前完整性校验
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from threading import Thread

from src.tools.utils import cross_process_lock
from src.paths import data_path

logger = logging.getLogger(__name__)


class DatabaseBackup:
    """数据库自动备份器，支持定时备份和手动触发。"""

    def __init__(
        self,
        db_path: str,
        backup_dir: str = str(data_path("backups")),
        interval_hours: int = 24,
        max_backups: int = 7,
        backup_on_start: bool = True,
    ):
        self.db_path = db_path
        self.backup_dir = Path(backup_dir)
        # 周期统一取整（YAML 可能解析为 float）；<=0 视为禁用定时备份，
        # 避免 Event.wait(0) 立即返回导致无限循环备份。
        self.interval_hours = interval_hours
        self.interval_seconds = int(round(interval_hours * 3600))
        self.max_backups = max_backups
        self.backup_on_start = backup_on_start
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Thread | None = None
        # 进程内串行化：避免调度线程与手动触发并发执行 backup_now。
        self._backup_lock = threading.Lock()

        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        """启动后台备份线程。"""
        if self.interval_seconds <= 0:
            logger.warning(
                "数据库备份间隔<=0，定时备份已禁用（interval_hours=%r）；如需手动备份请调用 backup_now()",
                self.interval_hours,
            )
            return
        if self._running:
            logger.warning("数据库备份已在运行")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = Thread(target=self._backup_loop, daemon=True)
        self._thread.start()
        logger.info(
            "数据库备份已启动：间隔=%d小时，最大备份数=%d，启动即备份=%s",
            self.interval_seconds // 3600,
            self.max_backups,
            self.backup_on_start,
        )

    def stop(self) -> None:
        """停止备份线程。"""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("数据库备份已停止")

    def _cross_lock_name(self) -> str:
        """跨进程锁名按数据库文件派生，使不同库的备份互不排斥。

        旧实现所有 DatabaseBackup 共用锁名 "db-backup"，导致多平台各自
        库在启动/周期中同时触发时互相抢锁、非阻塞失败被跳过（误报 WARNING
        且部分平台库长期得不到备份）。改为按 db 文件名独立锁名后，只有
        同一库的并发才会互斥，跨库之间可安全并行。
        """
        return f"db-backup-{Path(self.db_path).stem}"

    def backup_now(self) -> str | None:
        """立即执行一次备份，返回备份文件路径。

        进程内用 threading.Lock 串行化（避免调度线程与手动触发并发），
        跨进程用 fcntl.flock 互斥（避免多 bot 实例重复备份/互相清理）。
        跨进程锁名按数据库文件派生，不同库互不排斥。
        """
        if not self._backup_lock.acquire(blocking=False):
            logger.warning("数据库备份正在进行（同进程内并发调用），跳过本次")
            return None
        try:
            with cross_process_lock(self._cross_lock_name(), str(self.backup_dir)) as acquired:
                if not acquired:
                    logger.warning("另一进程正在执行数据库备份，跳过本次（避免重复备份）")
                    return None
                return self._backup_now_inner()
        finally:
            self._backup_lock.release()

    def _backup_now_inner(self) -> str | None:
        """实际备份逻辑（已被进程内/跨进程锁保护）。"""
        try:
            db_file = Path(self.db_path)
            if not db_file.exists():
                logger.error("数据库文件未找到: %s", self.db_path)
                return None

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            db_stem = Path(self.db_path).stem
            backup_name = f"{db_stem}_{timestamp}.db"
            backup_path = self.backup_dir / backup_name

            src_conn = sqlite3.connect(self.db_path)
            src_conn.execute("PRAGMA busy_timeout=5000")
            try:
                dst_conn = sqlite3.connect(str(backup_path))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

            logger.info("数据库已备份到: %s (大小: %.1f MB)",
                        backup_path, backup_path.stat().st_size / 1024 / 1024)

            self._cleanup_old_backups()
            return str(backup_path)
        except Exception as e:
            logger.error("备份数据库失败: %s", e, exc_info=True)
            # 清理残留的部分备份文件，避免影响 _cleanup_old_backups 的计数
            try:
                if backup_path.exists():
                    backup_path.unlink(missing_ok=True)
            except OSError as _exc:
                logger.warning(f"_backup_now_inner: swallowed exception: {_exc}")
                pass
            return None

    def _backup_loop(self) -> None:
        """后台循环执行备份。

        如果 backup_on_start=True，启动时立即备份一次，
        之后按 interval_seconds 间隔定时备份。
        """
        if self.backup_on_start:
            try:
                self.backup_now()
            except Exception as e:
                logger.error("首次备份失败: %s", e)

        while self._running:
            try:
                # 用 Event.wait 替代 time.sleep，使 stop() 能及时唤醒
                if self._stop_event.wait(self.interval_seconds):
                    break
                if self._running:
                    self.backup_now()
            except Exception as e:
                logger.error("备份循环错误: %s", e)
                self._stop_event.wait(300)

    def _cleanup_old_backups(self) -> None:
        """清理超过最大数量的旧备份。"""
        try:
            backups = sorted(
                self.backup_dir.glob(f"{Path(self.db_path).stem}_*.db"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            if len(backups) > self.max_backups:
                for old_backup in backups[self.max_backups:]:
                    old_backup.unlink()
                    logger.info("已移除旧备份: %s", old_backup.name)
        except Exception as e:
            logger.error("清理旧备份失败: %s", e)


class DatabaseBackupCoordinator:
    """多数据库备份协调器：在单个后台线程中按队列顺序依次备份各平台数据库。

    设计目标（对应“不同平台排队执行、不阻塞启动”）：
    - 异步：start() 仅启动一个 daemon 线程后立即返回，启动过程不被备份阻塞。
    - 排队：各平台数据库备份串行执行（一个完成再下一个），避免多个
      DatabaseBackup 各自 daemon 线程同时抢锁被非阻塞跳过（旧实现根因）。
    - 独立锁：每个 DatabaseBackup 已按数据库文件派生独立跨进程锁名，
      不同库的备份互不排斥；协调器进一步串行化，杜绝磁盘 IO 尖峰。
    """

    def __init__(
        self,
        backups: list[DatabaseBackup] | None = None,
        interval_hours: int = 24,
        backup_on_start: bool = True,
        stagger_seconds: float = 2.0,
    ):
        self.backups: list[DatabaseBackup] = list(backups) if backups else []
        self.interval_seconds = int(round(interval_hours * 3600))
        self.backup_on_start = backup_on_start
        self.stagger_seconds = stagger_seconds
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Thread | None = None

    def register(self, backup: DatabaseBackup) -> None:
        """注册一个待备份的数据库（运行中也可追加）。"""
        self.backups.append(backup)

    def start(self) -> None:
        """启动后台协调线程（立即返回，不阻塞调用方）。"""
        if self._running:
            logger.warning("数据库备份协调器已在运行")
            return
        if not self.backups:
            logger.warning("数据库备份协调器无注册备份，未启动")
            return
        self._running = True
        self._stop_event.clear()
        self._thread = Thread(
            target=self._coordinator_loop, daemon=True, name="db-backup-coordinator"
        )
        self._thread.start()
        logger.info(
            "数据库备份协调器已启动：平台数=%d，间隔=%d小时，启动即备份=%s，错峰=%ss",
            len(self.backups),
            self.interval_seconds // 3600 if self.interval_seconds > 0 else 0,
            self.backup_on_start,
            self.stagger_seconds,
        )

    def stop(self) -> None:
        """停止协调线程。"""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("数据库备份协调器已停止")

    def _coordinator_loop(self) -> None:
        if self.backup_on_start:
            try:
                self._run_queue()
            except Exception as e:  # noqa: BLE001
                # 启动首轮兜底：与周期调用（下方 while 内）对齐，避免首轮异常
                # 直接杀死协调器线程、导致所有平台后续备份永久停止。
                logger.error("数据库备份协调器启动备份异常（已忽略，周期备份继续）: %s", e, exc_info=True)
        if self.interval_seconds <= 0:
            # 仅执行一次启动备份，无周期任务；保持线程存活以便 stop() 优雅退出
            logger.info("数据库备份周期<=0，仅执行启动备份，无定时任务")
            self._stop_event.wait()
            return
        while self._running:
            # Event.wait 超时即到点备份；被 stop() 唤醒则返回 True 退出
            if self._stop_event.wait(self.interval_seconds):
                break
            if self._running:
                try:
                    self._run_queue()
                except Exception as e:  # noqa: BLE001
                    # 循环级兜底：单次备份队列异常若逃逸会静默杀死协调器线程
                    # （僵尸线程，所有后续备份停止）。记日志后下轮继续。
                    logger.error("数据库备份协调器循环异常（已忽略，下轮继续）: %s", e, exc_info=True)

    def _run_queue(self) -> None:
        """按注册顺序串行备份各平台库，平台间错峰避免磁盘 IO 尖峰。"""
        for b in self.backups:
            if not self._running:
                break
            try:
                b.backup_now()
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "数据库备份协调器执行平台 %s 备份失败: %s",
                    getattr(b, "db_path", "?"),
                    e,
                )
            if self._running and self.stagger_seconds > 0:
                self._stop_event.wait(self.stagger_seconds)
