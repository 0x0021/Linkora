"""关键词规则仓库。

从 src.memory.sqlite_store 拆出——keyword_rules 表的 CRUD 与统计。
委托模式：外部通过 SQLiteStore 调用，内部由 KeywordRuleRepo 持有 conn。
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class KeywordRuleRepo:
    """关键词规则仓库。

    不直接持有 SQLite 连接对象——连接必须按线程隔离（SQLite 默认
    ``check_same_thread=True``），否则在 Web 请求线程里复用主线程创建的连接会
    触发 ``SQLite objects created in a thread can only be used in that same thread``。
    因此只保存 store 引用，每次访问通过 ``self.conn`` 属性取【当前线程】的连接。
    """

    def __init__(self, store):
        self._store = store

    @property
    def conn(self) -> "sqlite3.Connection":
        """返回当前线程独立的 SQLite 连接（委托 store.conn）。"""
        return self._store.conn

    def add(
        self,
        match_pattern: str,
        reply_text: str,
        category: str = "default",
        match_type: str = "fuzzy",
        priority: int = 0,
        enabled: int = 1,
    ) -> int:
        """新增关键词规则，返回新行 id。"""
        cur = self.conn.cursor()
        now = datetime.now().isoformat()
        cur.execute(
            """INSERT INTO keyword_rules
               (category, match_pattern, reply_text, match_type, priority, enabled, hit_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (category, match_pattern, reply_text, match_type, priority, enabled, now, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def list(
        self,
        category: str = "",
        enabled: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        cur = self.conn.cursor()
        query = "SELECT * FROM keyword_rules WHERE 1=1"
        params = []
        if category:
            query += " AND category = ?"
            params.append(category)
        if enabled is not None:
            query += " AND enabled = ?"
            params.append(enabled)
        query += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(limit)
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]

    def get(self, rule_id: int) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM keyword_rules WHERE id = ?", (rule_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update(self, rule_id: int, **kwargs) -> None:
        if not kwargs:
            return
        allowed_fields = {
            "category", "match_pattern", "reply_text", "match_type",
            "priority", "enabled", "hit_count", "created_at", "updated_at",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not filtered:
            return
        filtered["updated_at"] = datetime.now().isoformat()
        cur = self.conn.cursor()
        fields = ", ".join(f"{k} = ?" for k in filtered.keys())
        values = list(filtered.values()) + [rule_id]
        cur.execute(f"UPDATE keyword_rules SET {fields} WHERE id = ?", values)
        self.conn.commit()

    def delete(self, rule_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM keyword_rules WHERE id = ?", (rule_id,))
        self.conn.commit()

    def categories(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT category FROM keyword_rules ORDER BY category")
        return [row["category"] for row in cur.fetchall()]

    def increment_hit(self, rule_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE keyword_rules SET hit_count = hit_count + 1 WHERE id = ?", (rule_id,))
        self.conn.commit()

    def count(self, enabled: int | None = None) -> int:
        """关键词规则条数；enabled 传 1/0 时只统计对应启用状态，None 表示全部。"""
        cur = self.conn.cursor()
        if enabled is None:
            cur.execute("SELECT COUNT(*) FROM keyword_rules")
        else:
            cur.execute("SELECT COUNT(*) FROM keyword_rules WHERE enabled = ?", (enabled,))
        return cur.fetchone()[0]

    def stats(self, top_hits_limit: int = 50) -> dict:
        """关键词规则概览：总数 / 启用数 / 按分类与匹配类型的分布 / 命中数 TOP N。"""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM keyword_rules")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as enabled FROM keyword_rules WHERE enabled = 1")
        enabled = cur.fetchone()["enabled"]
        cur.execute(
            "SELECT category, COUNT(*) as cnt FROM keyword_rules "
            "GROUP BY category ORDER BY cnt DESC"
        )
        by_category = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT match_type, COUNT(*) as cnt FROM keyword_rules "
            "GROUP BY match_type ORDER BY cnt DESC"
        )
        by_type = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT id, match_pattern, hit_count FROM keyword_rules "
            "ORDER BY hit_count DESC LIMIT ?",
            (top_hits_limit,),
        )
        top_hits = [dict(row) for row in cur.fetchall()]
        return {
            "total": total,
            "enabled": enabled,
            "by_category": by_category,
            "by_type": by_type,
            "top_hits": top_hits,
        }
