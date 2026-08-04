"""Repository for few-shot example management — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class FewShotRepo:
    """Repository extracted from SQLiteStore for few-shot example operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def get_few_shot_examples(self) -> list[dict]:
        """读取本人语气 few-shot 样例（平台级隔离，默认空列表）。"""
        try:
            cur = self.store.conn.cursor()
            cur.execute("SELECT value FROM kv WHERE key = 'few_shot_examples'")
            row = cur.fetchone()
            if row and row["value"]:
                data = json.loads(row["value"])
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, dict)]
        except Exception:
            logger.warning("[resilience] silent exception in get_few_shot_examples", exc_info=True)
        return []


    def set_few_shot_examples(self, examples: list[dict]) -> None:
        """整体写入本人语气 few-shot 样例（platform 级覆盖全局 config）。"""
        cleaned = []
        for e in (examples or []):
            if not isinstance(e, dict):
                continue
            u = (e.get("user") or "").strip()
            a = (e.get("assistant") or "").strip()
            if u and a:
                cleaned.append({"user": u, "assistant": a})
        cur = self.store.conn.cursor()
        cur.execute(
            """INSERT INTO kv (key, value, updated_at) VALUES ('few_shot_examples', ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (json.dumps(cleaned, ensure_ascii=False), datetime.now().isoformat()),
        )
        self.store.conn.commit()


    def append_few_shot_example(self, example: dict) -> int:
        """增量追加一条 few-shot 样例（按 user+assistant 去重），返回追加后的总数。"""
        u = (example.get("user") or "").strip()
        a = (example.get("assistant") or "").strip()
        if not u or not a:
            return len(self.get_few_shot_examples())
        existing = self.get_few_shot_examples()
        if not any(
            (e.get("user") or "").strip() == u and (e.get("assistant") or "").strip() == a
            for e in existing
        ):
            existing.append({"user": u, "assistant": a})
            self.set_few_shot_examples(existing)
        return len(existing)

