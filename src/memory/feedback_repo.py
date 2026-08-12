"""Repository for reply feedback operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class FeedbackRepo:
    """Repository extracted from SQLiteStore for feedback operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def save_feedback(self, message_id: str = "", conversation_id: str = "",
                      sender_id: str = "", rating: int = 0,
                      correction: str = "", note: str = "") -> int:
        cur = self.store.conn.cursor()
        cur.execute(
            """INSERT INTO feedback (message_id, conversation_id, sender_id, rating, correction, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message_id, conversation_id, sender_id, rating, correction or "", note or "", datetime.now().isoformat()),
        )
        self.store.conn.commit()
        # 插入后 lastrowid 必然存在（自增主键），None 实际不可能。
        assert cur.lastrowid is not None
        return cur.lastrowid


    def get_feedback(self, limit: int = 200) -> list[dict]:
        cur = self.store.conn.cursor()
        cur.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    def get_useful_rate(self) -> dict:
        """用户反馈有用率：rating > 0 的条数占全部反馈的比例。

        供成本/质量看板使用。读取失败（如表缺失）时返回全 0 结构，调用方无需兜底。
        """
        try:
            cur = self.store.conn.cursor()
            cur.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END) AS useful "
                "FROM feedback"
            )
            r = cur.fetchone()
            total = r["total"] or 0
            useful = r["useful"] or 0
            return {
                "total": total,
                "useful_count": useful,
                "useful_rate": round(useful / total, 4) if total else 0.0,
            }
        except Exception as e:
            logger.debug("[resilience] 反馈有用率读取失败: %s", e)
            return {"total": 0, "useful_count": 0, "useful_rate": 0.0}

