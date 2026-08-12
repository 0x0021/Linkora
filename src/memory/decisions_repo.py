"""Repository for decisions tracking — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class DecisionsRepo:
    """Repository extracted from SQLiteStore for decision tracking operations."""

    DEFAULT_RETENTION_DAYS = 30
    HARD_CAP = 10000  # 硬上限：极端情况下保证表不无限增长

    #: 决策记录 CSV 导出列（顺序即导出表头顺序，调用方直接复用以保持契约稳定）
    EXPORT_COLUMNS: tuple[str, ...] = (
        "id", "sender_id", "sender_name", "conversation_id", "conversation_name",
        "content_preview", "intent", "action", "routing_mode", "routed_tools",
        "skill_name", "skill_source", "reply_preview", "request_id", "platform_id",
        "llm_calls", "fallback_used", "tool_calls", "total_latency_ms",
        "handoff", "rag_grounded", "cited", "created_at",
    )

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store
        self._insert_count: int = 0
        self.retention_days: int = self.DEFAULT_RETENTION_DAYS
        self._hard_cap: int = self.HARD_CAP

    # ── properties that SQLiteStore delegates ──

    @property
    def _decision_insert_count(self) -> int:
        """Backward-compat: exposed so sqlite_store.py __init__ can still init counters."""
        return self._insert_count

    @_decision_insert_count.setter
    def _decision_insert_count(self, val: int) -> None:
        self._insert_count = val

    @property
    def _decisions_retention_days(self) -> int:
        return self.retention_days

    @_decisions_retention_days.setter
    def _decisions_retention_days(self, val: int) -> None:
        self.retention_days = int(val)

    @property
    def _decisions_hard_cap(self) -> int:
        return self._hard_cap

    @_decisions_hard_cap.setter
    def _decisions_hard_cap(self, val: int) -> None:
        self._hard_cap = int(val)

    # ── methods extracted from SQLiteStore ──

    def record_decision(
        self,
        sender_id: str,
        sender_name: str = "",
        conversation_id: str = "",
        conversation_name: str = "",
        content_preview: str = "",
        intent: str = "",
        action: str = "",
        routing_mode: str = "",
        routed_tools: str | list = "",
        skill_name: str = "",
        skill_source: str = "",
        reply_preview: str = "",
        request_id: str = "",
        platform_id: str = "",
        llm_calls: int = 0,
        fallback_used: int = 0,
        tool_calls: int = 0,
        total_latency_ms: int = 0,
        handoff: int = 0,
        rag_grounded: int = 0,
        cited: int = 0,
    ) -> int:
        """写入一条决策记录到持久化表。

        handoff / rag_grounded / cited 为成本/质量看板（Roadmap ③）质量标记：
        - handoff: 本条是否触发低置信转人工（草稿推主人）
        - rag_grounded: RAG 是否命中（best_score 非空）
        - cited: 是否实际追加了引文溯源页脚
        均为可选，默认 0，向后兼容旧调用方。
        """
        import json as _json
        tools_json = _json.dumps(routed_tools) if isinstance(routed_tools, list) else (routed_tools or "")
        cur = self.store.conn.cursor()
        cur.execute(
            """INSERT INTO decisions
               (sender_id, sender_name, conversation_id, conversation_name,
                content_preview, intent, action, routing_mode, routed_tools,
                skill_name, skill_source, reply_preview,
                request_id, platform_id, llm_calls, fallback_used, tool_calls, total_latency_ms,
                handoff, rag_grounded, cited)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sender_id or "", sender_name or "", conversation_id or "", conversation_name or "",
             content_preview or "", intent or "", action or "",
             routing_mode or "", tools_json,
             skill_name or "", skill_source or "", reply_preview or "",
             request_id or "", platform_id or "",
             int(llm_calls or 0), int(fallback_used or 0),
             int(tool_calls or 0), int(total_latency_ms or 0),
             int(handoff or 0), int(rag_grounded or 0), int(cited or 0)),
        )
        self.store.conn.commit()
        self._insert_count += 1
        if self._insert_count % 200 == 0:
            self._prune_decisions()
        # 插入后 lastrowid 必然存在（AUTOINCREMENT 主键），None 实际不可能。
        assert cur.lastrowid is not None
        return cur.lastrowid

    def mark_cited(self, *, request_id: str = "", platform_id: str = "",
                   conversation_id: str = "", cited: int = 0) -> int:
        """回填 cited 标记（引文溯源页脚是否实际追加）。

        用于成本/质量看板（Roadmap ③）。cited 只能在发送分支（拼接引文页脚后）确定，
        而 tracker.record 在更早的 LLM 分支已写入决策行，故此处按 request_id 定位刚写入的
        行并原地 UPDATE；request_id 缺省时回退到 (platform_id, conversation_id) 最新一行。
        返回受影响行数；任何异常静默吞掉，绝不影响正常回复。
        """
        try:
            cur = self.store.conn.cursor()
            if request_id:
                cur.execute(
                    "UPDATE decisions SET cited = ? "
                    "WHERE id = (SELECT id FROM decisions WHERE request_id = ? "
                    "ORDER BY id DESC LIMIT 1)",
                    (int(cited or 0), request_id),
                )
            elif conversation_id:
                cur.execute(
                    "UPDATE decisions SET cited = ? "
                    "WHERE id = (SELECT id FROM decisions "
                    "WHERE platform_id = ? AND conversation_id = ? "
                    "ORDER BY id DESC LIMIT 1)",
                    (int(cited or 0), platform_id or "", conversation_id),
                )
            else:
                return 0
            self.store.conn.commit()
            return cur.rowcount
        except Exception as e:

            logger.warning(f"mark_cited: swallowed exception: {e}")
            logger.debug("[resilience] 回填 cited 标记失败: %s", e)
            try:
                self.store.conn.rollback()
            except Exception:
                pass
            return 0

    def get_quality_stats(self, time_range_hours: int | None = None) -> dict:
        """成本/质量看板（Roadmap ③）：聚合决策表的质量标记。

        返回总处理数、低置信转人工(handoff)/RAG 命中(rag_grounded)/引文页脚(cited)
        的计数与占比（率 = 计数 / 总数）。无数据时返回全 0 结构，前端兜底显示。
        """
        try:
            cur = self.store.conn.cursor()
            time_clause = ""
            params: list = []
            if time_range_hours is not None and time_range_hours > 0:
                cutoff = (datetime.now() - timedelta(hours=time_range_hours)).isoformat()
                time_clause = "WHERE created_at >= ?"
                params = [cutoff]
            cur.execute(
                f"""SELECT
                       COUNT(*) AS total,
                       SUM(CASE WHEN handoff = 1 THEN 1 ELSE 0 END) AS handoff_cnt,
                       SUM(CASE WHEN rag_grounded = 1 THEN 1 ELSE 0 END) AS rag_cnt,
                       SUM(CASE WHEN cited = 1 THEN 1 ELSE 0 END) AS cited_cnt
                   FROM decisions
                   {time_clause}""",
                params,
            )
            r = cur.fetchone()
            total = r["total"] or 0
            handoff = r["handoff_cnt"] or 0
            rag = r["rag_cnt"] or 0
            cited = r["cited_cnt"] or 0
            return {
                "total": total,
                "handoff_count": handoff,
                "rag_grounded_count": rag,
                "cited_count": cited,
                "handoff_rate": round(handoff / total, 4) if total else 0.0,
                "rag_grounded_rate": round(rag / total, 4) if total else 0.0,
                "cited_rate": round(cited / total, 4) if total else 0.0,
            }
        except Exception as e:
            logger.debug("[resilience] 质量统计读取失败: %s", e)
            return {
                "total": 0, "handoff_count": 0, "rag_grounded_count": 0, "cited_count": 0,
                "handoff_rate": 0.0, "rag_grounded_rate": 0.0, "cited_rate": 0.0,
            }

    def set_decisions_retention_days(self, days: int) -> None:
        """设置决策记录留存天数（≤0 表示不按时间清理，仅保留硬上限兜底）。"""
        self.retention_days = int(days)

    def _prune_decisions(self) -> None:
        try:
            cur = self.store.conn.cursor()
            if self.retention_days > 0:
                cur.execute(
                    "DELETE FROM decisions WHERE created_at < datetime('now', ?, 'localtime')",
                    (f"-{self.retention_days} days",),
                )
            cur.execute("SELECT COUNT(*) FROM decisions")
            total = cur.fetchone()[0]
            if total > self._hard_cap:
                excess = total - self._hard_cap
                cur.execute(
                    "DELETE FROM decisions WHERE id IN ("
                    "SELECT id FROM decisions ORDER BY created_at ASC, id ASC LIMIT ?)",
                    (excess,),
                )
            self.store.conn.commit()
        except Exception as e:
            logger.warning("数据库提交失败: %s", e)
            try:
                self.store.conn.rollback()
            except Exception as re:
                logger.error("数据库回滚也失败: %s", re)

    def cleanup_old_records(self, decisions_retention_days: int | None = None) -> dict:
        days = decisions_retention_days if decisions_retention_days is not None else self.retention_days
        result = {"decisions_deleted": 0, "decisions_remaining": 0}
        try:
            cur = self.store.conn.cursor()
            if days > 0:
                cur.execute(
                    "DELETE FROM decisions WHERE created_at < datetime('now', ?, 'localtime')",
                    (f"-{days} days",),
                )
                result["decisions_deleted"] = cur.rowcount
            cur.execute("SELECT COUNT(*) FROM decisions")
            total = cur.fetchone()[0]
            if total > self._hard_cap:
                excess = total - self._hard_cap
                cur.execute(
                    "DELETE FROM decisions WHERE id IN ("
                    "SELECT id FROM decisions ORDER BY created_at ASC, id ASC LIMIT ?)",
                    (excess,),
                )
                result["decisions_deleted"] += cur.rowcount
            cur.execute("SELECT COUNT(*) FROM decisions")
            result["decisions_remaining"] = cur.fetchone()[0]
            self.store.conn.commit()
        except Exception as e:
            logger.warning("数据库提交失败: %s", e)
            try:
                self.store.conn.rollback()
            except Exception as re:
                logger.error("数据库回滚也失败: %s", re)
        return result

    @staticmethod
    def _build_filter_clause(
        sender_name: str | None = None,
        conversation_id: str | None = None,
        intent: str | None = None,
        action: str | None = None,
        time_filter: str | None = None,
    ) -> tuple[str, list]:
        """构造 decisions 表的 WHERE 子句与参数（分页查询与 CSV 导出共用，避免两处过滤条件漂移）。

        time_filter: 'today'（本地日期为今天）| 'month'（本地年月为本月）| 其他/None 不限。
        """
        conditions: list[str] = []
        params: list = []
        if sender_name:
            conditions.append("sender_name = ?")
            params.append(sender_name)
        if conversation_id:
            conditions.append("conversation_id = ?")
            params.append(conversation_id)
        if intent:
            conditions.append("intent = ?")
            params.append(intent)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if time_filter == 'today':
            conditions.append("DATE(created_at) = DATE('now', 'localtime')")
        elif time_filter == 'month':
            conditions.append("strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now', 'localtime')")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        return where, params

    def get_decisions(
        self,
        page: int = 1,
        page_size: int = 20,
        sender_name: str | None = None,
        conversation_id: str | None = None,
        intent: str | None = None,
        action: str | None = None,
        time_filter: str | None = None,
    ) -> dict:
        """分页查询决策记录，支持按 sender_name / conversation_id / intent / action / time_filter 过滤。"""
        import json as _json
        cur = self.store.conn.cursor()
        where, params = self._build_filter_clause(
            sender_name=sender_name,
            conversation_id=conversation_id,
            intent=intent,
            action=action,
            time_filter=time_filter,
        )

        cur.execute(f"SELECT COUNT(*) FROM decisions {where}", params)
        total = cur.fetchone()[0]

        offset = (page - 1) * page_size
        cur.execute(
            f"SELECT * FROM decisions {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        rows = cur.fetchall()
        items = []
        for r in rows:
            tools = r["routed_tools"] or ""
            if tools:
                try:
                    tools = _json.loads(tools)
                except Exception as e:
                    logger.debug("工具列表 JSON 解析失败: %s", e)
                    tools = []
            else:
                tools = []
            items.append({
                "id": r["id"],
                "sender_id": r["sender_id"],
                "sender_name": r["sender_name"],
                "conversation_id": r["conversation_id"],
                "conversation_name": r["conversation_name"],
                "content_preview": r["content_preview"],
                "intent": r["intent"],
                "action": r["action"],
                "routing_mode": r["routing_mode"],
                "routed_tools": tools,
                "skill_name": r["skill_name"] if "skill_name" in r.keys() else "",
                "skill_source": r["skill_source"] if "skill_source" in r.keys() else "",
                "reply_preview": r["reply_preview"],
                "created_at": r["created_at"],
            })
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def query_decisions_by_rid(self, request_id: str = "", limit: int = 50) -> list[dict]:
        """按 request_id 查询决策记录（用于全链路 trace 排查）。"""
        if not request_id:
            return []
        import json as _json
        cur = self.store.conn.cursor()
        cur.execute(
            """SELECT id, sender_id, sender_name, conversation_id, conversation_name,
                      content_preview, intent, action, routing_mode, routed_tools,
                      skill_name, skill_source, reply_preview, request_id,
                      platform_id, llm_calls, fallback_used, tool_calls,
                      total_latency_ms, created_at
               FROM decisions
               WHERE request_id = ?
               ORDER BY id ASC
               LIMIT ?""",
            (request_id, limit),
        )
        rows = cur.fetchall()
        items = []
        for r in rows:
            try:
                tools = _json.loads(r[9]) if r[9] else []
            except Exception as _exc:
                logger.debug(f"query_decisions_by_rid: swallowed exception: {_exc}")
                tools = []
            items.append({
                "id": r[0], "sender_id": r[1], "sender_name": r[2],
                "conversation_id": r[3], "conversation_name": r[4],
                "content_preview": r[5], "intent": r[6], "action": r[7],
                "routing_mode": r[8], "routed_tools": tools,
                "skill_name": r[10], "skill_source": r[11],
                "reply_preview": r[12], "request_id": r[13],
                "platform_id": r[14], "llm_calls": r[15],
                "fallback_used": r[16], "tool_calls": r[17],
                "total_latency_ms": r[18], "created_at": r[19],
            })
        return items

    def get_decisions_stats(self) -> dict:
        """返回各意图/动作的计数统计。"""
        cur = self.store.conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM decisions")
        total = cur.fetchone()["total"]

        cur.execute("SELECT intent, COUNT(*) as cnt FROM decisions GROUP BY intent ORDER BY cnt DESC")
        by_intent = {r["intent"] or "(none)": r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT action, COUNT(*) as cnt FROM decisions GROUP BY action ORDER BY cnt DESC")
        by_action = {r["action"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT sender_name, COUNT(*) as cnt FROM decisions GROUP BY sender_name ORDER BY cnt DESC LIMIT 20")
        by_sender = {r["sender_name"] or "(unknown)": r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT COUNT(*) as cnt FROM decisions WHERE skill_name != ''")
        skill_activated = cur.fetchone()["cnt"]
        cur.execute("SELECT skill_name, COUNT(*) as cnt FROM decisions WHERE skill_name != '' GROUP BY skill_name ORDER BY cnt DESC")
        by_skill = {r["skill_name"]: r["cnt"] for r in cur.fetchall()} if skill_activated > 0 else {}

        return {
            "total": total,
            "by_intent": by_intent,
            "by_action": by_action,
            "by_sender": by_sender,
            "skill_activated": skill_activated,
            "by_skill": by_skill,
        }

    def get_filter_options(self) -> dict:
        """决策历史筛选下拉的可选项：非空的 sender_name / intent / action（各自升序去重）。"""
        cur = self.store.conn.cursor()
        cur.execute("SELECT DISTINCT sender_name FROM decisions WHERE sender_name != '' ORDER BY sender_name")
        senders = [r["sender_name"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT intent FROM decisions WHERE intent != '' ORDER BY intent")
        intents = [r["intent"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT action FROM decisions WHERE action != '' ORDER BY action")
        actions = [r["action"] for r in cur.fetchall()]
        return {"senders": senders, "intents": intents, "actions": actions}

    def export_decisions(
        self,
        sender_name: str | None = None,
        conversation_id: str | None = None,
        intent: str | None = None,
        action: str | None = None,
        time_filter: str | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """按与分页查询相同的过滤条件导出决策记录（created_at 倒序，最多 limit 条）。

        每行是以 ``EXPORT_COLUMNS`` 为键的 dict，列顺序由 ``EXPORT_COLUMNS`` 决定。
        """
        cur = self.store.conn.cursor()
        where, params = self._build_filter_clause(
            sender_name=sender_name,
            conversation_id=conversation_id,
            intent=intent,
            action=action,
            time_filter=time_filter,
        )
        cols = ", ".join(self.EXPORT_COLUMNS)
        cur.execute(
            f"SELECT {cols} FROM decisions {where} ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        )
        return [{k: r[k] for k in self.EXPORT_COLUMNS} for r in cur.fetchall()]

    def get_recent_cited(self, limit: int = 20) -> list[dict]:
        """最近 N 条实际追加了引文页脚（cited=1）的决策，按 id 倒序。

        供成本/质量看板的引文子面板使用；读取失败时返回空列表。
        """
        try:
            cur = self.store.conn.cursor()
            cur.execute(
                "SELECT id, sender_name, conversation_name, intent, reply_preview, created_at "
                "FROM decisions WHERE cited = 1 ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [
                {
                    "id": r["id"],
                    "sender_name": r["sender_name"],
                    "conversation_name": r["conversation_name"],
                    "intent": r["intent"],
                    "reply_preview": r["reply_preview"],
                    "created_at": r["created_at"],
                }
                for r in cur.fetchall()
            ]
        except Exception as e:
            logger.debug("[resilience] 引文决策读取失败: %s", e)
            return []

    def get_daily_handoff_stats(self, day: str) -> dict:
        """指定本地日期（YYYY-MM-DD）当天的决策总数与转人工（handoff=1）数。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN handoff = 1 THEN 1 ELSE 0 END) AS handoff_cnt "
            "FROM decisions WHERE DATE(created_at) = ?",
            (day,),
        )
        r = cur.fetchone()
        return {"total": r["total"] or 0, "handoff_count": r["handoff_cnt"] or 0}

    def get_quality_flags_since_hours(self, hours: int) -> list[dict]:
        """最近 N 小时内决策的质量标记，供导出时按 (conversation_id, sender_id) 回填。

        仅返回定位键与三个标记位，避免把整行拉进内存。
        """
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT conversation_id, sender_id, created_at, handoff, rag_grounded, cited "
            "FROM decisions WHERE created_at >= datetime('now', 'localtime', ?)",
            (f"-{hours} hours",),
        )
        return [dict(r) for r in cur.fetchall()]
