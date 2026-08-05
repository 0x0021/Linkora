"""Repository for external friend repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self._cc() for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from src.memory.platform_context import get_current_platform

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class ExternalFriendRepo:
    """Repository extracted from SQLiteStore for external friend operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _cc(self) -> sqlite3.Connection:
        """按当前平台/账号隔离的会话连接（external_friends 属会话数据）。"""
        return self.store.conv_conn(get_current_platform())

    def add_external_friend(self, name: str, open_dingtalk_id: str,
                            chat_id: str = "", notes: str = "") -> dict:
        """添加外部好友映射。"""
        cur = self._cc().cursor()
        now = datetime.now().isoformat()
        try:
            cur.execute(
                """INSERT INTO external_friends (name, open_dingtalk_id, chat_id, notes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, open_dingtalk_id, chat_id, notes, now, now),
            )
        except sqlite3.IntegrityError:
            # 已存在则更新
            cur.execute(
                """UPDATE external_friends SET name=?, chat_id=?, notes=?, updated_at=?
                   WHERE open_dingtalk_id=?""",
                (name, chat_id, notes, now, open_dingtalk_id),
            )
        self._cc().commit()
        return self.get_external_friend_by_id(open_dingtalk_id)

    def get_external_friend_by_name(self, name: str) -> Optional[dict]:
        """按姓名查找外部好友。"""
        cur = self._cc().cursor()
        cur.execute("SELECT * FROM external_friends WHERE name = ?", (name,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_external_friend_by_id(self, open_dingtalk_id: str) -> Optional[dict]:
        """按 openDingTalkId 查找外部好友。"""
        cur = self._cc().cursor()
        cur.execute("SELECT * FROM external_friends WHERE open_dingtalk_id = ?", (open_dingtalk_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_external_friends(self) -> list[dict]:
        """列出所有外部好友。"""
        cur = self._cc().cursor()
        cur.execute("SELECT * FROM external_friends ORDER BY created_at DESC")
        return [dict(row) for row in cur.fetchall()]

    def delete_external_friend(self, open_dingtalk_id: str) -> bool:
        """删除外部好友。"""
        cur = self._cc().cursor()
        cur.execute("DELETE FROM external_friends WHERE open_dingtalk_id = ?", (open_dingtalk_id,))
        self._cc().commit()
        return cur.rowcount > 0

