"""LLM 用量台账（统一 token / 成本记账，全调用方覆盖）。

背景：此前 token/成本统计唯一来源是 routing_quality 表（仅主回复链路写入），
对话外的 LLM 消耗——对话摘要（H2-A 调度器 / 展示摘要 / 补跑）、记忆提取与合并、
主人画像、技能意图生成、工具结果清洗、知识库处理——全部漏计。

方案：LLMClient.chat() 每次调用（含流式）结束后调用本模块记账，purpose 由
调用方显式标注（未标注归 "other"）。台账存**主库**（data/linkora.db），
与平台分库解耦——它是全局消耗，统计页直接读，无需遍历 platform stores。

并发模型（遵守项目 SQLite 血泪规则）：每次记账/查询**短连接**（connect→写→close），
不复用长生命周期连接，从根上规避「游标残留 WAL 读快照 → SQLITE_BUSY_SNAPSHOT」。
记账频率 = LLM 调用频率（低频），短连接开销可忽略。

记账失败绝不抛出（best-effort）：任何异常仅记 debug 日志，绝不影响 LLM 调用主链路。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from src.paths import data_path

logger = logging.getLogger(__name__)

# 用途标识（purpose）。新增用途直接用新字符串即可（表按文本存储，无需迁移），
# 这里列出已知用途供前端做中文标签映射与回归测试锚定。
PURPOSE_REPLY = "reply"          # 主回复（含截断续写）
PURPOSE_SUMMARY = "summary"      # 对话摘要（H2-A / 展示摘要 / 补跑）
PURPOSE_MEMORY = "memory"        # 记忆提取 + 记忆摘要合并
PURPOSE_PERSONA = "persona"      # 主人画像
PURPOSE_SKILL = "skill"          # 技能意图/提示词生成
PURPOSE_TOOL = "tool"            # 工具结果 LLM 清洗
PURPOSE_KB = "kb"                # 知识库处理
PURPOSE_OTHER = "other"          # 未显式标注的调用

_DDL = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'other',
    platform TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    is_estimated INTEGER NOT NULL DEFAULT 0
)
"""


# 测试钩子：重定向台账库路径（默认主库 data/linkora.db；单测指向 tmp 文件避免污染真实库）
_db_path_override: str | None = None


def _db_file() -> str:
    if _db_path_override:
        return _db_path_override
    return str(data_path("linkora.db"))


def _connect() -> sqlite3.Connection:
    """短连接（每次记账/查询独立建立，用完即关）。"""
    conn = sqlite3.connect(_db_file(), timeout=5.0)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL)


def _current_platform() -> str:
    """读当前平台上下文（平台作用域由 platform_scope 统一封装；取不到为空串）。"""
    try:
        from src.memory.platform_context import get_current_platform
        return get_current_platform() or ""
    except Exception:
        return ""


def record_usage(
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float = 0.0,
    total_tokens: int | None = None,
    platform: str | None = None,
    is_estimated: bool = False,
) -> None:
    """记一笔 LLM 用量。任何失败只记 debug 日志，绝不抛出。"""
    try:
        if total_tokens is None:
            total_tokens = int(input_tokens or 0) + int(output_tokens or 0)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if platform is None:
            platform = _current_platform()
        conn = _connect()
        try:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO llm_usage (ts, purpose, platform, model, input_tokens,"
                " output_tokens, total_tokens, cost_usd, is_estimated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, purpose or PURPOSE_OTHER, platform or "", model or "",
                 int(input_tokens or 0), int(output_tokens or 0), int(total_tokens),
                 float(cost_usd or 0.0), 1 if is_estimated else 0),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.debug("[usage_ledger] 记账失败（不影响主链路）", exc_info=True)


def get_stats(hours: int | None = None) -> dict:
    """按用途/平台聚合用量。hours=None 统计全部历史；否则仅最近 N 小时。

    返回 {"totals": {...}, "by_purpose": {...}, "by_platform": {...}, "available": True}。
    cost_usd 原始值 + cost_cny（按 USD_CNY_RATE 展示汇率换算，与成本页同源）。
    查询失败返回 available=False（调用方降级展示），绝不抛出。
    """
    try:
        from src.metrics.collector import USD_CNY_RATE
    except Exception:
        USD_CNY_RATE = 7.2
    try:
        conn = _connect()
        try:
            _ensure_table(conn)
            where, params = "", []
            if hours is not None:
                cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                where, params = "WHERE ts >= ?", [cutoff]
            cur = conn.execute(
                "SELECT purpose, COUNT(*) AS calls,"
                " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
                " COALESCE(SUM(cost_usd), 0) AS cost_usd,"
                " COALESCE(SUM(is_estimated), 0) AS estimated_calls"
                f" FROM llm_usage {where} GROUP BY purpose",
                params,
            )
            rows = cur.fetchall()
            cur.close()
            cur2 = conn.execute(
                "SELECT COALESCE(NULLIF(platform, ''), 'unknown') AS platform, COUNT(*) AS calls,"
                " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
                " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
                " COALESCE(SUM(total_tokens), 0) AS total_tokens,"
                " COALESCE(SUM(cost_usd), 0) AS cost_usd"
                f" FROM llm_usage {where} GROUP BY platform ORDER BY total_tokens DESC",
                params,
            )
            plat_rows = cur2.fetchall()
            cur2.close()
        finally:
            conn.close()
        by_purpose: dict = {}
        for r in rows:
            by_purpose[r[0]] = {
                "calls": r[1], "input_tokens": r[2], "output_tokens": r[3],
                "total_tokens": r[4], "cost_usd": round(r[5], 6),
                "cost_cny": round(r[5] * USD_CNY_RATE, 4),
                "estimated_calls": r[6],
            }
        by_platform: dict = {}
        for r in plat_rows:
            by_platform[r[0]] = {
                "calls": r[1], "input_tokens": r[2], "output_tokens": r[3],
                "total_tokens": r[4], "cost_usd": round(r[5], 6),
                "cost_cny": round(r[5] * USD_CNY_RATE, 4),
            }
        totals = {
            "calls": sum(v["calls"] for v in by_purpose.values()),
            "input_tokens": sum(v["input_tokens"] for v in by_purpose.values()),
            "output_tokens": sum(v["output_tokens"] for v in by_purpose.values()),
            "total_tokens": sum(v["total_tokens"] for v in by_purpose.values()),
            "cost_usd": round(sum(v["cost_usd"] for v in by_purpose.values()), 6),
            "cost_cny": round(sum(v["cost_usd"] for v in by_purpose.values()) * USD_CNY_RATE, 4),
            "estimated_calls": sum(v["estimated_calls"] for v in by_purpose.values()),
        }
        return {"available": True, "totals": totals,
                "by_purpose": by_purpose, "by_platform": by_platform}
    except Exception:
        logger.warning("[usage_ledger] 统计查询失败", exc_info=True)
        return {"available": False, "totals": {}, "by_purpose": {}, "by_platform": {}}


def get_daily_cost_usd(day: str) -> float:
    """某自然日（YYYY-MM-DD）的台账成本（USD），供成本趋势与 routing_quality 日成本合并。"""
    try:
        conn = _connect()
        try:
            _ensure_table(conn)
            cur = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE ts LIKE ?",
                [day + "%"],
            )
            row = cur.fetchone()
            cur.close()
        finally:
            conn.close()
        return float(row[0] or 0.0)
    except Exception:
        return 0.0
