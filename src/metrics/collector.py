"""MetricsCollector — read-only observability queries.

Accepts a SQLiteStore instance; all queries are stateless and side-effect free.

Query categories:
1. tool_stats       — per-tool call counts, success rate, P50/P95/P99 latency
2. routing_accuracy — accuracy by platform/source, low-score decisions
3. blacklist_trends — permanent vs temp, by reason, recent additions
4. token_stats      — token consumption aggregated by time window
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from src.memory.platform_context import get_current_platform

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# USD → CNY（¥）展示汇率。成本追踪链路已端到端可用（agent._mk_reply 经
# update_routing_quality_trace 写入 cost_usd），此处仅做展示层换算，便于中文用户阅读。
# 后续可接 config.yaml，目前用常量（不破既有行为）。
USD_CNY_RATE = 7.2


class MetricsCollector:
    """Stateless read-only metrics collector over a single SQLiteStore."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    # ── 1. Tool Call Statistics ────────────────────────────────────────

    def tool_stats(
        self,
        time_range_hours: int | None = None,
        limit: int = 50,
    ) -> dict:
        """Per-tool aggregation: call count, success rate, P50/P95/P99 latency.

        Args:
            time_range_hours: if set, only consider records within this many hours.
            limit: max tool names returned.
        """
        cur = self.store.conn.cursor()

        time_clause = ""
        params: list = []
        if time_range_hours is not None and time_range_hours > 0:
            cutoff = (datetime.now() - timedelta(hours=time_range_hours)).isoformat()
            time_clause = "WHERE created_at >= ?"
            params = [cutoff]

        # Per-tool aggregates
        cur.execute(
            f"""SELECT tool_name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok,
                       ROUND(AVG(duration_ms), 1) AS avg_ms,
                       MIN(duration_ms) AS min_ms,
                       MAX(duration_ms) AS max_ms
                FROM tool_execution_logs
                {time_clause}
                GROUP BY tool_name
                ORDER BY total DESC
                LIMIT ?""",
            params + [limit],
        )
        rows = cur.fetchall()

        tools = []
        for r in rows:
            name = r["tool_name"]
            total = r["total"] or 0
            ok = r["ok"] or 0
            # Per-tool percentiles
            percentiles = self._compute_percentiles(
                name, "tool_execution_logs", "duration_ms", time_clause, params
            )
            tools.append({
                "tool_name": name,
                "total_calls": total,
                "success_count": ok,
                "success_rate": round(ok / total * 100, 1) if total > 0 else 0.0,
                "avg_ms": r["avg_ms"] or 0.0,
                "min_ms": r["min_ms"] or 0.0,
                "max_ms": r["max_ms"] or 0.0,
                "p50_ms": percentiles.get("p50", 0.0),
                "p95_ms": percentiles.get("p95", 0.0),
                "p99_ms": percentiles.get("p99", 0.0),
            })

        return {"tools": tools, "time_range_hours": time_range_hours}

    def tool_recent_failures(self, limit: int = 20) -> list[dict]:
        """Return the N most recent failed tool executions."""
        cur = self.store.conn.cursor()
        cur.execute(
            """SELECT id, tool_name, duration_ms, error_message, created_at
               FROM tool_execution_logs
               WHERE success = 0
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        )
        return [
            {
                "id": r["id"],
                "tool_name": r["tool_name"],
                "duration_ms": r["duration_ms"],
                "error_message": (r["error_message"] or "")[:500],
                "created_at": r["created_at"],
            }
            for r in cur.fetchall()
        ]

    def _compute_percentiles(
        self,
        tool_name: str,
        table: str,
        column: str,
        time_clause: str,
        time_params: list,
    ) -> dict[str, float]:
        """Compute P50/P95/P99 for a numeric column in a table, filtered by tool_name.

        Uses ORDER BY + LIMIT/OFFSET to approximate percentiles in SQLite.
        """
        cur = self.store.conn.cursor()
        where = f"{'AND' if time_clause else 'WHERE'} tool_name = ?"
        params = time_params + [tool_name]

        cur.execute(
            f"SELECT COUNT(*) AS cnt FROM {table} {time_clause} {where}",
            params,
        )
        cnt = cur.fetchone()["cnt"] or 0
        if cnt == 0:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}

        result: dict[str, float] = {}
        for label, frac in [("p50", 0.50), ("p95", 0.95), ("p99", 0.99)]:
            idx = max(0, min(cnt - 1, int(cnt * frac)))
            cur.execute(
                f"""SELECT {column} FROM {table} {time_clause} {where}
                    ORDER BY {column} ASC LIMIT 1 OFFSET ?""",
                params + [idx],
            )
            row = cur.fetchone()
            result[label] = round(float(row[column] or 0), 1) if row else 0.0

        return result

    # ── 2. Routing Accuracy Dashboard ──────────────────────────────────

    def routing_accuracy(
        self,
        low_score_threshold: float = 0.5,
        limit: int = 50,
    ) -> dict:
        """Routing accuracy metrics.

        Returns:
            - by_source: accuracy breakdown by primary_source
            - low_score_decisions: most recent N low-score entries
            - overall: total, avg_score, accuracy_rate
        """
        cur = self.store.conn.cursor()

        # Overall stats
        cur.execute(
            "SELECT COUNT(*) AS total, AVG(primary_score) AS avg_score FROM routing_quality"
        )
        overall = cur.fetchone()
        total = overall["total"] or 0
        avg_score = round(overall["avg_score"], 3) if overall and overall["avg_score"] else 0.0

        # "Accuracy" defined as the proportion of entries with primary_score >= threshold
        cur.execute(
            "SELECT COUNT(*) AS accurate FROM routing_quality WHERE primary_score >= ?",
            (low_score_threshold,),
        )
        accurate = cur.fetchone()["accurate"] or 0
        accuracy_rate = round(accurate / total * 100, 1) if total > 0 else 0.0

        # By source
        cur.execute(
            """SELECT primary_source,
                       COUNT(*) AS cnt,
                       ROUND(AVG(primary_score), 3) AS avg_score,
                       SUM(CASE WHEN primary_score >= ? THEN 1 ELSE 0 END) AS accurate_cnt
                FROM routing_quality
                WHERE primary_source != ''
                GROUP BY primary_source
                ORDER BY cnt DESC""",
            (low_score_threshold,),
        )
        by_source = []
        for r in cur.fetchall():
            c = r["cnt"] or 0
            by_source.append({
                "source": r["primary_source"],
                "count": c,
                "avg_score": r["avg_score"] or 0.0,
                "accurate_count": r["accurate_cnt"] or 0,
                "accuracy_rate": round(r["accurate_cnt"] / c * 100, 1) if c > 0 else 0.0,
            })

        # Low-score decisions (most recent)
        cur.execute(
            """SELECT id, sender_name, content_preview, primary_skill,
                       primary_score, primary_source, routing_mode, created_at
                FROM routing_quality
                WHERE primary_score > 0 AND primary_score < ?
                ORDER BY created_at DESC
                LIMIT ?""",
            (low_score_threshold, limit),
        )
        low_score = [
            {
                "id": r["id"],
                "sender_name": r["sender_name"],
                "content_preview": (r["content_preview"] or "")[:200],
                "primary_skill": r["primary_skill"],
                "primary_score": r["primary_score"],
                "primary_source": r["primary_source"],
                "routing_mode": r["routing_mode"],
                "created_at": r["created_at"],
            }
            for r in cur.fetchall()
        ]

        return {
            "total": total,
            "avg_score": avg_score,
            "accuracy_rate": accuracy_rate,
            "low_score_threshold": low_score_threshold,
            "by_source": by_source,
            "low_score_decisions": low_score,
        }

    # ── 3. Blacklist Trends ────────────────────────────────────────────

    def blacklist_trends(self, recent_days: int = 7) -> dict:
        """Blacklist analytics.

        Returns:
            - permanent_vs_temp: count comparison
            - by_reason: classification by reason field
            - recent_additions: new entries in the past N days
        """
        # blocked_conversations 已按账号隔离，走 per-account 会话库
        cur = self.store.conv_conn(get_current_platform()).cursor()

        # Permanent vs temporary
        cur.execute(
            """SELECT
                 CASE WHEN cooldown_until IS NULL OR cooldown_until = ''
                      THEN 'permanent'
                      ELSE 'temporary'
                 END AS type,
                 COUNT(*) AS cnt
               FROM blocked_conversations
               GROUP BY type"""
        )
        perm_vs_temp = {r["type"]: r["cnt"] for r in cur.fetchall()}

        # By reason
        cur.execute(
            """SELECT reason, COUNT(*) AS cnt
               FROM blocked_conversations
               WHERE reason != ''
               GROUP BY reason
               ORDER BY cnt DESC
               LIMIT 20"""
        )
        by_reason = [
            {"reason": r["reason"], "count": r["cnt"]} for r in cur.fetchall()
        ]

        # Recent additions
        cutoff = (datetime.now() - timedelta(days=recent_days)).isoformat()
        cur.execute(
            """SELECT chat_id, chat_name, chat_type, reason, source,
                       cooldown_until, failure_count, detected_at
               FROM blocked_conversations
               WHERE detected_at >= ?
               ORDER BY detected_at DESC
               LIMIT 50""",
            (cutoff,),
        )
        recent = [
            {
                "chat_id": r["chat_id"],
                "chat_name": r["chat_name"],
                "chat_type": r["chat_type"],
                "reason": r["reason"],
                "source": r["source"],
                "is_permanent": not r["cooldown_until"],
                "cooldown_until": r["cooldown_until"],
                "failure_count": r["failure_count"],
                "detected_at": r["detected_at"],
            }
            for r in cur.fetchall()
        ]

        return {
            "total": sum(perm_vs_temp.values()),
            "permanent_vs_temporary": perm_vs_temp,
            "by_reason": by_reason,
            "recent_days": recent_days,
            "recent_additions": recent,
        }

    # ── 4. Token Consumption Tracking ──────────────────────────────────

    def token_stats(self, time_range_hours: int | None = None) -> dict:
        """Token consumption aggregated from routing_quality records.

        If the routing_quality table has token columns (input_tokens,
        output_tokens, total_tokens, cost_usd), aggregates them.
        Otherwise returns an empty framework structure for future use.

        Args:
            time_range_hours: if set, limit to recent N hours.
        """
        cur = self.store.conn.cursor()

        # Detect whether token columns exist in routing_quality
        cur.execute("PRAGMA table_info(routing_quality)")
        columns = {r["name"] for r in cur.fetchall()}
        has_tokens = "total_tokens" in columns

        if not has_tokens:
            # Framework-only mode: routing_quality doesn't have token columns yet.
            # Return empty structure so dashboards don't crash.
            return {
                "available": False,
                "reason": "routing_quality 表尚无 token 列（input_tokens/output_tokens/total_tokens/cost_usd），"
                          "token 追踪框架已就绪，待后续向表中补列后可自动激活。",
                "record_count": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "total_cost_cny": 0.0,
                "avg_input_tokens": 0,
                "avg_output_tokens": 0,
                "avg_total_tokens": 0,
                "avg_cost_usd": 0.0,
                "avg_cost_cny": 0.0,
                "time_range_hours": time_range_hours,
                "by_chat": [],
                "hourly": [],
            }

        time_clause = ""
        params: list = []
        if time_range_hours is not None and time_range_hours > 0:
            cutoff = (datetime.now() - timedelta(hours=time_range_hours)).isoformat()
            time_clause = "WHERE created_at >= ?"
            params = [cutoff]

        cur.execute(
            f"""SELECT
                   COUNT(*) AS record_count,
                   SUM(input_tokens) AS total_input,
                   SUM(output_tokens) AS total_output,
                   SUM(total_tokens) AS total_tokens,
                   ROUND(SUM(cost_usd), 6) AS total_cost_usd,
                   ROUND(AVG(input_tokens), 0) AS avg_input,
                   ROUND(AVG(output_tokens), 0) AS avg_output,
                   ROUND(AVG(total_tokens), 0) AS avg_total,
                   ROUND(AVG(cost_usd), 6) AS avg_cost_usd
               FROM routing_quality
               {time_clause}""",
            params,
        )
        agg = cur.fetchone()

        # Per chat_id aggregation (top N by token volume)
        cur.execute(
            f"""SELECT conversation_id,
                       COUNT(*) AS record_count,
                       SUM(total_tokens) AS total_tokens,
                       ROUND(SUM(cost_usd), 6) AS total_cost_usd
                FROM routing_quality
                {time_clause}
                {'AND' if time_clause else 'WHERE'} conversation_id != ''
                GROUP BY conversation_id
                ORDER BY total_tokens DESC
                LIMIT 20""",
            params,
        )
        by_chat = [
            {
                "conversation_id": r["conversation_id"],
                "record_count": r["record_count"],
                "total_tokens": r["total_tokens"] or 0,
                "total_cost_usd": r["total_cost_usd"] or 0,
            }
            for r in cur.fetchall()
        ]

        # Hourly breakdown (last 24 hours, grouped by hour)
        cur.execute(
            """SELECT strftime('%Y-%m-%dT%H:00:00', created_at) AS hour,
                      COUNT(*) AS record_count,
                      SUM(total_tokens) AS total_tokens,
                      ROUND(SUM(cost_usd), 6) AS total_cost_usd
               FROM routing_quality
               WHERE created_at >= datetime('now', '-24 hours', 'localtime')
               GROUP BY hour
               ORDER BY hour ASC"""
        )
        hourly = [
            {
                "hour": r["hour"],
                "record_count": r["record_count"],
                "total_tokens": r["total_tokens"] or 0,
                "total_cost_usd": r["total_cost_usd"] or 0,
            }
            for r in cur.fetchall()
        ]

        return {
            "available": True,
            "record_count": agg["record_count"] or 0,
            "total_input_tokens": agg["total_input"] or 0,
            "total_output_tokens": agg["total_output"] or 0,
            "total_tokens": agg["total_tokens"] or 0,
            "total_cost_usd": agg["total_cost_usd"] or 0.0,
            "total_cost_cny": round((agg["total_cost_usd"] or 0.0) * USD_CNY_RATE, 4),
            "avg_input_tokens": agg["avg_input"] or 0,
            "avg_output_tokens": agg["avg_output"] or 0,
            "avg_total_tokens": agg["avg_total"] or 0,
            "avg_cost_usd": agg["avg_cost_usd"] or 0.0,
            "avg_cost_cny": round((agg["avg_cost_usd"] or 0.0) * USD_CNY_RATE, 6),
            "time_range_hours": time_range_hours,
            "by_chat": by_chat,
            "hourly": hourly,
        }

    # ── 5. Composite Snapshot ──────────────────────────────────────────

    def snapshot(self, time_range_hours: int | None = 24) -> dict:
        """Return a complete metrics snapshot (all four categories)."""
        return {
            "tool_stats": self.tool_stats(time_range_hours=time_range_hours),
            "tool_recent_failures": self.tool_recent_failures(limit=20),
            "routing_accuracy": self.routing_accuracy(),
            "blacklist_trends": self.blacklist_trends(),
            "token_stats": self.token_stats(time_range_hours=time_range_hours),
            "generated_at": datetime.now().isoformat(),
        }
