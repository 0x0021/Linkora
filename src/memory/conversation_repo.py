"""Repository for conversation repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter. 会话相关 6 张表
（conversations / conversation_summaries / messages / external_friends /
blocked_conversations / dedup_messages）物理上位于 per-account 会话库，通过
``self.store.conv_conn(platform)`` 访问，与主库（平台无关表）隔离，实现「重登录换
账号后旧 chat_id/open_id 不再串台」。每个公开方法新增 ``platform`` 参数，由调用方
（poller/agent/runtime，均已知 platform_id）透传。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from src.memory.sqlite_store import ConversationSummaryRow
from src.memory.platform_context import get_current_platform
from src.memory.image_cleanup import purge_orphan_images
from src.models import Message

logger = logging.getLogger(__name__)


class ConversationRepo:
    """Repository extracted from SQLiteStore for conversation operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _cc(self, platform: str) -> sqlite3.Connection:
        plat = platform or get_current_platform()
        return self.store.conv_conn(plat)

    def get_last_reply_time(self, chat_id: str, platform: str = "") -> Optional[str]:
        """获取某个会话最后回复时间（ISO 格式），用于回复冷却。"""
        cur = self._cc(platform).cursor()
        cur.execute("SELECT last_reply_time FROM conversations WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row else None

    def get_latest_user_message_time(self, chat_id: str, exclude_msg_id: str = None,
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
        cur = self._cc(platform).cursor()
        cur.execute(
            "UPDATE conversations SET last_reply_time = ?, updated_at = ? WHERE chat_id = ?",
            (now, now, chat_id)
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT OR IGNORE INTO conversations (chat_id, chat_type, last_reply_time, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, chat_type, now, now, now)
            )
        self._cc(platform).commit()

    def get_last_replied_msg_id(self, chat_id: str, platform: str = "") -> Optional[str]:
        """获取会话最后回复过的用户消息 msg_id（用于基于消息 ID 的防重复回复）。"""
        cur = self._cc(platform).cursor()
        cur.execute("SELECT last_replied_msg_id FROM conversations WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    def update_last_replied_msg_id(self, chat_id: str, msg_id: str,
                                   chat_type: str = "unknown", platform: str = "") -> None:
        """记录会话最后回复过的用户消息 msg_id。"""
        now = datetime.now().isoformat()
        cur = self._cc(platform).cursor()
        cur.execute(
            "UPDATE conversations SET last_replied_msg_id = ?, updated_at = ? WHERE chat_id = ?",
            (msg_id, now, chat_id),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT OR IGNORE INTO conversations (chat_id, chat_type, last_replied_msg_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, chat_type, msg_id, now, now)
            )
        self._cc(platform).commit()

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
        cur = self._cc(platform).cursor()
        now = datetime.now().isoformat()
        # last_message_time 优先用调用方传入的真实最后消息时间（如轮询器从消息时间戳推算），
        # 缺省回落到当前时间，保持历史行为一致。
        lmt = last_message_time or now
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
            (chat_id, chat_name or "", chat_type, peer_user_id, peer_open_dingtalk_id, lmt, now, now),
        )
        self._cc(platform).commit()

    def delete_conversation(self, chat_id: str, platform: str = "") -> None:
        """删除会话（遇权限错误时调用，避免反复重试）。"""
        cur = self._cc(platform).cursor()
        cur.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
        self._cc(platform).commit()
        logger.info("[SQLiteStore] 已删除会话 %s", chat_id)

    def delete_conversations(self, chat_ids: list[str], platform: str = "") -> int:
        """批量删除会话（含其消息 / 摘要 / 去重记录）。返回实际删除的会话数。

        用于 Web「批量删除消息记录」：按 chat_id 清掉 messages /
        conversation_summaries / dedup_messages，再删 conversations 行本身，
        避免留下「空会话占位」或孤儿摘要/去重记录。单库单事务，原子提交。
        """
        chat_ids = [str(c) for c in (chat_ids or []) if str(c).strip()]
        if not chat_ids:
            return 0
        conn = self._cc(platform)
        cur = conn.cursor()
        placeholders = ",".join("?" * len(chat_ids))
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
        cur.execute(f"DELETE FROM dedup_messages WHERE chat_id IN ({placeholders})", chat_ids)
        cur.execute(f"DELETE FROM conversations WHERE chat_id IN ({placeholders})", chat_ids)
        deleted = cur.rowcount
        conn.commit()
        removed_files = purge_orphan_images(self.store.db_path, image_paths)
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
               ORDER BY c.updated_at DESC LIMIT ?""",
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
        cur = self._cc(platform).cursor()
        now = datetime.now().isoformat()
        total_updated = 0
        for chat_id, chat_type in updates:
            cur.execute(
                "UPDATE conversations SET chat_type = ?, updated_at = ? WHERE chat_id = ? AND chat_type != ?",
                (chat_type, now, chat_id, chat_type),
            )
            total_updated += cur.rowcount
        self._cc(platform).commit()
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
        )

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
        cur = self._cc(platform).cursor()
        now = datetime.now().isoformat()
        new_gen = expected_generation + 1
        cur.execute(
            """UPDATE conversation_summaries
               SET summary_text = ?, older_boundary_msg_id = ?, covered_count = ?,
                   generation = ?, updated_at = ?
               WHERE chat_id = ? AND generation = ?""",
            (summary, older_boundary_msg_id, int(covered_count), new_gen, now,
             str(chat_id), int(expected_generation)),
        )
        if cur.rowcount > 0:
            self._cc(platform).commit()
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
            self._cc(platform).commit()
            return True
        logger.debug(
            "[摘要] CAS 跳过写回 chat_id=%s（代际不符：期望 %d，库已被更新）",
            chat_id, expected_generation,
        )
        self._cc(platform).commit()
        return False

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
