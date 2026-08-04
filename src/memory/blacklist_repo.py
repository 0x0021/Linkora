"""Repository for blacklist repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self._cc() for per-thread connection access. Zero behavior change.

Performance: memory cache layer eliminates DB roundtrips for read-heavy
is_conversation_blocked() / cooldown_remaining() — cache is loaded from DB
on first access and synced on every write.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional

from src.memory.platform_context import get_current_platform

logger = logging.getLogger(__name__)

# 哨兵：区分"缓存中不存在"与"缓存值为 None（永久黑名单）"
_SENTINEL = object()


class BlacklistRepo:
    """Repository extracted from SQLiteStore for blacklist operations.

    Read-path optimization: maintains an in-memory dict cache of
    {chat_id: cooldown_until} to avoid per-call DB queries.  Writes
    (add / upgrade / remove / clear) go DB-first then update cache.
    """

    DEFAULT_COOLDOWN_HOURS = 1  # 默认冷却时长

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store
        # ── 内存缓存 {chat_id: cooldown_until} ──
        # cooldown_until=None → 永久黑名单；有值 → 临时冷却到期时间
        self._cache: dict[str, str | None] = {}
        self._cache_loaded: bool = False
        self._cache_lock = threading.Lock()

    def _cc(self) -> sqlite3.Connection:
        """按当前平台/账号隔离的会话连接（blocked_conversations 属会话数据）。"""
        return self.store.conv_conn(get_current_platform())

    # ──────────────── cache helpers ────────────────

    def _ensure_cache_loaded(self) -> None:
        """首次访问时从 DB 全量加载到内存 dict。幂等，线程安全。"""
        if self._cache_loaded:
            return
        with self._cache_lock:
            if self._cache_loaded:
                return
            cur = self._cc().cursor()
            cur.execute(
                "SELECT chat_id, cooldown_until FROM blocked_conversations"
            )
            for row in cur.fetchall():
                cid = (row["chat_id"] or "").rstrip("=")
                if cid:
                    self._cache[cid] = row["cooldown_until"]  # None → 永久
            self._cache_loaded = True
            logger.debug(
                "BlacklistRepo cache loaded: %d entries", len(self._cache)
            )

    def _update_cache(self, chat_id: str, cooldown_until: str | None) -> None:
        """同步写入缓存（add / upgrade 后调用）。"""
        with self._cache_lock:
            self._cache[chat_id] = cooldown_until

    def _remove_from_cache(self, chat_id: str) -> None:
        """从缓存摘除（remove / clear 后调用）。"""
        with self._cache_lock:
            self._cache.pop(chat_id, None)

    # ──────────────── public API ────────────────

    def add_blocked_conversation(self, chat_id: str, chat_name: str = "",
                                 chat_type: str = "", reason: str = "",
                                 source: str = "", last_error: str = "",
                                 cooldown_until: str | None = None,
                                 failure_count: int | None = None) -> None:
        """将某个会话加入不遍历黑名单。

        两种模式：
        1. 永久黑名单：cooldown_until=None（默认）→ 重启后仍不可发。
        2. 临时冷却：cooldown_until="2026-07-22T12:00:00"→ 到期后 is_conversation_blocked 返回 False。
           用于「跨租户外部好友不一律黑名单：失败 1/2 次仅冷却重试，3 次后才升级为永久黑名单」。

        failure_count：连续失败计数。None = 不动（保留已有计数）。调用方决定何时升级。
        """
        chat_id = (chat_id or "").rstrip("=")
        if not chat_id:
            return
        cur = self._cc().cursor()
        now = datetime.now().isoformat()
        # 如果调用方未显式给 failure_count，从表中读出后 +1，保留连续计数
        if failure_count is None:
            cur.execute("SELECT failure_count FROM blocked_conversations WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            failure_count = (int(row["failure_count"]) + 1) if row and row["failure_count"] is not None else 1
        # cooldown_until 默认走「1h 冷却」（不传则自动设）。只有显式传空字符串
        # 才表示永久黑名单。ON CONFLICT 保留旧 cooldown_until 不被覆盖—— chat.py 会
        # 读出后 UPDATE 设正确时间，以避免被刚 INSERT 的 NULL 擦掉。
        if cooldown_until is None:
            cur.execute("SELECT cooldown_until FROM blocked_conversations WHERE chat_id = ?", (chat_id,))
            existing = cur.fetchone()
            if existing is None:
                cooldown_until = (datetime.now() + timedelta(hours=self.DEFAULT_COOLDOWN_HOURS)).isoformat()
            else:
                cooldown_until = existing["cooldown_until"]  # 保留旧值
        cur.execute(
            """INSERT INTO blocked_conversations
               (chat_id, chat_name, chat_type, reason, detected_at, source, last_error,
                cooldown_until, failure_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 chat_name=excluded.chat_name,
                 chat_type=excluded.chat_type,
                 reason=excluded.reason,
                 detected_at=excluded.detected_at,
                 source=excluded.source,
                 last_error=excluded.last_error,
                 cooldown_until=excluded.cooldown_until,
                 failure_count=excluded.failure_count""",
            (chat_id, chat_name or "", chat_type or "", reason or "",
             now, source or "", (last_error or "")[:1000],
             cooldown_until, int(failure_count)),
        )
        self._cc().commit()
        # 同步缓存（写入后最终 cooldown_until 已确定）
        self._update_cache(chat_id, cooldown_until)

    def upgrade_to_permanent_block(self, chat_id: str) -> bool:
        """把临时冷却升级为永久黑名单（cooldown_until=NULL）。返回是否执行了更新。"""
        chat_id = str(chat_id or "").rstrip("=")
        if not chat_id:
            return False
        cur = self._cc().cursor()
        cur.execute(
            "UPDATE blocked_conversations SET cooldown_until=NULL, "
            "reason=COALESCE(reason, '') || ' [升级永久黑名单]' "
            "WHERE chat_id = ?",
            (chat_id,),
        )
        self._cc().commit()
        updated = cur.rowcount > 0
        if updated:
            self._update_cache(chat_id, None)  # 永久黑名单
        return updated

    def is_conversation_blocked(self, chat_id: str) -> bool:
        """是否不可发送（优先读缓存）。

        返回 True 的两种情况：
        1. 永久黑名单（cooldown_until IS NULL 且记录存在）。
        2. 临时冷却中（cooldown_until 未到期）。

        返回 False 的两种情况：
        1. 记录不存在。
        2. 临时冷却已到期（cooldown_until < now）——需调用方清理该行或由下次发送重新写入。
        """
        chat_id = str(chat_id).rstrip("=")
        if not chat_id:
            return False
        self._ensure_cache_loaded()
        with self._cache_lock:
            cu = self._cache.get(chat_id, _SENTINEL)
        if cu is _SENTINEL:
            return False  # 不在缓存 = 不在黑名单
        if cu is None or cu == "":
            return True  # 永久黑名单
        return cu > datetime.now().isoformat()

    def cooldown_remaining(self, chat_id: str) -> int:
        """返回冷却剩余秒数。未在冷却/不存在/永久黑名单时返回 0。"""
        chat_id = str(chat_id).rstrip("=")
        if not chat_id:
            return 0
        self._ensure_cache_loaded()
        with self._cache_lock:
            cu = self._cache.get(chat_id, _SENTINEL)
        if cu is _SENTINEL or not cu:
            return 0
        try:
            until = datetime.fromisoformat(cu)
            return max(0, int((until - datetime.now()).total_seconds()))
        except Exception:
            logger.warning("[resilience] silent exception in cooldown_remaining", exc_info=True)
            return 0

    def load_blocked_conversations(self) -> list[dict]:
        """加载所有黑名单会话（启动时用于重建内存跳过集合）。

        返回的 chat_id 统一去掉末尾 '='，与 add_blocked_conversation 写入规范一致。
        """
        cur = self._cc().cursor()
        cur.execute("SELECT chat_id, chat_name, chat_type, reason, detected_at, source, last_error, cooldown_until, failure_count FROM blocked_conversations")
        rows = [dict(row) for row in cur.fetchall()]
        for r in rows:
            if r.get("chat_id"):
                r["chat_id"] = r["chat_id"].rstrip("=")
        return rows

    def remove_blocked_conversation(self, chat_id: str) -> None:
        chat_id = str(chat_id).rstrip("=")
        cur = self._cc().cursor()
        cur.execute("DELETE FROM blocked_conversations WHERE chat_id = ?", (chat_id,))
        self._cc().commit()
        self._remove_from_cache(chat_id)

    def list_blocked_conversations(self) -> list[dict]:
        """列出黑名单（供状态查询/日志）。"""
        return self.load_blocked_conversations()

    def clear_blocked_conversations(self) -> int:
        """清空黑名单表，返回清除数量。"""
        cur = self._cc().cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM blocked_conversations")
        row = cur.fetchone()
        count = row["cnt"] if row else 0
        cur.execute("DELETE FROM blocked_conversations")
        self._cc().commit()
        with self._cache_lock:
            self._cache.clear()
        return count

