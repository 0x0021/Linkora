"""Repository for routing quality tracking — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class RoutingQualityRepo:
    """Repository extracted from SQLiteStore for routing quality tracking."""

    DEFAULT_RETENTION_DAYS = 30

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store
        self._insert_count: int = 0
        self._retention_days: int = self.DEFAULT_RETENTION_DAYS

    # ── backward-compat properties for sqlite_store.py __init__ ──

    @property
    def _rq_insert_count(self) -> int:
        return self._insert_count

    @_rq_insert_count.setter
    def _rq_insert_count(self, val: int) -> None:
        self._insert_count = val

    @property
    def _rq_retention_days(self) -> int:
        return self._retention_days

    @_rq_retention_days.setter
    def _rq_retention_days(self, val: int) -> None:
        self._retention_days = int(val)

    # ── methods extracted from SQLiteStore ──

    def record_routing_quality(
        self,
        sender_id: str,
        sender_name: str = "",
        conversation_id: str = "",
        content_preview: str = "",
        primary_skill: str = "",
        primary_score: float = 0.0,
        primary_source: str = "",
        combo_count: int = 0,
        combo_skills: str = "[]",
        convergence_zone_size: int = 0,
        convergence_applied: int = 0,
        goal_fit_details: str = "{}",
        tools_exposed: str = "[]",
        routing_mode: str = "",
        candidates_count: int = 0,
        intent_disposition: str = "",
        intent_action: str = "",
        intent_actions: str = "",
        blocked_by_disabled_skill: str = "[]",
        message_type: str = "",
        llm_model: str = "",
        llm_rounds: int = 0,
        llm_latency_ms: float = 0.0,
        total_latency_ms: float = 0.0,
        reply_len: int = 0,
        reply_text: str = "",
        stages_json: str = "[]",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> int:
        def js(v: object) -> str | object:
            return json.dumps(v) if isinstance(v, (list, dict)) else (v or "")
        cur = self.store.conn.cursor()
        cur.execute(
            """INSERT INTO routing_quality
               (sender_id, sender_name, conversation_id, content_preview,
                primary_skill, primary_score, primary_source,
                combo_count, combo_skills,
                convergence_zone_size, convergence_applied, goal_fit_details,
                tools_exposed, routing_mode, candidates_count,
                intent_disposition, intent_action, intent_actions,
                blocked_by_disabled_skill, message_type,
                llm_model, llm_rounds, llm_latency_ms, total_latency_ms,
                reply_len, reply_text, stages_json,
                input_tokens, output_tokens, total_tokens, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sender_id or "", sender_name or "", conversation_id or "",
             content_preview or "",
             primary_skill or "", primary_score, primary_source or "",
             combo_count, js(combo_skills),
             convergence_zone_size, convergence_applied, js(goal_fit_details),
             js(tools_exposed), routing_mode or "", candidates_count,
             intent_disposition or "", intent_action or "", intent_actions or "",
             js(blocked_by_disabled_skill), message_type or "",
             llm_model or "", llm_rounds, llm_latency_ms, total_latency_ms,
             reply_len, reply_text or "", js(stages_json),
             input_tokens, output_tokens, total_tokens, cost_usd),
        )
        self.store.conn.commit()
        self._insert_count += 1
        if self._insert_count % 200 == 0:
            self._prune_routing_quality()
        return cur.lastrowid

    def update_routing_quality_trace(
        self,
        rq_id: int,
        llm_latency_ms: float = 0.0,
        llm_rounds: int = 0,
        llm_model: str = "",
        total_latency_ms: float = 0.0,
        reply_len: int = 0,
        reply_text: str = "",
        stages_json: str = "[]",
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        def js(v: object) -> str | object:
            return json.dumps(v) if isinstance(v, (list, dict)) else (v or "")
        cur = self.store.conn.cursor()
        cur.execute(
            """UPDATE routing_quality
               SET llm_latency_ms=?, llm_rounds=?, llm_model=?,
                   total_latency_ms=?, reply_len=?, reply_text=?, stages_json=?,
                   input_tokens=?, output_tokens=?, total_tokens=?, cost_usd=?
               WHERE id=?""",
            (llm_latency_ms, llm_rounds, llm_model or "",
             total_latency_ms, reply_len, reply_text or "", js(stages_json),
             input_tokens, output_tokens, total_tokens, cost_usd, rq_id),
        )
        self.store.conn.commit()

    def get_routing_quality(
        self,
        page: int = 1,
        page_size: int = 20,
        primary_skill: str | None = None,
        primary_source: str | None = None,
        time_filter: str | None = None,
        blocked_filter: str | None = None,
    ) -> dict:
        cur = self.store.conn.cursor()
        conditions: list[str] = []
        params: list = []
        if primary_skill:
            conditions.append("primary_skill = ?")
            params.append(primary_skill)
        if primary_source:
            conditions.append("primary_source = ?")
            params.append(primary_source)
        if time_filter:
            if time_filter == "today":
                conditions.append("DATE(created_at) = DATE('now', 'localtime')")
            elif time_filter == "month":
                conditions.append("strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now', 'localtime')")
            else:
                conditions.append("created_at >= ?")
                params.append(time_filter)
        if blocked_filter == "blocked":
            conditions.append("(blocked_by_disabled_skill NOT IN ('', '[]') AND blocked_by_disabled_skill IS NOT NULL)")
        elif blocked_filter == "unblocked":
            conditions.append("(blocked_by_disabled_skill IN ('', '[]') OR blocked_by_disabled_skill IS NULL)")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        cur.execute(f"SELECT COUNT(*) FROM routing_quality {where}", params)
        total = cur.fetchone()[0]

        offset = (page - 1) * page_size
        cur.execute(
            f"SELECT * FROM routing_quality {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            for col in ("combo_skills", "goal_fit_details", "tools_exposed", "stages_json"):
                val = r.get(col, "")
                if isinstance(val, str) and val:
                    try:
                        r[col] = json.loads(val)
                    except Exception as e:
                        logger.debug("列 %s JSON 解析失败: %s", col, e)
                        if col == "stages_json":
                            r[col] = []
                elif col == "stages_json":
                    r[col] = []
        return {"items": rows, "total": total, "page": page, "page_size": page_size}

    def get_routing_quality_stats(self) -> dict:
        cur = self.store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM routing_quality")
        total = cur.fetchone()[0]

        cur.execute("SELECT primary_skill, COUNT(*) as cnt FROM routing_quality WHERE primary_skill != '' GROUP BY primary_skill ORDER BY cnt DESC LIMIT 20")
        by_skill = {r["primary_skill"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT primary_source, COUNT(*) as cnt FROM routing_quality WHERE primary_source != '' GROUP BY primary_source ORDER BY cnt DESC")
        by_source = {r["primary_source"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT SUM(combo_count) as total_combo, SUM(convergence_applied) as total_convergence FROM routing_quality")
        agg = cur.fetchone()
        total_combo = agg["total_combo"] or 0
        total_convergence = agg["total_convergence"] or 0

        cur.execute("SELECT AVG(primary_score) as avg_score FROM routing_quality WHERE primary_score > 0")
        avg_row = cur.fetchone()
        avg_score = round(avg_row["avg_score"], 3) if avg_row and avg_row["avg_score"] else 0.0

        cur.execute("SELECT AVG(candidates_count) as avg_candidates FROM routing_quality WHERE candidates_count > 0")
        avg_row2 = cur.fetchone()
        avg_candidates = round(avg_row2["avg_candidates"], 1) if avg_row2 and avg_row2["avg_candidates"] else 0.0

        cur.execute("SELECT AVG(llm_latency_ms) as a, AVG(total_latency_ms) as b, MAX(total_latency_ms) as c FROM routing_quality WHERE total_latency_ms > 0")
        lat = cur.fetchone()
        avg_llm_ms = round(lat["a"], 1) if lat and lat["a"] else 0.0
        avg_total_ms = round(lat["b"], 1) if lat and lat["b"] else 0.0
        max_total_ms = round(lat["c"], 1) if lat and lat["c"] else 0.0

        cur.execute("SELECT SUM(input_tokens) as in_t, SUM(output_tokens) as out_t, SUM(total_tokens) as t_t, SUM(cost_usd) as c_usd FROM routing_quality")
        token_row = cur.fetchone()
        total_input_tokens = token_row["in_t"] or 0
        total_output_tokens = token_row["out_t"] or 0
        total_tokens = token_row["t_t"] or 0
        total_cost_usd = round(token_row["c_usd"] or 0.0, 4)

        cur.execute("SELECT AVG(input_tokens) as a, AVG(output_tokens) as b, AVG(total_tokens) as c FROM routing_quality WHERE total_tokens > 0")
        avg_token_row = cur.fetchone()
        avg_input_tokens = round(avg_token_row["a"]) if avg_token_row and avg_token_row["a"] else 0
        avg_output_tokens = round(avg_token_row["b"]) if avg_token_row and avg_token_row["b"] else 0
        avg_total_tokens = round(avg_token_row["c"]) if avg_token_row and avg_token_row["c"] else 0

        return {
            "total": total,
            "by_skill": by_skill,
            "by_source": by_source,
            "total_combo": total_combo,
            "total_convergence": total_convergence,
            "avg_score": avg_score,
            "avg_candidates": avg_candidates,
            "avg_llm_ms": avg_llm_ms,
            "avg_total_ms": avg_total_ms,
            "max_total_ms": max_total_ms,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost_usd,
            "avg_input_tokens": avg_input_tokens,
            "avg_output_tokens": avg_output_tokens,
            "avg_total_tokens": avg_total_tokens,
        }

    def get_routing_quality_aggregate(self) -> dict:
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT total_latency_ms, reply_len, stages_json, primary_source, "
            "convergence_applied, combo_count, intent_actions, "
            "blocked_by_disabled_skill FROM routing_quality"
        )
        rows = cur.fetchall()

        total_records = len(rows)
        ok_count = 0
        empty_count = 0
        total_lat_sum = 0.0
        stage_ms_sum: dict = {}
        stage_ms_cnt: dict = {}
        stage_fail_cnt: dict = {}
        source_cnt: dict = {}
        total_convergence = 0
        total_combo = 0
        total_proactive = 0
        total_blocked_records = 0
        total_blocked_tools = 0
        lat_hist = [0, 0, 0, 0, 0]
        lat_edges = (200, 500, 1000, 2000)

        for r in rows:
            total_lat = float(r["total_latency_ms"] or 0)
            total_lat_sum += total_lat
            rl = int(r["reply_len"] or 0)
            if rl > 0:
                ok_count += 1
            else:
                empty_count += 1

            sj = r["stages_json"]
            stages = []
            if isinstance(sj, str) and sj:
                try:
                    stages = json.loads(sj)
                except Exception as e:
                    logger.debug("阶段 JSON 解析失败: %s", e)
                    stages = []
            for s in stages:
                st = s.get("stage")
                if not st:
                    continue
                ms = float(s.get("ms") or 0)
                stage_ms_sum[st] = stage_ms_sum.get(st, 0.0) + ms
                stage_ms_cnt[st] = stage_ms_cnt.get(st, 0) + 1
                if s.get("status") == "fail":
                    stage_fail_cnt[st] = stage_fail_cnt.get(st, 0) + 1

            src = r["primary_source"] or "—"
            source_cnt[src] = source_cnt.get(src, 0) + 1
            total_convergence += int(r["convergence_applied"] or 0)
            total_combo += int(r["combo_count"] or 0)

            ia = r["intent_actions"] or ""
            if "action.monitor" in ia or "action.subscribe" in ia:
                total_proactive += 1

            _b = r["blocked_by_disabled_skill"] or ""
            _b_list = []
            if _b and _b != "[]":
                try:
                    _b_list = json.loads(_b) if isinstance(_b, str) else _b
                except Exception as e:
                    logger.debug("屏蔽工具 JSON 解析失败: %s", e)
                    _b_list = []
            if _b_list:
                total_blocked_records += 1
                total_blocked_tools += len(_b_list)

            idx = 0
            for i, edge in enumerate(lat_edges):
                if total_lat <= edge:
                    idx = i
                    break
            else:
                idx = len(lat_hist) - 1
            lat_hist[idx] += 1

        stage_avg: dict = {}
        for st, cnt in stage_ms_cnt.items():
            stage_avg[st] = round(stage_ms_sum[st] / cnt, 1) if cnt else 0.0
        sum_avg = sum(stage_avg.values()) or 1.0
        stage_share = {st: round(v / sum_avg, 3) for st, v in stage_avg.items()}
        bottleneck_stage = ""
        bottleneck_ms = 0.0
        for st, v in stage_avg.items():
            if v > bottleneck_ms:
                bottleneck_ms = v
                bottleneck_stage = st
        bottleneck_share = round((bottleneck_ms / sum_avg), 3) if sum_avg else 0.0

        avg_total_ms = round(total_lat_sum / total_records, 1) if total_records else 0.0
        empty_rate = round(empty_count / total_records, 3) if total_records else 0.0

        src_items = sorted(source_cnt.items(), key=lambda kv: kv[1], reverse=True)
        top_src = src_items[:6]
        other_cnt = sum(c for _, c in src_items[6:])
        source_split = {k: v for k, v in top_src}
        if other_cnt:
            source_split["其他"] = other_cnt

        lat_labels = ["≤200ms", "200-500", "500-1k", "1k-2k", ">2k"]
        latency_hist = [{"label": lat_labels[i], "count": lat_hist[i]} for i in range(len(lat_hist))]

        return {
            "total_records": total_records,
            "ok_count": ok_count,
            "empty_count": empty_count,
            "empty_rate": empty_rate,
            "avg_total_ms": avg_total_ms,
            "stage_avg": stage_avg,
            "stage_share": stage_share,
            "stage_fail_cnt": stage_fail_cnt,
            "bottleneck_stage": bottleneck_stage,
            "bottleneck_ms": bottleneck_ms,
            "bottleneck_share": bottleneck_share,
            "source_split": source_split,
            "latency_hist": latency_hist,
            "total_convergence": total_convergence,
            "total_combo": total_combo,
            "total_proactive": total_proactive,
            "total_blocked_records": total_blocked_records,
            "total_blocked_tools": total_blocked_tools,
        }

    def get_filter_options(self) -> dict:
        """路由质量筛选下拉的可选项：非空的 primary_skill / primary_source（各自升序去重）。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT DISTINCT primary_skill FROM routing_quality "
            "WHERE primary_skill != '' ORDER BY primary_skill"
        )
        skills = [r["primary_skill"] for r in cur.fetchall()]
        cur.execute(
            "SELECT DISTINCT primary_source FROM routing_quality "
            "WHERE primary_source != '' ORDER BY primary_source"
        )
        sources = [r["primary_source"] for r in cur.fetchall()]
        return {"skills": skills, "sources": sources}

    #: 详情接口中需要从 JSON 文本还原为对象的列
    _JSON_DETAIL_COLUMNS = ("combo_skills", "goal_fit_details", "tools_exposed", "stages_json")

    def get_routing_quality_detail(self, rq_id: int) -> dict | None:
        """单条路由追踪详情（含全链路瀑布 stages_json），不存在时返回 None。

        JSON 文本列会就地解析为对象；``stages_json`` 解析失败或缺失时统一降级为 []。
        """
        cur = self.store.conn.cursor()
        cur.execute("SELECT * FROM routing_quality WHERE id=?", (rq_id,))
        row = cur.fetchone()
        if not row:
            return None
        rec = dict(row)
        for col in self._JSON_DETAIL_COLUMNS:
            val = rec.get(col, "")
            if isinstance(val, str) and val:
                try:
                    rec[col] = json.loads(val)
                except Exception as _exc:
                    logger.debug(f"get_routing_quality_detail: swallowed exception: {_exc}")
                    if col == "stages_json":
                        rec[col] = []
            elif col == "stages_json":
                rec[col] = []
        return rec

    def get_primary_score_buckets(
        self, time_range_hours: int | None = None, bucket_count: int = 10
    ) -> list[int]:
        """把 primary_score（0~1）分成 bucket_count 个等宽左闭右开桶并计数。

        越界分值向两端夹取；score 为 NULL 的记录跳过。time_range_hours > 0 时只统计
        created_at 在该时间窗内的记录，否则统计全表。
        """
        cur = self.store.conn.cursor()
        time_clause = ""
        params: list = []
        if time_range_hours and time_range_hours > 0:
            cutoff = (datetime.now() - timedelta(hours=time_range_hours)).isoformat()
            time_clause = "WHERE created_at >= ?"
            params = [cutoff]
        cur.execute(f"SELECT primary_score FROM routing_quality {time_clause}", params)
        buckets = [0] * bucket_count
        for r in cur.fetchall():
            s = r["primary_score"]
            if s is None:
                continue
            idx = int(s * bucket_count)
            if idx < 0:
                idx = 0
            elif idx > bucket_count - 1:
                idx = bucket_count - 1
            buckets[idx] += 1
        return buckets

    def get_daily_cost_usd(self, day: str) -> float:
        """指定本地日期（YYYY-MM-DD）当天路由质量记录的 cost_usd 合计（保留 6 位小数）。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT ROUND(SUM(cost_usd), 6) AS cost FROM routing_quality "
            "WHERE DATE(created_at) = ?",
            (day,),
        )
        row = cur.fetchone()
        return row["cost"] or 0.0

    def get_records_since_hours(self, hours: int, limit: int) -> list[dict]:
        """最近 N 小时内的路由质量整行记录，created_at 倒序，最多 limit 条（供 CSV 导出）。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT * FROM routing_quality "
            "WHERE created_at >= datetime('now', 'localtime', ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (f"-{hours} hours", limit),
        )
        return [dict(r) for r in cur.fetchall()]

    def set_rq_retention_days(self, days: int) -> None:
        self._retention_days = int(days)

    def _prune_routing_quality(self) -> None:
        try:
            cur = self.store.conn.cursor()
            if self._retention_days > 0:
                cur.execute(
                    "DELETE FROM routing_quality WHERE "
                    "created_at < datetime('now', ?, 'localtime')",
                    (f"-{self._retention_days} days",),
                )
            self.store.conn.commit()
        except Exception as e:
            logger.warning("数据库提交失败: %s", e)
            try:
                self.store.conn.rollback()
            except Exception as re:
                logger.error("数据库回滚也失败: %s", re)
