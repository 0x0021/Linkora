"""Repository for conversation repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter. 会话相关 6 张表
（conversations / conversation_summaries / messages / external_friends /
blocked_conversations / dedup_messages）物理上位于 per-account 会话库，通过
``self.store.conv_conn(platform)`` 访问，与主库（平台无关表）隔离，实现「重登录换
账号后旧 chat_id/open_id 不再串台」。每个公开方法新增 ``platform`` 参数，由调用方
（poller/agent/runtime，均已知 platform_id）透传。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Optional, TYPE_CHECKING

from src.memory.sqlite_store import ConversationSummaryRow
from src.memory.platform_context import get_current_platform
from src.memory.image_cleanup import purge_orphan_images
from src.paths import data_path

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


def _today_start_iso() -> str:
    """返回本地时区今日 00:00:00 的 ISO 字符串。"""
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _since_iso(window: str = "today") -> str:
    """按时间窗口返回起始 ISO 字符串，供 list_recent_summaries 的 since 参数使用。

    窗口可选值：
      - "today"   : 今日 00:00:00（默认）
      - "yesterday": 昨日 00:00:00
      - "7days"   : 7 天前 00:00:00
    """
    now = datetime.now()
    if window == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if window == "yesterday":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if window == "7days":
        return (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    # 兜底：今日
    return _today_start_iso()


class ConversationRepo:
    """Repository extracted from SQLiteStore for conversation operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _write(self, fn, platform: str = "") -> None:
        """执行一次写操作：进程内写闸门 + 锁失败自愈重试（委托 store 层）。

        【P0 2026-10-05】**所有**写路径必须走本方法，不得直接 ``self._cc().execute(...)``。

        事故背景见 ``MessageRepo._write`` 的说明（同一根因：``e2997cb`` 修好读游标
        泄漏 = 拆掉了「悬空读事务」这个天然写入闸门，而写入侧未同步补闸门）。

        本类的写方法此前只包了 3 个（upsert_conversation / update_last_reply_time /
        update_last_replied_msg_id），其余（delete_conversations / batch_update_chat_types /
        upsert_conversation_summary / upsert_display_summary / update_summary_updated_at）
        仍在闸门外 → 单进程下依然撞锁。**收口必须做全。**
        """
        self.store.write_with_retry(fn, platform or get_current_platform())

    def _write_result(self, fn: Callable[[sqlite3.Connection], bool], platform: str = "") -> bool:
        """同 :meth:`_write`，但返回 ``fn`` 的 bool 结果（用于需要写成功标志的路径）。"""
        box: list[bool] = []

        def _wrapped(conn: sqlite3.Connection) -> None:
            box.append(fn(conn))

        self.store.write_with_retry(_wrapped, platform or get_current_platform())
        return box[0] if box else False

    def _cc(self, platform: str) -> sqlite3.Connection:
        plat = platform or get_current_platform()
        return self.store.conv_conn(plat)

    def get_last_reply_time(self, chat_id: str, platform: str = "") -> Optional[str]:
        """获取某个会话最后回复时间（ISO 格式），用于回复冷却。"""
        cur = self._cc(platform).cursor()
        cur.execute("SELECT last_reply_time FROM conversations WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def get_latest_user_message_time(self, chat_id: str, exclude_msg_id: str | None = None,
                                     platform: str = "") -> Optional[str]:
        """获取会话中最新用户消息的时间戳（可排除指定 msg_id）。"""
        cur = self._cc(platform).cursor()
        if exclude_msg_id:
            cur.execute(
                "SELECT MAX(timestamp) FROM messages WHERE chat_id = ? AND role = 'user' AND msg_id != ?",
                (chat_id, exclude_msg_id),
            )
        else:
            cur.execute(
                "SELECT MAX(timestamp) FROM messages WHERE chat_id = ? AND role = 'user'",
                (chat_id,),
            )
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def update_last_reply_time(self, chat_id: str, chat_type: str = "unknown",
                               platform: str = "") -> None:
        """更新某个会话的最后回复时间为现在。"""
        now = datetime.now().isoformat()
        plat = platform or get_current_platform()

        def _write(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE conversations SET last_reply_time = ?, updated_at = ? WHERE chat_id = ?",
                    (now, now, chat_id)
                )
                if cur.rowcount == 0:
                    cur.execute(
                        "INSERT OR IGNORE INTO conversations (chat_id, chat_type, last_reply_time, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (chat_id, chat_type, now, now, now)
                    )
            finally:
                cur.close()
            conn.commit()

        self.store.write_with_retry(_write, plat)

    def get_last_replied_msg_id(self, chat_id: str, platform: str = "") -> Optional[str]:
        """获取会话最后回复过的用户消息 msg_id（用于基于消息 ID 的防重复回复）。"""
        cur = self._cc(platform).cursor()
        try:
            cur.execute("SELECT last_replied_msg_id FROM conversations WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        finally:
            cur.close()

    def update_last_replied_msg_id(self, chat_id: str, msg_id: str,
                                   chat_type: str = "unknown", platform: str = "") -> None:
        """记录会话最后回复过的用户消息 msg_id。"""
        now = datetime.now().isoformat()
        plat = platform or get_current_platform()

        def _write(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE conversations SET last_replied_msg_id = ?, updated_at = ? WHERE chat_id = ?",
                    (msg_id, now, chat_id),
                )
                if cur.rowcount == 0:
                    cur.execute(
                        "INSERT OR IGNORE INTO conversations (chat_id, chat_type, last_replied_msg_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (chat_id, chat_type, msg_id, now, now)
                    )
            finally:
                cur.close()
            conn.commit()

        self.store.write_with_retry(_write, plat)

    def has_user_message_from(self, chat_id: str, since_iso_ts: str,
                              sender_ids: list[str],
                              max_age_days: int = 30, platform: str = "") -> bool:
        """检查会话中是否已有用户手动发出的消息（非机器人代发）。"""
        if not sender_ids:
            return False
        cur = self._cc(platform).cursor()
        placeholders = ",".join("?" for _ in sender_ids)
        sql = (
            f"SELECT 1 FROM messages WHERE chat_id = ?"
            f" AND sender_id IN ({placeholders})"
            f" AND timestamp >= ?"
            f" AND timestamp >= datetime('now', ?, 'localtime')"
            f" AND is_bot = 0"
            f" LIMIT 1"
        )
        cur.execute(sql, (str(chat_id), *sender_ids, since_iso_ts,
                          f"-{max_age_days} days"))
        return cur.fetchone() is not None

    def upsert_conversation(self, chat_id: str, chat_name: Optional[str],
                            chat_type: str,
                            peer_user_id: str = "",
                            peer_open_dingtalk_id: str = "",
                            platform: str = "",
                            last_message_time: Optional[str] = None) -> None:
        # 跨平台防护：ou_xxx 是飞书用户级 open_id，不能作为会话级 chat_id。
        if not chat_id or str(chat_id).startswith("ou_"):
            logger.warning(
                "[SQLiteStore] 拒绝写入非法 chat_id（ou_ 前缀是用户 ID，非会话 ID）: %s, name=%s, type=%s",
                chat_id, chat_name or "(未知)", chat_type,
            )
            return
        plat = platform or get_current_platform()
        now = datetime.now().isoformat()
        # last_message_time 优先用调用方传入的真实最后消息时间（如轮询器从消息时间戳推算），
        # 缺省回落到当前时间，保持历史行为一致。
        lmt = last_message_time or now
        # 【锁自愈】经 write_with_retry 包装：SQLITE_BUSY_SNAPSHOT（陈旧读快照）下
        # 本连接写库立即失败且 busy_timeout 无效，唯一可靠恢复是丢弃连接重建。
        # 2026-10-05 线上事故：本方法是轮询器每轮必调的写点，锁失败时整个会话被跳过
        # （155 次/持续），根因就是这里没有自愈。语义与重构前完全一致。
        def _write(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            try:
                cur.execute(
                    """INSERT INTO conversations (chat_id, chat_name, chat_type, peer_user_id, peer_open_dingtalk_id, last_message_time, message_count, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                           chat_name = COALESCE(NULLIF(excluded.chat_name, ''), conversations.chat_name),
                           chat_type = excluded.chat_type,
                           peer_user_id = CASE WHEN excluded.peer_user_id != '' THEN excluded.peer_user_id ELSE conversations.peer_user_id END,
                           peer_open_dingtalk_id = CASE WHEN excluded.peer_open_dingtalk_id != '' THEN excluded.peer_open_dingtalk_id ELSE conversations.peer_open_dingtalk_id END,
                           last_message_time = excluded.last_message_time,
                           updated_at = excluded.updated_at""",
                    (chat_id, chat_name or "", chat_type, peer_user_id,
                     peer_open_dingtalk_id, lmt, now, now),
                )
            finally:
                cur.close()
            conn.commit()

        self.store.write_with_retry(_write, plat)

    def delete_conversation(self, chat_id: str, platform: str = "") -> None:
        """删除单个会话（遇权限错误时调用，避免反复重试）。

        与 :meth:`delete_conversations` 一致：级联清理 messages /
        conversation_summaries / dedup_messages，并清理关联的本地图片
        （``purge_orphan_images``），避免留下孤儿行与磁盘图片累积。
        直接复用批量实现，保证两路径行为一致。
        """
        self.delete_conversations([chat_id], platform)

    def delete_conversations(self, chat_ids: list[str], platform: str = "") -> int:
        """批量删除会话（含其消息 / 摘要 / 去重记录）。返回实际删除的会话数。

        用于 Web「批量删除消息记录」：按 chat_id 清掉 messages /
        conversation_summaries / dedup_messages，再删 conversations 行本身，
        避免留下「空会话占位」或孤儿摘要/去重记录。单库单事务，原子提交。
        """
        chat_ids = [str(c) for c in (chat_ids or []) if str(c).strip()]
        if not chat_ids:
            return 0
        # 【P0 2026-10-05】写入走 _write（进程内写闸门 + 锁失败自愈），
        # 且**磁盘 IO 移出事务**：purge_orphan_images 会扫描并 unlink 文件，
        # 在事务内执行会长时间霸占 SQLite 写锁。
        image_paths: list[str] = []
        deleted = 0

        def _do_write(conn: sqlite3.Connection) -> None:
            nonlocal image_paths, deleted
            cur = conn.cursor()
            placeholders = ",".join("?" * len(chat_ids))
            try:
                # 先收集待删消息引用的本地图片，删行后一并清理磁盘，避免整会话图片成孤儿文件累积
                cur.execute(
                    f"SELECT image_path FROM messages WHERE chat_id IN ({placeholders}) AND image_path != ''",
                    chat_ids,
                )
                image_paths = [r[0] for r in cur.fetchall()]
                cur.execute(f"DELETE FROM messages WHERE chat_id IN ({placeholders})", chat_ids)
                cur.execute(
                    f"DELETE FROM conversation_summaries WHERE chat_id IN ({placeholders})", chat_ids
                )
                cur.execute(
                    f"DELETE FROM conversation_display_summaries WHERE chat_id IN ({placeholders})", chat_ids
                )
                cur.execute(
                    f"DELETE FROM dedup_messages WHERE chat_id IN ({placeholders})", chat_ids
                )
                cur.execute(
                    f"DELETE FROM conversations WHERE chat_id IN ({placeholders})", chat_ids
                )
                deleted = cur.rowcount
            finally:
                cur.close()
            conn.commit()

        self._write(_do_write, platform)

        # 闸门外做磁盘 IO：不持 SQLite 写锁
        removed_files = purge_orphan_images(
            self.store.db_path, image_paths, base_dir=str(data_path("tmp_images"))
        )
        logger.info("[SQLiteStore] 批量删除会话 %d 个: %s，删除孤儿图片 %d 个",
                    deleted, chat_ids[:5], removed_files)
        return deleted

    def get_conversation(self, chat_id: str, platform: str = "") -> Optional[dict]:
        if not chat_id:
            return None
        cur = self._cc(platform).cursor()
        cur.execute("SELECT * FROM conversations WHERE chat_id = ?", (str(chat_id),))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_conversation_by_peer(self, peer_open_dingtalk_id: str,
                                 platform: str = "") -> Optional[dict]:
        """通过对方 open_dingtalk_id 查找会话（用于外部好友 → 真实 chat_id 映射）。"""
        if not peer_open_dingtalk_id:
            return None
        cur = self._cc(platform).cursor()
        cur.execute(
            "SELECT * FROM conversations WHERE peer_open_dingtalk_id = ? LIMIT 1",
            (str(peer_open_dingtalk_id),),
        )
        row = cur.fetchone()
        if row:
            return dict(row)
        cur.execute(
            """SELECT DISTINCT c.* FROM conversations c
               INNER JOIN messages m ON m.chat_id = c.chat_id
               WHERE m.sender_id = ? AND c.chat_id LIKE 'oc_%'
               LIMIT 1""",
            (str(peer_open_dingtalk_id),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def get_recent_conversations(self, limit: int = 20, platform: str = "") -> list[dict]:
        """获取最近有消息的会话列表。"""
        cur = self._cc(platform).cursor()
        cur.execute(
            "SELECT * FROM conversations ORDER BY last_message_time DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

    def count_conversations(self, platform: str = "") -> int:
        """会话总数（供状态面板概览）。"""
        cur = self._cc(platform).cursor()
        cur.execute("SELECT COUNT(*) FROM conversations")
        return cur.fetchone()[0]

    def list_conversations_with_preview(self, limit: int = 50,
                                        platform: str = "") -> list[dict]:
        """会话列表 + 最新消息预览 + 对方昵称 + 真实消息数，按 updated_at 倒序。

        单次查询用相关子查询取回三个衍生字段（chat_id 上有索引），避免逐行 N+1 往返：
        - last_message_preview: 该会话最新一条消息正文
        - peer_name: 最近一条非空、非 assistant 的发送者名（用于回填会话名）
        - real_count: 该会话实际消息条数（用于回填 message_count）
        """
        cur = self._cc(platform).cursor()
        cur.execute(
            """SELECT
                   c.chat_id, c.chat_name, c.chat_type, c.last_message_time,
                   c.message_count, c.updated_at,
                   (SELECT content FROM messages m WHERE m.chat_id = c.chat_id
                    ORDER BY m.timestamp DESC LIMIT 1) AS last_message_preview,
                   (SELECT sender_name FROM messages m WHERE m.chat_id = c.chat_id
                    AND m.sender_name != '' AND m.role != 'assistant'
                    ORDER BY m.timestamp DESC LIMIT 1) AS peer_name,
                   (SELECT COUNT(*) FROM messages m WHERE m.chat_id = c.chat_id) AS real_count
               FROM conversations c
               ORDER BY COALESCE(
                   (SELECT MAX(m.timestamp) FROM messages m WHERE m.chat_id = c.chat_id),
                   c.updated_at
               ) DESC LIMIT ?""",
            (limit,),
        )
        rows = [dict(row) for row in cur.fetchall()]
        cur.close()
        return rows

    def batch_update_chat_types(self, updates: list[tuple[str, str]],
                                platform: str = "") -> int:
        """批量更新会话的 chat_type 字段。"""
        if not updates:
            return 0
        now = datetime.now().isoformat()
        total_updated = 0

        def _do_write(conn: sqlite3.Connection) -> None:
            nonlocal total_updated
            cur = conn.cursor()
            try:
                for chat_id, chat_type in updates:
                    cur.execute(
                        "UPDATE conversations SET chat_type = ?, updated_at = ?"
                        " WHERE chat_id = ? AND chat_type != ?",
                        (chat_type, now, chat_id, chat_type),
                    )
                    total_updated += cur.rowcount
            finally:
                cur.close()
            conn.commit()

        self._write(_do_write, platform)
        return total_updated

    # ---- 外部好友映射（非组织内成员） ----

    def get_conversation_summary(self, chat_id: str,
                                 platform: str = "") -> "ConversationSummaryRow | None":
        """读 chat 缓存摘要（快，本地读，无 LLM）。"""
        if not chat_id:
            return None
        try:
            cur = self._cc(platform).cursor()
            cur.execute(
                """SELECT chat_id, summary_text, older_boundary_msg_id, covered_count,
                          generation, created_at, updated_at
                   FROM conversation_summaries WHERE chat_id = ?""",
                (str(chat_id),),
            )
            row = cur.fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning("[resilience] 读取会话摘要缓存失败 chat_id=%s: %s", chat_id, e)
            return None
        if row is None:
            return None
        return ConversationSummaryRow(
            chat_id=row["chat_id"],
            summary_text=row["summary_text"] or "",
            older_boundary_msg_id=row["older_boundary_msg_id"] or "",
            covered_count=int(row["covered_count"] or 0),
            generation=int(row["generation"] or 0),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            boundary_ts="",
        )

    def list_recent_summaries(self, limit: int = 20, platform: str = "",
                               since: str | None = None) -> list[dict]:
        """近期对话摘要列表（JOIN conversations 取 chat_name），按 updated_at 倒序。

        供 Web「对话摘要」页与主动触达聚合展示复用；单次查询取回，避免逐条 N+1。
        ``since`` 给定时仅返回 ``updated_at >= since`` 的摘要（用于「今日」过滤，
        调用方传 ``_today_start_iso()`` 即可只看今天生成的摘要）。
        """
        cur = self._cc(platform).cursor()
        sql = """SELECT s.chat_id, s.summary_text, s.covered_count, s.updated_at,
                        COALESCE(c.chat_name, '') AS chat_name
                 FROM conversation_summaries s
                 LEFT JOIN conversations c ON c.chat_id = s.chat_id"""
        params: list = []
        if since:
            sql += " WHERE s.updated_at >= ?"
            params.append(since)
        sql += " ORDER BY s.updated_at DESC LIMIT ?"
        params.append(int(limit))
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "chat_id": r["chat_id"],
                "summary_text": r["summary_text"] or "",
                "covered_count": int(r["covered_count"] or 0),
                "updated_at": r["updated_at"] or "",
                "chat_name": r["chat_name"] or "",
            }
            for r in rows
        ]

    def upsert_conversation_summary(
        self,
        chat_id: str,
        summary: str,
        older_boundary_msg_id: str,
        covered_count: int,
        expected_generation: int = 0,
        platform: str = "",
    ) -> bool:
        """CAS 写回 H2-A 后台摘要（state machine 边界）。"""
        if not chat_id or not summary:
            return False
        # 【P0 2026-10-05】写入走 _write_result（进程内写闸门 + 锁失败自愈重试）
        now = datetime.now().isoformat()
        new_gen = expected_generation + 1

        def _do_write(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            try:
                cur.execute(
                    """UPDATE conversation_summaries
                       SET summary_text = ?, older_boundary_msg_id = ?, covered_count = ?,
                           generation = ?, updated_at = ?
                       WHERE chat_id = ? AND generation = ?""",
                    (summary, older_boundary_msg_id, int(covered_count), new_gen, now,
                     str(chat_id), int(expected_generation)),
                )
                if cur.rowcount > 0:
                    return True
                cur.execute(
                    "SELECT 1 FROM conversation_summaries WHERE chat_id = ?",
                    (str(chat_id),),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        """INSERT INTO conversation_summaries
                               (chat_id, summary_text, older_boundary_msg_id, covered_count,
                                generation, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (str(chat_id), summary, older_boundary_msg_id, int(covered_count),
                         new_gen, now, now),
                    )
                    return True
                logger.debug(
                    "[摘要] CAS 跳过写回 chat_id=%s（代际不符：期望 %d，库已被更新）",
                    chat_id, expected_generation,
                )
                return False
            finally:
                # 【锁争用】本方法有 3 个 return 路径，游标若不 close 会悬在结果集上，
                # 在长生命周期 worker 线程里累积持锁（CPython 靠 GC 兜底但时机不可控）。
                cur.close()
                conn.commit()

        return self._write_result(_do_write, platform)

    def fetch_messages_in_range(
        self,
        start_iso: str,
        end_iso: str,
        platform: str = "",
        limit_per_chat: int = 200,
        skip_msg_types: "list[str] | None" = None,
    ) -> "dict[str, list[dict]]":
        """按时间窗口 [start_iso, end_iso) 取出消息，按 chat_id 分组返回原始行字典。

        用于「摘要连续性补跑」按自然日回放停机期消息；只取非系统类消息，
        按会话聚合，供调用方逐会话调 LLM 生成当日摘要。失败返回空 dict（非致命）。
        """
        skip = set(skip_msg_types or [])
        skip.add("system")  # 系统通知永远不进摘要
        cur = None
        try:
            cur = self._cc(platform).cursor()
            placeholders = ",".join("?" for _ in skip)
            cur.execute(
                f"""SELECT msg_id, chat_id, chat_type, sender_id, sender_name,
                           content, msg_type, timestamp
                    FROM messages
                    WHERE timestamp >= ? AND timestamp < ?
                      AND is_withdrawn = 0
                      AND (msg_type IS NULL OR msg_type NOT IN ({placeholders}))
                    ORDER BY chat_id, timestamp ASC""",
                (start_iso, end_iso, *skip),
            )
            rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            logger.warning("[摘要补跑] 查询窗口消息失败: %s", e)
            return {}
        finally:
            # 【锁争用】本查询是全表扫描（无 LIMIT），游标悬在结果集上会一直持有
            # WAL 读事务；异常路径若不显式 close，连接在该线程内长期带快照，
            # 后续写库将立即报 database is locked（SQLITE_BUSY_SNAPSHOT）。
            if cur is not None:
                cur.close()

        grouped: "dict[str, list[dict]]" = {}
        for r in rows:
            chat_id = r["chat_id"]
            bucket = grouped.setdefault(chat_id, [])
            if len(bucket) >= limit_per_chat:
                continue
            bucket.append({
                "msg_id": r["msg_id"] or "",
                "chat_id": chat_id,
                "chat_type": r["chat_type"] or "",
                "sender_id": r["sender_id"] or "",
                "sender_name": r["sender_name"] or "",
                "content": r["content"] or "",
                "msg_type": r["msg_type"] or "text",
                "timestamp": r["timestamp"] or "",
            })
        return grouped

    def update_summary_updated_at(self, chat_id: str, updated_at_iso: str,
                                   platform: str = "") -> bool:
        """覆盖某会话摘要的 updated_at（供补跑按自然日正确归日）。非致命。"""
        if not chat_id:
            return False

        def _do_write(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            try:
                cur.execute(
                    "UPDATE conversation_summaries SET updated_at = ? WHERE chat_id = ?",
                    (updated_at_iso, str(chat_id)),
                )
                ok = cur.rowcount > 0
            finally:
                cur.close()
            conn.commit()
            return ok

        try:
            # 【P0 2026-10-05】走 _write（进程内写闸门 + 锁失败自愈）
            return self._write_result(_do_write, platform)
        except Exception as e:  # noqa: BLE001
            logger.debug("[摘要补跑] 更新 updated_at 失败 chat_id=%s: %s", chat_id, e)
            return False

    # ---- 展示用全量会话摘要（与 H2-A/动态摘要解耦，专供 Web「对话摘要」页） ----
    def get_display_summary(self, chat_id: str, platform: str = "") -> "ConversationSummaryRow | None":
        """读展示用全量摘要缓存（快读，无 LLM）。"""
        if not chat_id:
            return None
        # 必须显式 close()：调度器线程的连接是长生命周期复用的，未关闭的游标会一直
        # 持有 WAL 读锁；该连接随后写库时报 "database is locked"（SQLITE_BUSY_SNAPSHOT，
        # busy_timeout / rollback 均无效，实测 100% 复现）。详见 test_display_summary_* 回归。
        try:
            cur = self._cc(platform).cursor()
            try:
                cur.execute(
                    """SELECT chat_id, summary_text, boundary_msg_id AS older_boundary_msg_id,
                              covered_count, generation, created_at, updated_at, boundary_ts
                       FROM conversation_display_summaries WHERE chat_id = ?""",
                    (str(chat_id),),
                )
                row = cur.fetchone()
            finally:
                cur.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("[resilience] 读取展示摘要缓存失败 chat_id=%s: %s", chat_id, e)
            return None
        if row is None:
            return None
        return ConversationSummaryRow(
            chat_id=row["chat_id"],
            summary_text=row["summary_text"] or "",
            older_boundary_msg_id=row["older_boundary_msg_id"] or "",
            covered_count=int(row["covered_count"] or 0),
            generation=int(row["generation"] or 0),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            boundary_ts=row["boundary_ts"] or "",
        )

    def list_recent_display_summaries(self, limit: int = 30, platform: str = "",
                                      since: str | None = None) -> list[dict]:
        """近期展示用全量摘要列表（JOIN conversations 取 chat_name）。

        窗口过滤命中的是**会话最新活动时间**（boundary_ts = 该会话最近一条消息时间），
        而非摘要生成时间（updated_at）——后者会因展示调度器批量刷新而全部变成当天，
        导致「今日/昨日」筛选器形同虚设。boundary_ts 为空时回退到 updated_at 以兼容旧行。
        """
        cur = self._cc(platform).cursor()
        sql = """SELECT s.chat_id, s.summary_text, s.covered_count, s.updated_at,
                        s.boundary_ts,
                        COALESCE(c.chat_name, '') AS chat_name
                 FROM conversation_display_summaries s
                 LEFT JOIN conversations c ON c.chat_id = s.chat_id"""
        params: list = []
        if since:
            sql += " WHERE COALESCE(s.boundary_ts, s.updated_at) >= ?"
            params.append(since)
        sql += " ORDER BY COALESCE(s.boundary_ts, s.updated_at) DESC LIMIT ?"
        params.append(int(limit))
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "chat_id": r["chat_id"],
                "summary_text": r["summary_text"] or "",
                "covered_count": int(r["covered_count"] or 0),
                "updated_at": r["updated_at"] or "",
                "boundary_ts": r["boundary_ts"] or "",
                "chat_name": r["chat_name"] or "",
            }
            for r in rows
        ]

    def upsert_display_summary(
        self,
        chat_id: str,
        summary: str,
        boundary_msg_id: str,
        covered_count: int,
        expected_generation: int = 0,
        platform: str = "",
        boundary_ts: str = "",
    ) -> bool:
        """CAS 写回展示用全量摘要（state machine 边界，与 H2-A 摘要互不干扰）。

        ``boundary_ts`` 记为该会话最近一条被覆盖消息的时间（会话最新活动时间），
        供 Web「对话摘要」页的「今日/昨日」窗口过滤，避免被 updated_at（摘要生成时间）污染。
        """
        if not chat_id or not summary:
            return False
        # 【P0 2026-10-05】写入走 _write_result（进程内写闸门 + 锁失败自愈重试）。
        # 本方法是展示摘要调度器的写回入口，是 2026-10-05 锁争用事故的主角路径。
        now = datetime.now().isoformat()
        new_gen = expected_generation + 1

        def _do_write(conn: sqlite3.Connection) -> bool:
            cur = conn.cursor()
            try:
                cur.execute(
                    """UPDATE conversation_display_summaries
                       SET summary_text = ?, boundary_msg_id = ?, covered_count = ?,
                           generation = ?, updated_at = ?, boundary_ts = ?
                       WHERE chat_id = ? AND generation = ?""",
                    (summary, boundary_msg_id, int(covered_count), new_gen, now,
                     str(boundary_ts), str(chat_id), int(expected_generation)),
                )
                if cur.rowcount > 0:
                    return True
                cur.execute(
                    "SELECT 1 FROM conversation_display_summaries WHERE chat_id = ?",
                    (str(chat_id),),
                )
                if cur.fetchone() is None:
                    cur.execute(
                        """INSERT INTO conversation_display_summaries
                               (chat_id, summary_text, boundary_msg_id, covered_count,
                                generation, created_at, updated_at, boundary_ts)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(chat_id), summary, boundary_msg_id, int(covered_count),
                         new_gen, now, now, str(boundary_ts)),
                    )
                    return True
                return False
            finally:
                cur.close()
                # 【锁争用】无论走哪条返回路径都要 commit 并 close：
                # 游标悬在结果集上会残留 WAL 读事务，让本连接此后写库立即失败。
                conn.commit()

        return self._write_result(_do_write, platform)
    def get_chat_type(self, chat_id: str = "", platform: str = "") -> str:
        """查询会话类型（single/group/...），供范围分类使用；查不到返回空串。"""
        if not chat_id:
            return ""
        try:
            cur = self._cc(platform).cursor()
            cur.execute("SELECT chat_type FROM conversations WHERE chat_id = ? LIMIT 1", (chat_id,))
            row = cur.fetchone()
            return row["chat_type"] if row else ""
        except Exception as e:
            logger.debug("get_chat_type 失败: %s", e)
            return ""
