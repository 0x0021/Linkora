"""SQLiteStore 连接管理 mixin（主库 conn / 会话库 conv_conn / 迁移 / 完整性检查）。

拆分自 sqlite_store.py。
"""
from __future__ import annotations
from .sqlite_store_mixins_base import SQLiteStoreBase

import contextlib
import hashlib
import logging
import os
import sqlite3
import threading
import traceback
import time
from pathlib import Path
from typing import Optional

from src.memory import account_identity
from src.memory.schema import init_conv_schema, init_schema

logger = logging.getLogger(__name__)


def _caller_label(skip_files: tuple[str, ...] = ("contextlib.py", "sqlite_store_conn.py")) -> str:
    """向上穿透 contextlib / 本文件，返回**首个业务调用者**的 ``文件:行:函数``。

    【P0 2026-10-05 排障】``_write_gate`` 是 ``@contextlib.contextmanager``，
    直接用 ``sys._getframe(1)`` 拿到的是 ``contextlib.py:__enter__`` —— 毫无
    信息量（实测踩过，输出恒为 ``contextlib.py:141:__enter__``）。故必须跳过
    框架帧与本文件帧，取第一个业务调用点。
    """
    try:
        for f in reversed(traceback.extract_stack(limit=12)[:-1]):
            name = Path(f.filename).name
            if name in skip_files:
                continue
            return f"{name}:{f.lineno}:{f.name}"
    except Exception:  # noqa: BLE001
        pass
    return "<unknown>"


class SQLiteStoreConnMixin(SQLiteStoreBase):
    # 【P0 2026-10-05】写入走 :meth:`_write_gate` 进程内串行化闸门。
    # 锁与深度计数的定义与「为何需要」详见 SQLiteStoreBase 同名类变量注释。
    # 教训：e2997cb(2026-09-18) 修好读游标泄漏 = 拆掉了「读事务挡住写入」的天然
    # 闸门，若不同步给写入侧加限流与自愈，跨进程争用会立即爆发（该日之前日志零锁错误）。

    @classmethod
    @contextlib.contextmanager
    def _write_gate(cls, timeout: float = 60.0):
        """进程内写入串行化闸门（可重入，避免同一线程嵌套自锁）。

        用 threading.local 记深度：``write_with_retry`` 内部可能再调用
        ``write_with_retry``（例如 repo 方法互相包装），重入时直接放行以免自锁。
        """
        # 惰性初始化（与 _conv_schema_init_lock 同一模式）：基类只做类型声明，
        # 值在此首次使用时创建，所有实例共享同一把锁。
        if not hasattr(cls, "_write_gate_lock"):
            cls._write_gate_lock = threading.Lock()
        if not hasattr(cls, "_write_gate_depth"):
            cls._write_gate_depth = threading.local()
        depth = getattr(cls._write_gate_depth, "value", 0)
        if depth > 0:
            # 已在本线程的闸门内（嵌套调用）→ 直接放行
            cls._write_gate_depth.value = depth + 1
            try:
                yield
            finally:
                cls._write_gate_depth.value = depth
            return
        if not cls._write_gate_lock.acquire(timeout=timeout):
            # 闸门等不到（写入堆积过久）→ 不静默阻塞主流程，告警后放行
            logger.warning(
                "[SQLite] 写入闸门等待 %.0fs 超时（写堆积过多），本次放行", timeout
            )
            yield
            return
        # 【诊断】记录当前持有者：锁争用时「闸门被谁霸着」是唯一关键线索。
        # ⚠️ 必须**向上穿透 contextlib**：_write_gate 是 @contextlib.contextmanager，
        #    sys._getframe(1) 拿到的是 contextlib.py 的 __enter__（实测输出
        #    「持有者=contextlib.py:141:__enter__」毫无信息量）。故从栈里
        #    找第一个非 contextlib / 非本文件的帧。
        try:
            cls._write_gate_holder = _caller_label()
            cls._write_gate_since = time.monotonic()
        except Exception:  # noqa: BLE001
            cls._write_gate_holder = "<unknown>"
            cls._write_gate_since = time.monotonic()
        cls._write_gate_depth.value = 1
        try:
            yield
        finally:
            cls._write_gate_depth.value = 0
            cls._write_gate_holder = ""
            cls._write_gate_lock.release()

    def _busy_timeout_ms(self) -> int:
        """解析 SQLite busy_timeout（毫秒），缺省 20000。

        单一真源：主库 ``conn`` 与会话库 ``conv_conn`` 两处 PRAGMA 共用本方法，
        避免同一参数在两处漂移。配置项 ``storage.busy_timeout_ms``。

        【为什么调大到 20s】2026-10-05 生产事故：硬编码 5000 时，双进程架构
        （web + worker 同时写同一 110MB 分库）下每分钟 20~44 次
        "database is locked"，轮询器大量丢会话。加长等待窗口让短时锁竞争
        「等过去」而非直接失败。
        ⚠️ 对 SQLITE_BUSY_SNAPSHOT（陈旧读快照）**无效**——那种是 0.0000s 立即
        失败，只能靠丢弃连接重建（``write_with_retry``）。
        """
        # 防御式 getattr：最小/测试配置（SimpleNamespace 桩）可能没有 storage 段
        storage = getattr(self, "config", None)
        storage = getattr(storage, "storage", None)
        raw = getattr(storage, "busy_timeout_ms", None)
        if not isinstance(raw, int) or raw <= 0:
            return 20000
        return raw

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
            c.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms()}")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            # 页面缓存：默认 2MB 太小，这里设 ~8MB（-8000 页 × 1KB/页 ≈ 8MB）。
            # 【内存】注意这是「每线程每连接」独立上限：worker 常驻 ~37 线程、
            # 实测已开 9 主库 + 9 会话库连接，早前的 -64000（≈62.5MB/连接）会让
            # 页缓存随查询量缓慢爬升到 GB 级，表现为「跑得越久内存越高」。
            # 【实测依据】会话库 102MB / 11.1 万条消息，单连接三档对比：
            #   62.5MB → 300 次按会话查最近 50 条 0.019s、全表扫描 0.150s、RSS 85MB
            #   16MB   → 0.014s / 0.145s / 46MB
            #   8MB    → 0.015s / 0.149s / 36MB
            # 即：真实查询走索引、热数据仅几 MB；全表扫描的 102MB 主要由 macOS 统一
            # 缓冲缓存（unified buffer cache）兜底，加大 SQLite 应用层缓存并无收益。
            c.execute("PRAGMA cache_size=-8000")
            self._conns[tid] = c
            # 【HIGH-4】首次连接时主动执行 schema 迁移（CREATE TABLE + ALTER TABLE 补齐缺列），
            # 不依赖首次 SQL 触发时的隐式异常恢复。`init_db()` 内部幂等，重复调用安全。
            # 【CRITICAL】init_db() 必须在 _conns_lock 之外调用：此前在锁内调用 init_db，
            # 若 sqlite3.connect / PRAGMA integrity_check 因文件锁阻塞，Worker 线程将永远持有
            # _conns_lock，主线程后续访问 self.conn 时永久死锁。现改为锁内原子设置标志位、
            # 锁外执行 init_db，失败时回退标志位以便下一个线程重试。
            # 【P0-race-fix】schema 初始化改为【类级、按 db_path 串行去重】：Web 会为同一
            # linkora.db 创建多个 store 实例，原实例级 _schema_initialized 无法阻止跨实例
            # 并发 init_schema，导致多连接同时 ALTER 同列撞 "duplicate column name"（HIGH-5
            # 新增的 status/superseded_by 等列尤甚）。现用类级锁 + 已初始化路径集合，保证
            # 同一物理文件全局只真正初始化一次，彻底消除 DDL 竞态。
            _cls = type(self)
            if not hasattr(_cls, "_schema_init_lock"):
                _cls._schema_init_lock = threading.Lock()
            if not hasattr(_cls, "_schema_initialized_paths"):
                _cls._schema_initialized_paths = set()
            with _cls._schema_init_lock:
                need_init = self.db_path not in _cls._schema_initialized_paths
                if need_init:
                    _cls._schema_initialized_paths.add(self.db_path)
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
                        except sqlite3.Error as e:
                            logger.debug("关闭旧连接失败: %s", e)
                        self._conns.pop(otid, None)
                        break  # 每次只回收一个，避免一次性关闭过多
        # ── init_db 在 _conns_lock 之外执行，避免阻塞时锁死整个 store ──
        if need_init:
            try:
                self.init_db()
            except sqlite3.Error as e:
                # 失败时回退标志位，下一个线程访问 conn 时会重新尝试 init_db
                self._schema_initialized = False
                type(self)._schema_initialized_paths.discard(self.db_path)
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

    def conv_db_path(self, platform: str, fallback_corp_id: Optional[str] = None) -> str:
        """返回某平台【当前账号】会话 DB 的物理路径（只读计算，不创建目录/不打开连接）。

        与 :meth:`_conv_db_path` 同公式，但**不**调用 ``os.makedirs``、不打开连接，
        供外部（如孤儿会话库扫描）在不产生副作用的前提下拿到"应用实际会写入的会话分库
        路径"，用于判断 ``data/conversations/`` 下哪些分库仍被活跃平台绑定。

        注意：``db_path``（全局主库 ``linkora.db``）与此处的会话分库是**不同文件**；
        孤儿扫描必须用本方法而非 ``db_path``，否则会把全部会话分库误判为孤儿。
        """
        account_id = account_identity.resolve_account_id(platform, fallback_corp_id)
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]
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
                except sqlite3.Error as _close_err:  # noqa: BLE001
                    logger.debug("[账号隔离] 关闭旧会话连接失败: %s", _close_err)
            existed = os.path.exists(path)
            c = sqlite3.connect(path)
            c.row_factory = sqlite3.Row
            c.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms()}")
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA cache_size=-8000")  # 与主库一致（每连接上限，见上方实测依据）
            # 【P0-锁竞争】会话库 schema 与主库同样「每个物理文件、每进程只初始化一次」。
            # 此前每次新建连接都跑 init_conv_schema —— 其中含 CREATE TABLE/CREATE INDEX 等
            # DDL 与 boundary_ts 回填 UPDATE，每次都要取写锁；轮询器（线程池并发抓取）与
            # 摘要调度器各自新建连接时反复取锁、互相挤锁，实测导致 poller 的
            # upsert_conversation 报 "database is locked"（单会话抓取被跳过）。
            _ccls = type(self)
            if not hasattr(_ccls, "_conv_schema_init_lock"):
                _ccls._conv_schema_init_lock = threading.Lock()
            if not hasattr(_ccls, "_conv_schema_initialized_paths"):
                _ccls._conv_schema_initialized_paths = set()
            with _ccls._conv_schema_init_lock:
                need_conv_init = path not in _ccls._conv_schema_initialized_paths
                if need_conv_init:
                    _ccls._conv_schema_initialized_paths.add(path)
            if need_conv_init:
                try:
                    init_conv_schema(c, path)
                except sqlite3.Error as e:
                    # 回退标志：下一个线程新建连接时会重试初始化
                    _ccls._conv_schema_initialized_paths.discard(path)
                    logger.error("会话库 schema 初始化失败 %s: %s", path, e)
                    raise
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
                        except sqlite3.Error as e:
                            logger.debug("关闭旧会话连接失败: %s", e)
                        self._conv_conns.pop((otid, oplat), None)
                        break
        # 首次为新账号创建会话库 → 从主库迁移既有会话数据（归属当前账号）
        if need_migrate:
            try:
                self._migrate_main_to_conv(c, platform)
            except sqlite3.Error as e:  # noqa: BLE001
                logger.warning("[账号隔离] 主库→会话库迁移失败（不影响新库使用）: %s", e)
        return c

    def discard_conv_conn(self, platform: str = "") -> None:
        """丢弃【当前线程】缓存的会话库连接（下次 ``conv_conn`` 会新建一个）。

        【为什么需要】长生命周期线程（如摘要调度器 worker）复用同一连接；若连接上
        残留未关闭游标，就会持有陈旧的 WAL 读快照，此后该连接写库稳定报
        "database is locked"（SQLITE_BUSY_SNAPSHOT；busy_timeout 不生效、rollback()
        /commit() 也清不掉——已实测 100% 复现）。此时「关掉并重建连接」是唯一可靠的
        恢复手段，故供调用方在写失败时自愈。仅影响当前线程，不干扰其他线程连接。
        """
        platform = (platform or "").lower()
        tid = threading.get_ident()
        with self._conv_conns_lock:
            entry = self._conv_conns.pop((tid, platform), None)
        if entry is not None:
            try:
                entry[1].close()
            except sqlite3.Error as e:
                logger.debug("丢弃会话连接失败（可忽略）: %s", e)

    def write_with_retry(self, fn, platform: str = "", *, max_attempts: int = 3) -> None:
        """执行一次会话库写操作，**进程内串行化** + 遇锁失败自动自愈重试。

        【P0 2026-10-05 事故根因与本方法的由来】

        git 溯源：``e2997cb``（2026-09-18）之前**从不报** database is locked。原因是
        当时读游标从不关闭 → WAL 读事务一直悬着 → **写入被 SQLite 自然挡在门外**。
        该提交「修好」了游标泄漏（本身正确），闸门被拆除，而配套的写入限流与自愈
        并未同步，于是写入开始真枪实弹抢锁，跨进程争用立即爆发。
        铁证：``logs/linkora.log.1 5``（09-06，21747 行、已有展示摘要）锁错误 0 条。

        **教训：修掉读游标泄漏 = 解除写入的天然闸门，必须同步重建写入侧闸门。**
        本方法就是那个闸门——用**显式串行化**替代「靠 bug 挡写入」：

        - ``_write_gate`` 是**进程内**的全局写锁，把本进程内所有 ``write_with_retry``
          写入排成队列。SQLite 同一时刻只允许一个写者，串行化后本进程不再自己撞自己。
        - ⚠️ **无法解决跨进程争用**：web 与 worker 是两个进程，各有自己的闸门。
          跨进程仍须靠 busy_timeout 等待 + ``scripts/run_linkora.py --single-process``
          合并为单进程才能根治。
        - 闸门只包住「拿到连接 → 执行写入 → commit」这一段，读操作不受影响，
          故不会把读路径拖慢。

        判别口诀（实测）：**「立即失败」= 陈旧读快照**（busy_timeout 无效，只能丢连接
        重建）；**「等满 busy_timeout 才失败」= 真锁等待**（靠等待或串行化解）。

        ⚠️ **闸门绝不能被长等待霸占**（2026-10-05 现场教训）：若闸门包住整个
        「busy_timeout 等待 + 重试」过程，则单次调用最坏耗时
        ``20s × 3 次 = 60s``，期间**所有其他写线程全部堵在闸门上**，
        反而放大写堆积（现场日志：「写入闸门等待 60s 超时」）。

        因此本方法的结构是**「每次尝试各取一次闸门，闸门只包住单次 execute+commit」**：
        等待 busy_timeout 的时间发生在**闸门之外**——等锁的线程不占闸门，
        让正在写的那一方能尽快完成并释放。
        """
        for attempt in range(1, max_attempts + 1):
            conn = self.conv_conn(platform)   # 闸门外取连接
            try:
                # 闸门只包住「execute + commit」，不包住 busy_timeout 等待
                with self._write_gate():
                    fn(conn)
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt >= max_attempts:
                    raise
                # 【诊断】打出调用栈与库路径——锁争用时必须知道「谁在写、写的哪个库」。
                # ⚠️ 用 WARNING 且**单行**输出：多行栈会被日志格式截断成 "[SQLite] ..."，
                # 什么也看不到（实测踩过）。压成一行用 " ← " 串联调用者。
                try:
                    _stack = " ← ".join(
                        f"{Path(f.filename).name}:{f.lineno}:{f.name}"
                        for f in traceback.extract_stack(limit=8)[:-1]
                        if "sqlite_store" not in f.filename
                    )
                except Exception:  # noqa: BLE001
                    _stack = "<stack unavailable>"
                logger.warning(
                    "[SQLite] 写锁来源 self=%s platform=%s db=%s 链路: %s",
                    id(self), platform or "(ctx)",
                    Path(self._conv_db_path_safe(platform)).name, _stack,
                )
                logger.warning(
                    "[SQLite] 写库锁失败，自愈重试 %d/%d db=%s 闸门持有者=%s: %s",
                    attempt, max_attempts,
                    Path(self._conv_db_path_safe(platform)).name,
                    self._write_gate_current_holder() or "(无·锁在别处)",
                    e,
                )
                self.discard_conv_conn(platform)
                time.sleep(0.05 * attempt)  # 轻微退避，给并发写让出窗口

    def _write_gate_current_holder(self) -> str:
        """诊断用：返回当前持有全局写闸门的线程标识（无持有者则空串）。

        【P0 2026-10-05 排障】锁争用时「谁在持锁」是唯一关键信息。外部工具
        （py-spy/faulthandler）都不可用时，靠这个自报机制定位。
        """
        holder = getattr(type(self), "_write_gate_holder", "")
        if not holder:
            return ""
        import time as _t
        return f"{holder} (持锁 {_t.monotonic() - getattr(type(self), '_write_gate_since', 0):.1f}s)"

    def _conv_db_path_safe(self, platform: str = "") -> str:
        """诊断用：取会话库路径，任何失败都降级为占位符（绝不因诊断而抛异常）。"""
        try:
            from src.memory import account_identity
            plat = (platform or "").lower()
            return self._conv_db_path(plat, account_identity.resolve_account_id(plat))
        except Exception:  # noqa: BLE001
            return "<unknown>"

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
            except sqlite3.Error as e:  # noqa: BLE001
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
        except (sqlite3.Error, OSError) as e:
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
