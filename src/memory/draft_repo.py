"""Repository for draft repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class DraftRepo:
    """Repository extracted from SQLiteStore for draft operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def add_dead_letter(self, *, msg_id: str | None, chat_id: str, chat_name: str,
                        sender_id: str | None, sender_name: str | None,
                        content: str, msg_type: str, stage: str, error: str,
                        raw: dict | None) -> int:
        """将一条彻底失败的消息写入死信队列，返回新行 id。

        行幂等：同一 (msg_id, stage, status='pending') 已存在则不再重复插入，
        避免重试风暴下反复落库刷屏。
        """
        now = datetime.now().isoformat()
        raw_json = json.dumps(raw, ensure_ascii=False) if raw else None
        cur = self.store.conn.cursor()
        # 幂等：若同一原始消息同阶段已有 pending，直接复用
        cur.execute(
            "SELECT id FROM dead_letter_messages WHERE msg_id = ? AND stage = ? AND status = 'pending'",
            (msg_id, stage),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE dead_letter_messages SET error = ?, updated_at = ? WHERE id = ?",
                (error, now, existing[0]),
            )
            self.store.conn.commit()
            return int(existing[0])
        cur.execute(
            """INSERT INTO dead_letter_messages
               (msg_id, chat_id, chat_name, sender_id, sender_name, content,
                msg_type, stage, error, raw, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (msg_id, chat_id, chat_name, sender_id, sender_name, content,
             msg_type, stage, error, raw_json, now, now),
        )
        self.store.conn.commit()
        return int(cur.lastrowid)

    def list_dead_letters(self, status: str = "pending", limit: int = 100,
                             offset: int = 0) -> tuple[list[dict], int]:
        """列出死信消息（默认仅 pending，status='all' 返回全部）。

        Returns:
            (items, total) —— items 为当前页数据，total 为该状态过滤下的总数
            （用于前端翻页，total 不受 limit/offset 影响）。
        """
        cur = self.store.conn.cursor()
        cols = ["id", "msg_id", "chat_id", "chat_name", "sender_id", "sender_name",
                "content", "msg_type", "stage", "error", "status", "created_at",
                "replayed_at", "replay_note", "updated_at"]
        if status == "all":
            cur.execute(
                """SELECT id, msg_id, chat_id, chat_name, sender_id, sender_name,
                          content, msg_type, stage, error, status, created_at,
                          replayed_at, replay_note, updated_at
                   FROM dead_letter_messages
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        else:
            cur.execute(
                """SELECT id, msg_id, chat_id, chat_name, sender_id, sender_name,
                          content, msg_type, stage, error, status, created_at,
                          replayed_at, replay_note, updated_at
                   FROM dead_letter_messages
                   WHERE status = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (status, limit, offset),
            )
        items = [dict(zip(cols, row)) for row in cur.fetchall()]
        # 先取完数据行，再执行 COUNT（避免复用同一 cursor 导致结果被覆盖）
        if status == "all":
            cur.execute("SELECT COUNT(*) FROM dead_letter_messages")
        else:
            cur.execute(
                "SELECT COUNT(*) FROM dead_letter_messages WHERE status = ?",
                (status,),
            )
        total = cur.fetchone()[0]
        return items, total

    #: 死信队列 CSV 导出列（顺序即导出表头顺序，调用方直接复用以保持契约稳定）
    DEAD_LETTER_EXPORT_COLUMNS: tuple[str, ...] = (
        "id", "msg_id", "chat_id", "chat_name", "sender_id", "sender_name",
        "content", "msg_type", "stage", "error", "status",
        "created_at", "updated_at", "replayed_at", "replay_note",
    )

    def export_dead_letters(self, status: str = "all", limit: int = 10000) -> list[dict]:
        """导出死信消息：status 为空或 'all' 时不过滤，否则按状态过滤。

        created_at 倒序，最多 limit 条；每行是以 ``DEAD_LETTER_EXPORT_COLUMNS``
        为键的 dict。
        """
        cur = self.store.conn.cursor()
        cols = ", ".join(self.DEAD_LETTER_EXPORT_COLUMNS)
        if status and status != "all":
            cur.execute(
                f"SELECT {cols} FROM dead_letter_messages "
                "WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur.execute(
                f"SELECT {cols} FROM dead_letter_messages "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(zip(self.DEAD_LETTER_EXPORT_COLUMNS, row)) for row in cur.fetchall()]

    def count_dead_letters(self, status: str = "") -> int:
        """死信条数；status 为空或 'all' 时统计全部，否则只统计该状态。"""
        cur = self.store.conn.cursor()
        if status and status != "all":
            cur.execute(
                "SELECT COUNT(*) FROM dead_letter_messages WHERE status = ?",
                (status,),
            )
        else:
            cur.execute("SELECT COUNT(*) FROM dead_letter_messages")
        return cur.fetchone()[0]

    def get_dead_letter(self, dl_id: int) -> dict | None:
        cur = self.store.conn.cursor()
        # 【bug fix】必须 SELECT replay_note，否则 resolve_dead_letter 写入的 note 读不出。
        cur.execute(
            """SELECT id, msg_id, chat_id, chat_name, sender_id, sender_name,
                      content, msg_type, stage, error, status, raw,
                      created_at, updated_at, replayed_at, replay_note
               FROM dead_letter_messages WHERE id = ?""",
            (dl_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        cols = ["id", "msg_id", "chat_id", "chat_name", "sender_id", "sender_name",
                "content", "msg_type", "stage", "error", "status", "raw",
                "created_at", "updated_at", "replayed_at", "replay_note"]
        return dict(zip(cols, row))

    def resolve_dead_letter(self, dl_id: int, *, status: str, note: str = "") -> bool:
        """将死信标记为 replayed / discarded（重放或丢弃）。"""
        now = datetime.now().isoformat()
        cur = self.store.conn.cursor()
        if status == "replayed":
            cur.execute(
                "UPDATE dead_letter_messages SET status = 'replayed', replayed_at = ?, replay_note = ?, updated_at = ? WHERE id = ?",
                (now, note, now, dl_id),
            )
        else:
            cur.execute(
                "UPDATE dead_letter_messages SET status = ?, replay_note = ?, updated_at = ? WHERE id = ?",
                (status, note, now, dl_id),
            )
        self.store.conn.commit()
        return cur.rowcount > 0

    # ============ 草稿管理（低置信度消息审签）============

    def add_draft(self, platform: str, chat_id: str, chat_name: str,
                  chat_type: str, sender_id: str, sender_name: str,
                  user_message: str, ai_reply: str,
                  rag_confidence: float | None = None,
                  rag_threshold: float | None = None,
                  rag_best_chunk: str | None = None) -> str:
        """将低置信度草稿写入 message_drafts 表，返回 draft_id（UUID）。"""
        draft_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        cur = self.store.conn.cursor()
        cur.execute(
            """INSERT INTO message_drafts
               (draft_id, platform, chat_id, chat_name, chat_type,
                sender_id, sender_name, user_message, ai_reply,
                rag_confidence, rag_threshold, rag_best_chunk,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (draft_id, platform, chat_id, chat_name, chat_type,
             sender_id, sender_name, user_message, ai_reply,
             rag_confidence, rag_threshold, rag_best_chunk, now),
        )
        self.store.conn.commit()
        logger.info("[草稿] 已落库 draft_id=%s chat=%s", draft_id, chat_id)
        return draft_id

    def list_drafts(self, status: str | None = None,
                    platform: str | None = None,
                    limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """列出草稿，按 created_at DESC 排序。

        Returns:
            (items, total) —— items 为当前页数据，total 为该过滤条件下的总数
            （用于前端翻页，total 不受 limit/offset 影响）。
        """
        cur = self.store.conn.cursor()
        sql = "SELECT * FROM message_drafts WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur.execute(sql, params)
        items = [dict(row) for row in cur.fetchall()]
        # 先取完数据行，再执行 COUNT（避免复用同一 cursor 导致结果被覆盖）
        count_sql = "SELECT COUNT(*) FROM message_drafts WHERE 1=1"
        count_params: list = []
        if status:
            count_sql += " AND status = ?"
            count_params.append(status)
        if platform:
            count_sql += " AND platform = ?"
            count_params.append(platform)
        cur.execute(count_sql, count_params)
        total = cur.fetchone()[0]
        return items, total

    def get_draft(self, draft_id: str) -> dict | None:
        """按 draft_id 获取单条草稿。"""
        cur = self.store.conn.cursor()
        cur.execute("SELECT * FROM message_drafts WHERE draft_id = ?", (draft_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def resolve_draft(self, draft_id: str, action: str,
                      final_reply: str | None = None,
                      notes: str = "") -> bool:
        """处理草稿：approved / discarded / edited。更新 status、processed_at、final_reply。"""
        now = datetime.now().isoformat()
        cur = self.store.conn.cursor()
        status_map = {"approved": "approved", "discarded": "discarded", "edited": "edited"}
        new_status = status_map.get(action)
        if not new_status:
            logger.warning("[草稿] 未知处理动作: %s", action)
            return False
        cur.execute(
            """UPDATE message_drafts
               SET status = ?, processed_at = ?, final_reply = ?, notes = ?
               WHERE draft_id = ?""",
            (new_status, now, final_reply or "", notes, draft_id),
        )
        self.store.conn.commit()
        return cur.rowcount > 0

    def count_pending_drafts(self, platform: str | None = None) -> int:
        """返回待处理草稿数量。"""
        cur = self.store.conn.cursor()
        if platform:
            cur.execute(
                "SELECT COUNT(*) FROM message_drafts WHERE status = 'pending' AND platform = ?",
                (platform,),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) FROM message_drafts WHERE status = 'pending'"
            )
        row = cur.fetchone()
        return row[0] if row else 0

