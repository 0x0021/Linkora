"""答复门禁规则仓库。

从 src.memory.sqlite_store 拆出——gate_rules 表的 CRUD 与统计。
委托模式：外部通过 SQLiteStore 调用，内部由 GateRuleRepo 持有 conn。

与 KeywordRuleRepo 同构，但字段针对「答复门禁」语义：
- category / category_label：规则分类（私人隐私 / 违法信息 / 负面情绪 / 其他可扩展）
- match_type：keyword（子串）| regex（正则）
- pattern：命中模式（keyword 逗号分隔多词 / regex 表达式）
- intercept_message：命中后给用户的拦截提示
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

logger = logging.getLogger(__name__)


class GateRuleRepo:
    """答复门禁规则仓库。

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
        category: str,
        category_label: str,
        name: str,
        match_type: str,
        pattern: str,
        intercept_message: str,
        priority: int = 0,
        enabled: int = 1,
    ) -> int:
        """新增门禁规则，返回新行 id。"""
        cur = self.conn.cursor()
        now = datetime.now().isoformat()
        cur.execute(
            """INSERT INTO gate_rules
               (category, category_label, name, match_type, pattern, intercept_message,
                priority, enabled, hit_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (category, category_label, name, match_type, pattern, intercept_message,
             priority, enabled, now, now),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def list(
        self,
        category: str = "",
        enabled: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        cur = self.conn.cursor()
        query = "SELECT * FROM gate_rules WHERE 1=1"
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
        cur.execute("SELECT * FROM gate_rules WHERE id = ?", (rule_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def update(self, rule_id: int, **kwargs) -> None:
        if not kwargs:
            return
        allowed_fields = {
            "category", "category_label", "name", "match_type", "pattern",
            "intercept_message", "priority", "enabled", "hit_count",
            "created_at", "updated_at",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not filtered:
            return
        filtered["updated_at"] = datetime.now().isoformat()
        cur = self.conn.cursor()
        fields = ", ".join(f"{k} = ?" for k in filtered.keys())
        values = list(filtered.values()) + [rule_id]
        cur.execute(f"UPDATE gate_rules SET {fields} WHERE id = ?", values)
        self.conn.commit()

    def delete(self, rule_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM gate_rules WHERE id = ?", (rule_id,))
        self.conn.commit()

    def categories(self) -> list[tuple[str, str]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT DISTINCT category, category_label FROM gate_rules "
            "ORDER BY category"
        )
        # 返回 [(category, category_label), ...]，便于前端展示分类名
        return [(row["category"], row["category_label"]) for row in cur.fetchall()]

    def increment_hit(self, rule_id: int) -> None:
        cur = self.conn.cursor()
        cur.execute("UPDATE gate_rules SET hit_count = hit_count + 1 WHERE id = ?", (rule_id,))
        self.conn.commit()

    def count(self, enabled: int | None = None) -> int:
        """门禁规则条数；enabled 传 1/0 时只统计对应启用状态，None 表示全部。"""
        cur = self.conn.cursor()
        if enabled is None:
            cur.execute("SELECT COUNT(*) FROM gate_rules")
        else:
            cur.execute("SELECT COUNT(*) FROM gate_rules WHERE enabled = ?", (enabled,))
        return cur.fetchone()[0]

    def stats(self, top_hits_limit: int = 50) -> dict:
        """门禁规则概览：总数 / 启用数 / 按分类分布 / 命中数 TOP N。"""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM gate_rules")
        total = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as enabled FROM gate_rules WHERE enabled = 1")
        enabled = cur.fetchone()["enabled"]
        cur.execute(
            "SELECT category, category_label, COUNT(*) as cnt FROM gate_rules "
            "GROUP BY category ORDER BY cnt DESC"
        )
        by_category = [dict(row) for row in cur.fetchall()]
        cur.execute(
            "SELECT id, name, category, hit_count FROM gate_rules "
            "ORDER BY hit_count DESC LIMIT ?",
            (top_hits_limit,),
        )
        top_hits = [dict(row) for row in cur.fetchall()]
        return {
            "total": total,
            "enabled": enabled,
            "by_category": by_category,
            "top_hits": top_hits,
        }
