"""工作时间外未回复消息与待办创建记录的仓储层。

两张表都落在**平台主库**（``data/linkora.db`` / ``feishu-ai.db`` / ``wecom-ai.db``），
平台隔离由 store 实例本身保证；记录里的 platform 字段是冗余列，便于 Web 跨库聚合时
不再二次拼装。所有写操作经 ``store.write_main_with_retry()`` 收口，禁止直连 commit。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

STATUSES = ("pending", "todo_created", "notified_only", "ignored", "replied_later", "todo_failed")


class OffhoursTodoRepo:
    """未回复消息（offhours_unanswered）+ 待办记录（offhours_todo_records）仓储。"""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    # ---------- 未回复消息 ----------

    def upsert_unanswered(
        self,
        *,
        platform: str,
        chat_id: str,
        msg_id: str,
        dedupe_key: str,
        sender_id: str = "",
        sender_name: str = "",
        chat_name: str = "",
        chat_type: str = "",
        content_preview: str = "",
        msg_timestamp: str = "",
        reason: str = "",
        detail: str = "",
    ) -> bool:
        """落一条未回复消息。dedupe_key 唯一，重复扫描返回 False 不覆盖已有状态。

        Returns:
            True = 新插入；False = 已存在（去重命中）。
        """
        now = datetime.now().isoformat(timespec="seconds")

        def _do(conn):
            cur = conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO offhours_unanswered
                   (platform, chat_id, chat_name, chat_type, msg_id, sender_id, sender_name,
                    content_preview, msg_timestamp, reason, status, dedupe_key,
                    detected_at, updated_at, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (
                    platform, chat_id, chat_name, chat_type, msg_id,
                    sender_id, sender_name, (content_preview or "")[:500],
                    msg_timestamp, reason, dedupe_key, now, now, detail or "",
                ),
            )
            inserted = cur.rowcount > 0
            conn.commit()
            return inserted

        return bool(self.store.write_main_with_retry(_do))

    def list_unanswered(
        self,
        *,
        status: str = "",
        platform: str = "",
        chat_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页查询未回复消息，按消息时间倒序。"""
        where: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if platform:
            where.append("platform = ?")
            params.append(platform)
        if chat_id:
            where.append("chat_id = ?")
            params.append(chat_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        cur = self.store.conn.cursor()
        total = cur.execute(
            "SELECT COUNT(*) FROM offhours_unanswered" + clause, params
        ).fetchone()[0]
        rows = cur.execute(
            "SELECT * FROM offhours_unanswered" + clause
            + " ORDER BY msg_timestamp DESC, id DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), max(0, offset)],
        ).fetchall()
        return [dict(r) for r in rows], int(total or 0)

    def list_pending_grouped(self, *, platform: str = "", limit: int = 500) -> dict[str, list[dict[str, Any]]]:
        """按会话聚合 pending 消息，供建单时按会话整理（不分页，受 limit 约束）。"""
        where = "status = 'pending'"
        params: list[Any] = []
        if platform:
            where += " AND platform = ?"
            params.append(platform)
        cur = self.store.conn.cursor()
        rows = cur.execute(
            "SELECT * FROM offhours_unanswered WHERE " + where
            + " ORDER BY chat_id, msg_timestamp ASC LIMIT ?",
            [*params, max(1, min(limit, 1000))],
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            row = dict(r)
            grouped.setdefault(row["chat_id"], []).append(row)
        return grouped

    def update_unanswered_status(
        self,
        dedupe_key: str,
        status: str,
        *,
        detail: str = "",
    ) -> bool:
        """更新单条消息状态；仅允许在 STATUSES 白名单内的值，防注入。"""
        if status not in STATUSES:
            logger.warning("[未回复待办] 拒绝非法状态 %s", status)
            return False

        def _do(conn):
            cur = conn.cursor()
            cur.execute(
                "UPDATE offhours_unanswered SET status = ?, detail = ?, updated_at = ? "
                "WHERE dedupe_key = ?",
                (status, detail or "", datetime.now().isoformat(timespec="seconds"), dedupe_key),
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated

        return bool(self.store.write_main_with_retry(_do))

    def mark_replied_later(self, *, chat_id: str, up_to_ts: str) -> int:
        """回填「后来已由真人回复」的消息为 replied_later。

        判定口径与 ``ConversationRepo.has_user_message_from`` 一致：sender_id ∈ 本人 ID
        集合且 is_bot=0，这一步由调用方在扫描会话库时完成；这里按「回复时间点之前」的
        时间窗批量回填该会话的 pending 项。

        Returns:
            回填条数。
        """
        def _do(conn):
            cur = conn.cursor()
            cur.execute(
                "UPDATE offhours_unanswered SET status = 'replied_later', "
                "detail = '本人已回复', updated_at = ? "
                "WHERE chat_id = ? AND status = 'pending' AND msg_timestamp <= ?",
                (datetime.now().isoformat(timespec="seconds"), chat_id, up_to_ts),
            )
            updated = cur.rowcount
            conn.commit()
            return updated

        return int(self.store.write_main_with_retry(_do))

    def get_unanswered_by_key(self, dedupe_key: str) -> dict[str, Any] | None:
        cur = self.store.conn.cursor()
        row = cur.execute(
            "SELECT * FROM offhours_unanswered WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return dict(row) if row else None

    # ---------- 待办创建记录 ----------

    def insert_todo_record(
        self,
        *,
        platform: str,
        chat_id: str,
        msg_id: str,
        dedupe_key: str,
        todo_id: str = "",
        todo_type: str = "",
        title: str = "",
        status: str = "created",
        sender_notified: bool = False,
        owner_notified: bool = False,
        error: str = "",
    ) -> bool:
        """落一条建单记录。dedupe_key 唯一 → 重复建单被忽略（返回 False）。"""
        def _do(conn):
            cur = conn.cursor()
            cur.execute(
                """INSERT OR IGNORE INTO offhours_todo_records
                   (platform, chat_id, msg_id, dedupe_key, todo_id, todo_type, title,
                    status, sender_notified, owner_notified, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    platform, chat_id, msg_id, dedupe_key, todo_id, todo_type, title,
                    status, int(sender_notified), int(owner_notified), error or "",
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            inserted = cur.rowcount > 0
            conn.commit()
            return inserted

        return bool(self.store.write_main_with_retry(_do))

    def has_todo_record(self, dedupe_key: str) -> bool:
        cur = self.store.conn.cursor()
        row = cur.execute(
            "SELECT 1 FROM offhours_todo_records WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row is not None

    def list_todo_records(
        self,
        *,
        platform: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where = ""
        params: list[Any] = []
        if platform:
            where = " WHERE platform = ?"
            params.append(platform)
        cur = self.store.conn.cursor()
        total = cur.execute(
            "SELECT COUNT(*) FROM offhours_todo_records" + where, params
        ).fetchone()[0]
        rows = cur.execute(
            "SELECT * FROM offhours_todo_records" + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), max(0, offset)],
        ).fetchall()
        return [dict(r) for r in rows], int(total or 0)

    def get_stats(self) -> dict[str, int]:
        """各状态计数，供 Web 概览卡片。"""
        cur = self.store.conn.cursor()
        stats = {s: 0 for s in STATUSES}
        for row in cur.execute(
            "SELECT status, COUNT(*) AS n FROM offhours_unanswered GROUP BY status"
        ):
            if row["status"] in stats:
                stats[row["status"]] = int(row["n"])
        stats["todo_total"] = int(cur.execute(
            "SELECT COUNT(*) FROM offhours_todo_records WHERE status = 'created'"
        ).fetchone()[0] or 0)
        return stats

    def cleanup_old_records(self, retention_days: int) -> int:
        """清理超过保留期的旧记录，返回删除条数（由调度器低频调用）。"""
        from datetime import timedelta

        cutoff = (datetime.now() - timedelta(days=max(1, retention_days))).isoformat(timespec="seconds")

        def _do(conn):
            cur = conn.cursor()
            cur.execute("DELETE FROM offhours_unanswered WHERE detected_at < ?", (cutoff,))
            n1 = cur.rowcount
            cur.execute("DELETE FROM offhours_todo_records WHERE created_at < ?", (cutoff,))
            n2 = cur.rowcount
            conn.commit()
            return n1 + n2

        return int(self.store.write_main_with_retry(_do))
