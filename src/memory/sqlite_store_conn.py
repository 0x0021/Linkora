"""SQLiteStore 连接管理 mixin（主库 conn / 会话库 conv_conn / 迁移 / 完整性检查）。

拆分自 sqlite_store.py。
"""
from __future__ import annotations
from .sqlite_store_mixins_base import SQLiteStoreBase

import hashlib
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from src.memory import account_identity
from src.memory.schema import init_conv_schema, init_schema

logger = logging.getLogger(__name__)


class SQLiteStoreConnMixin(SQLiteStoreBase):
    @property
    def conn(self) -> sqlite3.Connection:
        """返回【当前线程】独立的 SQLite 连接（懒创建 + 缓存）。

        每个线程首次访问时新建连接并应用 WAL / busy_timeout 等 pragma，
        之后复用同一连接。连接对象不跨线程，彻底满足架构约束，
        消除此前单连接被 5+ 线程共享导致的 database is locked。
        对外 API（.conn 属性）不变，调用方无需改动。
        """
        tid = threading.get_ident()
        with self._conns_lock:
            if self._closed:
                raise RuntimeError("SQLiteStore is closed")
            existing = self._conns.get(tid)
            if existing is not None:
                return existing
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            # 并发写等待窗口：WAL 下仍可能短暂锁，设置 5s 避免直接抛 database is locked
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            # 页面缓存：默认 2MB 太小，提高到 ~64MB（-64000 页 × 1KB/页 = 64MB）
            # 对知识库检索 / 对话历史查询等读密集型场景显著减少磁盘 IO
            c.execute("PRAGMA cache_size=-64000")
            self._conns[tid] = c
            # 【HIGH-4】首次连接时主动执行 schema 迁移（CREATE TABLE + ALTER TABLE 补齐缺列），
            # 不依赖首次 SQL 触发时的隐式异常恢复。`init_db()` 内部幂等，重复调用安全。
            # 【CRITICAL】init_db() 必须在 _conns_lock 之外调用：此前在锁内调用 init_db，
            # 若 sqlite3.connect / PRAGMA integrity_check 因文件锁阻塞，Worker 线程将永远持有
            # _conns_lock，主线程后续访问 self.conn 时永久死锁。现改为锁内原子设置标志位、
            # 锁外执行 init_db，失败时回退标志位以便下一个线程重试。
            need_init = not self._schema_initialized
            if need_init:
                self._schema_initialized = True
            # 连接回收：per-thread 连接长期不关闭，若线程数动态增长（如 Web 框架
            # 每请求新线程）会缓慢泄漏 FD。超过上限时关闭最久未用的连接（dict
            # 保持插入顺序，next(iter) 即最早创建者），但跳过当前线程自身。
            if len(self._conns) > self._max_conns:
                alive = {t.ident for t in threading.enumerate() if t.ident is not None}
                # 仅回收已死线程的连接，避免关闭别的活跃线程正在使用的连接
                for otid, oconn in list(self._conns.items()):
                    if otid == tid:
                        continue
                    if otid not in alive:
                        try:
                            oconn.close()
                        except Exception as e:
                            logger.debug("关闭旧连接失败: %s", e)
                        self._conns.pop(otid, None)
                        break  # 每次只回收一个，避免一次性关闭过多
        # ── init_db 在 _conns_lock 之外执行，避免阻塞时锁死整个 store ──
        if need_init:
            try:
                self.init_db()
            except Exception as e:
                # 失败时回退标志位，下一个线程访问 conn 时会重新尝试 init_db
                self._schema_initialized = False
                logger.error("SQLiteStore schema 初始化失败 %s: %s", self.db_path, e)
                raise
        return c

    def _conv_db_path(self, platform: str, account_id: str) -> str:
        """计算某平台当前账号的会话 DB 文件路径。

        文件名对 account_id 取 sha256 前 16 位，避免把平台身份原文（可能含 corpId/
        appId）暴露到磁盘路径，同时保证同账号稳定、换账号必变。
        """
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
        os.makedirs(self._conv_root, exist_ok=True)
        return os.path.join(self._conv_root, f"{platform}__{digest}.db")

    def conv_conn(self, platform: str, fallback_corp_id: Optional[str] = None) -> sqlite3.Connection:
        """返回【当前线程 × 平台当前账号】独立的会话库连接（懒创建 + 缓存）。

        连接的物理文件由 ``resolve_account_id(platform)`` 决定，因此：
          - 同一账号 → 同一文件 → 数据互通；
          - 重登录换账号 → account_id 变 → db_path 变 → 自动打开新文件，旧账号数据天然隔离。

        会话相关的 6 张表（conversations / messages / conversation_summaries /
        external_friends / blocked_conversations / dedup_messages）都在此连接上，
        与主库（平台无关表）物理分离。首次为新账号创建会话库时，会把主库里既有
        的会话数据迁移过来（归属当前账号），避免历史会话丢失。

        若会话表查询调用方不知道 platform，应显式传入；缺省按平台解析，
        解析失败有稳定兜底键，绝不阻断启动。
        """
        platform = (platform or "").lower()
        if not platform:
            logger.warning("[账号隔离] conv_conn 收到空 platform，回退到空账号命名空间（可能查不到预期数据）")
        tid = threading.get_ident()
        account_id = account_identity.resolve_account_id(platform, fallback_corp_id)
        path = self._conv_db_path(platform, account_id)
        with self._conv_conns_lock:
            if self._closed:
                raise RuntimeError("SQLiteStore is closed")
            cached = self._conv_conns.get((tid, platform))
            if cached is not None and cached[0] == path:
                return cached[1]
            # 同线程同平台换账号导致物理路径变化：先关闭旧连接，避免 fd/WAL 句柄泄漏
            if cached is not None:
                try:
                    cached[1].close()
                except Exception as _close_err:  # noqa: BLE001
                    logger.debug("[账号隔离] 关闭旧会话连接失败: %s", _close_err)
            existed = os.path.exists(path)
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA cache_size=-64000")
            init_conv_schema(c, path)
            # 空/未知 platform 不触发迁移：避免盲拷主库全量数据进无前缀孤儿库
            need_migrate = bool(platform) and (not existed) and (path not in self._conv_migrated)
            if need_migrate:
                self._conv_migrated.add(path)
            self._conv_conns[(tid, platform)] = (path, c)
            # 连接回收（与主库同策略：仅回收已死线程的连接）
            if len(self._conv_conns) > self._max_conns:
                alive = {t.ident for t in threading.enumerate() if t.ident is not None}
                for (otid, oplat), (_op, oconn) in list(self._conv_conns.items()):
                    if otid == tid:
                        continue
                    if otid not in alive:
                        try:
                            oconn.close()
                        except Exception as e:
                            logger.debug("关闭旧会话连接失败: %s", e)
                        self._conv_conns.pop((otid, oplat), None)
                        break
        # 首次为新账号创建会话库 → 从主库迁移既有会话数据（归属当前账号）
        if need_migrate:
            try:
                self._migrate_main_to_conv(c, platform)
            except Exception as e:  # noqa: BLE001
                logger.warning("[账号隔离] 主库→会话库迁移失败（不影响新库使用）: %s", e)
        return c

    def _migrate_main_to_conv(self, conv: sqlite3.Connection, platform: str) -> None:
        """把主库既有会话数据拷贝进当前账号的会话库（一次性引导迁移）。

        仅拷贝当前平台可见的会话：按 chat_id 前缀归类（oc_=feishu、cid/DD=dingtalk，
        wecom 兜底全拷）。空/未知平台不迁移，避免把主库全量跨平台数据盲拷进一个无前缀
        的孤儿库。幂等：目标表用 INSERT OR IGNORE，重复账号引导不会造脏数据。
        """
        prefixes = self._MIGRATE_PLATFORM_PREFIXES.get(platform)
        if prefixes is None and platform not in self._MIGRATE_PLATFORM_PREFIXES:
            logger.warning(
                "[账号隔离] 平台 %r 未知/空，跳过主库→会话库迁移（不盲拷全量数据，防止产生孤儿库）",
                platform,
            )
            return
        main = self.conn
        tables = [
            ("dedup_messages", "msg_id, chat_id, processed_at"),
            ("conversations", "chat_id, chat_name, chat_type, peer_user_id, peer_open_dingtalk_id, "
                              "last_message_time, message_count, last_reply_time, last_replied_msg_id, "
                              "last_summary_at, created_at, updated_at"),
            ("messages", "chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, "
                         "timestamp, role, image_path, is_bot, is_archived, skip_reason, created_at"),
            ("conversation_summaries", "chat_id, summary_text, older_boundary_msg_id, covered_count, "
                                       "generation, created_at, updated_at"),
            ("external_friends", "name, open_dingtalk_id, chat_id, notes, created_at, updated_at"),
            ("blocked_conversations", "chat_id, chat_name, chat_type, reason, detected_at, source, "
                                      "last_error, cooldown_until, failure_count"),
        ]
        for table, cols in tables:
            try:
                if prefixes:
                    where = " OR ".join(["chat_id LIKE ?"] * len(prefixes))
                    params = [p + "%" for p in prefixes]
                    rows = main.execute(
                        f"SELECT {cols} FROM {table} WHERE {where}", params
                    ).fetchall()
                else:
                    rows = main.execute(f"SELECT {cols} FROM {table}").fetchall()
            except Exception as e:  # noqa: BLE001
                logger.debug("[账号隔离] 迁移表 %s 失败（主库可能无此表）: %s", table, e)
                continue
            if not rows:
                continue
            placeholders = ",".join(["?"] * len(cols.split(",")))
            col_list = cols.replace(" ", "")
            conv.executemany(
                f"INSERT OR IGNORE INTO {table} ({col_list}) VALUES ({placeholders})",
                [tuple(r) for r in rows],
            )
        conv.commit()
        logger.info("[账号隔离] 已从主库迁移 %s 平台会话数据到 %s", platform, platform)

    def _check_integrity_initial(self, cursor: sqlite3.Cursor | None = None) -> None:
        """执行 SQLite integrity_check（仅首次 init_db 时触发）。

        通过类属性 _checked_db_paths 记录已校验路径，同一路径只校验一次。
        校验失败时抛出 RuntimeError 阻止使用损坏的数据库。
        """
        # 用 type(self) 而非类名 SQLiteStore：mixin 拆分后类名不再位于本模块命名空间
        _cls = type(self)
        if not hasattr(_cls, "_checked_db_paths"):
            # 注：属性表达式上的类型注解会被 Python 静默忽略（不进 __annotations__），
            # 类型声明统一放 SQLiteStoreBase（ClassVar），此处只做赋值
            _cls._checked_db_paths = set()
        if self.db_path in _cls._checked_db_paths:
            return
        _cls._checked_db_paths.add(self.db_path)
        try:
            c = cursor or self.conn.cursor()
            row = c.execute("PRAGMA integrity_check").fetchone()
            result = row[0] if row else "unknown"
            if result == "ok":
                logger.debug("DB integrity_check 通过: %s", self.db_path)
            else:
                logger.error("DB integrity_check 失败: %s -> %s", self.db_path, result)
                raise RuntimeError(f"数据库完整性检查失败: {self.db_path} — {result}")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning("DB integrity_check 执行异常: %s -> %s", self.db_path, e)

    def _cleanup_orphan_wal_shm(self) -> None:
        """清理无主库的孤儿 WAL/SHM 文件（仅首次 init_db 时触发）。

        来源：Finder 复制/移动 .db 时自动命名（如 linkora 2.db），副本主文件
        被删/移走后 -wal/-shm 残留；SQLite 的 -wal/-shm 只在主库存在时才有
        意义，无主库时是无效文件，留着只会堆积垃圾（曾出现 31 个孤儿 4.7MB）。
        只删「无对应 .db」的 -wal/-shm，绝不碰活动库；类属性去重同路径只扫一次。
        """
        _cls = type(self)
        if not hasattr(_cls, "_cleaned_orphan_paths"):
            # 同上：类型声明在 SQLiteStoreBase（ClassVar）
            _cls._cleaned_orphan_paths = set()
        if self.db_path in _cls._cleaned_orphan_paths:
            return
        _cls._cleaned_orphan_paths.add(self.db_path)

        dirs = [Path(self.db_path).parent, Path(self._conv_root)]
        for d in dirs:
            if not d.is_dir():
                continue
            dbs = {p.name for p in d.glob("*.db")}
            for suffix in ("-wal", "-shm"):
                for f in d.glob(f"*.db{suffix}"):
                    base = f.name[: -len(suffix)]
                    if base not in dbs:
                        try:
                            f.unlink()
                            logger.info("[SQLiteStore] 已清理孤儿 WAL/SHM: %s", f)
                        except OSError as exc:
                            logger.warning("[SQLiteStore] 清理孤儿文件失败 %s: %s", f, exc)

    def init_db(self) -> None:
        # 首次初始化时执行 PRAGMA integrity_check，尽早暴露数据库文件损坏
        self._check_integrity_initial()
        # 首次初始化时清理无主库的孤儿 WAL/SHM（Finder 复制/外部移动残留）
        self._cleanup_orphan_wal_shm()
        init_schema(self.conn, self.db_path)
