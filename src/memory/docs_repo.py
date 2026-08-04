"""Repository for DingTalk Docs operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DocsRepo:
    """Repository extracted from SQLiteStore for DingTalk Documents operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def upsert_dingtalk_doc(self, doc_id: str, title: str, doc_type: str = "",
                            space_id: str = "", parent_id: str = "",
                            url: str = "", content: str = "",
                            last_modified: str = "") -> None:
        cur = self.store.conn.cursor()
        now = datetime.now().isoformat()
        cur.execute("SELECT id FROM dingtalk_docs WHERE doc_id = ?", (doc_id,))
        exists = cur.fetchone()
        if exists:
            cur.execute(
                """UPDATE dingtalk_docs SET title=?, doc_type=?, space_id=?, parent_id=?,
                   url=?, content=?, last_modified=?, synced_at=? WHERE doc_id=?""",
                (title, doc_type, space_id, parent_id, url, content, last_modified, now, doc_id),
            )
        else:
            cur.execute(
                """INSERT INTO dingtalk_docs
                   (doc_id, title, doc_type, space_id, parent_id, url, content, last_modified, synced_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (doc_id, title, doc_type, space_id, parent_id, url, content, last_modified, now, now),
            )
        self.store.conn.commit()

    def list_dingtalk_docs(self, keyword: str = "", limit: int = 100) -> list[dict]:
        cur = self.store.conn.cursor()
        if keyword:
            cur.execute(
                "SELECT * FROM dingtalk_docs WHERE title LIKE ? ORDER BY synced_at DESC LIMIT ?",
                (f"%{keyword}%", limit),
            )
        else:
            cur.execute("SELECT * FROM dingtalk_docs ORDER BY synced_at DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]

    def count_dingtalk_docs(self) -> int:
        """已同步的钉钉文档数（供状态面板概览）。"""
        cur = self.store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dingtalk_docs")
        return cur.fetchone()[0]

    def get_dingtalk_doc(self, doc_id: str) -> dict | None:
        cur = self.store.conn.cursor()
        cur.execute("SELECT * FROM dingtalk_docs WHERE doc_id = ?", (doc_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def delete_dingtalk_doc(self, doc_id: str) -> None:
        cur = self.store.conn.cursor()
        cur.execute("DELETE FROM dingtalk_docs WHERE doc_id = ?", (doc_id,))
        self.store.conn.commit()

    def update_dingtalk_doc(self, doc_id: str, **kwargs) -> None:
        """更新钉钉文档字段（title, content 等）。"""
        if not kwargs:
            return
        # 字段白名单过滤，防止 SQL 注入（kwargs.keys() 不可直接拼接到 SQL）
        allowed_fields = {
            "title", "doc_type", "space_id", "parent_id", "url", "content",
            "last_modified", "synced_at", "auto_sync", "created_at",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not filtered:
            return
        filtered["synced_at"] = datetime.now().isoformat()
        cur = self.store.conn.cursor()
        fields = ", ".join(f"{k} = ?" for k in filtered.keys())
        values = list(filtered.values()) + [doc_id]
        cur.execute(f"UPDATE dingtalk_docs SET {fields} WHERE doc_id = ?", values)
        self.store.conn.commit()

    def set_doc_auto_sync(self, doc_id: str, auto_sync: bool) -> bool:
        """设置钉钉文档的自动同步开关。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "UPDATE dingtalk_docs SET auto_sync = ? WHERE doc_id = ?",
            (1 if auto_sync else 0, doc_id),
        )
        self.store.conn.commit()
        return cur.rowcount > 0
