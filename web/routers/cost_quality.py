"""成本 / 质量看板路由（Roadmap ③，可并入 P2-7 Web 引文面板）。

聚合各平台的成本（Token / ¥）与质量标记（低置信转人工率 / RAG 命中率 /
引文页脚命中率 / 反馈有用率）及置信度分布，供前端成本质量看板一次性拉取。

所有数据均经 MetricsCollector 与仓储层（DecisionsRepo / RoutingQualityRepo /
FeedbackRepo）读取，本模块不直接持有 SQL；所有阻塞 SQLite 查询经
run_in_threadpool 包裹（T6 模式），不阻塞异步事件循环。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from web.dependencies import get_app_instance, logger

router = APIRouter()


def _iter_platform_stores():
    """Yield (platform_id, store) for all initialized platforms."""
    app_instance = get_app_instance()
    if app_instance is None or not hasattr(app_instance, "platforms"):
        return
    for platform_id, ctx in app_instance.platforms.items():
        store = getattr(ctx, "store", None)
        if store is None:
            continue
        yield platform_id, store


def _bucket_label(i, bucket_count=10):
    return f"{i/bucket_count:.1f}-{(i+1)/bucket_count:.1f}"


def _confidence_hist(store, time_range_hours):
    """routing_quality.primary_score 分 10 桶（0~1，左闭右开）。"""
    buckets = store._routing_quality_repo.get_primary_score_buckets(
        time_range_hours=time_range_hours
    )
    return [{"bucket": _bucket_label(i), "count": c} for i, c in enumerate(buckets)]


def _citations_recent(store, limit=20):
    """P2-7：最近 N 条实际追加了引文页脚(cited=1)的决策，用于引文子面板占位。

    当前 decisions 表仅记录 cited 标记与 reply_preview；完整引文 source/score/snippet
    后续可从 routing_quality / 引文溯源链路补全。此处先返回可展示的精简结构。
    """
    items = store._decisions_repo.get_recent_cited(limit=limit)
    return {"total": len(items), "items": items}


def _work_summary(hours):
    from src.metrics.collector import MetricsCollector

    totals = {
        "total_cost_cny": 0.0,
        "total_cost_usd": 0.0,
        "total_tokens": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "handoff_count": 0,
        "rag_grounded_count": 0,
        "cited_count": 0,
        "decision_total": 0,
        "feedback_total": 0,
        "feedback_useful_count": 0,
    }
    by_platform: dict = {}
    confidence_hist_acc = [0] * 10

    for pid, store in _iter_platform_stores():
        c = MetricsCollector(store)
        token = c.token_stats(time_range_hours=hours)
        q = store._decisions_repo.get_quality_stats(time_range_hours=hours)
        fb = store._feedback_repo.get_useful_rate()
        hist = _confidence_hist(store, hours)

        by_platform[pid] = {
            "cost_cny": token.get("total_cost_cny", 0.0),
            "cost_usd": token.get("total_cost_usd", 0.0),
            "total_tokens": token.get("total_tokens", 0),
            "quality": q,
            "feedback": fb,
        }
        totals["total_cost_cny"] += token.get("total_cost_cny", 0.0)
        totals["total_cost_usd"] += token.get("total_cost_usd", 0.0)
        totals["total_tokens"] += token.get("total_tokens", 0)
        totals["total_input_tokens"] += token.get("total_input_tokens", 0)
        totals["total_output_tokens"] += token.get("total_output_tokens", 0)
        totals["handoff_count"] += q["handoff_count"]
        totals["rag_grounded_count"] += q["rag_grounded_count"]
        totals["cited_count"] += q["cited_count"]
        totals["decision_total"] += q["total"]
        totals["feedback_total"] += fb["total"]
        totals["feedback_useful_count"] += fb["useful_count"]
        for i, b in enumerate(hist):
            confidence_hist_acc[i] += b["count"]

    decision_total = totals["decision_total"] or 0
    feedback_total = totals["feedback_total"] or 0
    return {
        "available": bool(by_platform),
        "totals": {
            **totals,
            "total_cost_cny": round(totals["total_cost_cny"], 4),
            "total_cost_usd": round(totals["total_cost_usd"], 6),
            "handoff_rate": round(totals["handoff_count"] / decision_total, 4) if decision_total else 0.0,
            "rag_grounded_rate": round(totals["rag_grounded_count"] / decision_total, 4) if decision_total else 0.0,
            "cited_rate": round(totals["cited_count"] / decision_total, 4) if decision_total else 0.0,
            "feedback_useful_rate": round(totals["feedback_useful_count"] / feedback_total, 4) if feedback_total else 0.0,
        },
        "by_platform": by_platform,
        "confidence_hist": [
            {"bucket": _bucket_label(i), "count": confidence_hist_acc[i]}
            for i in range(10)
        ],
        "hours": hours,
    }


@router.get("/api/cost-quality/summary")
async def cost_quality_summary(hours: int = Query(default=24, ge=1, le=720)):
    """成本/质量看板总览：成本(¥) + 质量标记率 + 反馈有用率 + 置信度分布。"""
    try:
        return await run_in_threadpool(_work_summary, hours)
    except Exception as e:
        logger.error("成本质量看板 summary API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/cost-quality/confidence-hist")
async def cost_quality_confidence_hist(hours: int = Query(default=24, ge=1, le=720)):
    """置信度分布（routing_quality.primary_score 分 10 桶 0~1）。"""
    try:
        def _work():
            acc = [0] * 10
            for pid, store in _iter_platform_stores():
                hist = _confidence_hist(store, hours)
                for i, b in enumerate(hist):
                    acc[i] += b["count"]
            return {
                "available": True,
                "hours": hours,
                "hist": [
                    {"bucket": _bucket_label(i), "count": acc[i]}
                    for i in range(10)
                ],
            }
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("成本质量看板 confidence-hist API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/cost-quality/trend")
async def cost_quality_trend(days: int = Query(default=7, ge=1, le=365)):
    """每日成本(¥) / 转人工率 趋势（供折线图）。"""
    try:
        def _work():
            from src.metrics.collector import MetricsCollector, USD_CNY_RATE

            series = []
            for d in range(days - 1, -1, -1):
                day_start = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
                day_cost_cny = 0.0
                day_handoff = 0
                day_total = 0
                for pid, store in _iter_platform_stores():
                    c = MetricsCollector(store)
                    ts = c.token_stats(time_range_hours=None)
                    # 按天筛选 token_stats 的 hourly（最近 24h 窗口）不可靠，改为直接按 created_at 日聚合
                    day_cost_cny += store._routing_quality_repo.get_daily_cost_usd(day_start) * USD_CNY_RATE
                    q = store._decisions_repo.get_quality_stats(
                        time_range_hours=None
                    )
                    # 按天筛选 decisions
                    dr = store._decisions_repo.get_daily_handoff_stats(day_start)
                    day_total += dr["total"]
                    day_handoff += dr["handoff_count"]
                series.append({
                    "date": day_start,
                    "cost_cny": round(day_cost_cny, 4),
                    "handoff_rate": round(day_handoff / day_total, 4) if day_total else 0.0,
                    "total": day_total,
                })
            return {"available": True, "days": days, "series": series}
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("成本质量看板 trend API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/cost-quality/citations")
async def cost_quality_citations(limit: int = Query(default=20, ge=1, le=100)):
    """P2-7：最近 N 条实际追加了引文页脚(cited=1)的决策（引文子面板占位）。"""
    try:
        def _work():
            items_acc: list = []
            for pid, store in _iter_platform_stores():
                res = _citations_recent(store, limit=limit)
                for it in res["items"]:
                    it["platform_id"] = pid
                    items_acc.append(it)
            items_acc.sort(key=lambda x: x.get("id", 0), reverse=True)
            return {"available": True, "total": len(items_acc), "items": items_acc[:limit]}
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("成本质量看板 citations API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 导出成本质量数据为 CSV ─────────────────────────────────────────────
_CQ_CSV_COLS = [
    "id", "platform", "sender_id", "sender_name", "conversation_id",
    "primary_skill", "primary_score", "routing_mode",
    "total_latency_ms", "llm_latency_ms", "llm_calls", "llm_model",
    "reply_len", "content_preview", "handoff", "rag_grounded", "cited",
    "feedback_useful", "created_at",
]


@router.get("/api/cost-quality/export")
async def export_cost_quality(hours: int = Query(default=24, ge=1, le=720), limit: int = Query(default=10000, le=20000)):
    """导出成本质量数据为 CSV（utf-8-sig BOM，Excel 兼容）。"""
    try:
        limit = max(1, min(limit, 20000))

        def _work():
            all_rows = []
            for pid, store in _iter_platform_stores():
                rq_rows = store._routing_quality_repo.get_records_since_hours(hours, limit)

                for r in rq_rows:
                    row = {
                        "id": r.get("id"),
                        "platform": pid,
                        "sender_id": r.get("sender_id", ""),
                        "sender_name": r.get("sender_name", ""),
                        "conversation_id": r.get("conversation_id", ""),
                        "primary_skill": r.get("primary_skill", ""),
                        "primary_score": r.get("primary_score", 0),
                        "routing_mode": r.get("routing_mode", ""),
                        "total_latency_ms": r.get("total_latency_ms", 0),
                        "llm_latency_ms": r.get("llm_latency_ms", 0),
                        "llm_calls": r.get("llm_rounds", 0),
                        "llm_model": r.get("llm_model", ""),
                        "reply_len": r.get("reply_len", 0),
                        "content_preview": (r.get("content_preview", "") or "")[:200],
                        "handoff": 0,
                        "rag_grounded": 0,
                        "cited": 0,
                        "feedback_useful": 0,
                        "created_at": r.get("created_at", ""),
                    }
                    all_rows.append(row)

            # 补填 decisions 表的质量标记
            for pid, store in _iter_platform_stores():
                dec_map = {}
                for d in store._decisions_repo.get_quality_flags_since_hours(hours):
                    key = (d["conversation_id"], d["sender_id"])
                    dec_map[key] = d

                for row in all_rows:
                    if row["platform"] != pid:
                        continue
                    key = (row["conversation_id"], row["sender_id"])
                    d = dec_map.get(key)
                    if d:
                        row["handoff"] = d["handoff"] or 0
                        row["rag_grounded"] = d["rag_grounded"] or 0
                        row["cited"] = d["cited"] or 0

            all_rows.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return all_rows[:limit]

        rows = await run_in_threadpool(_work)

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(_CQ_CSV_COLS)
        for r in rows:
            writer.writerow([r.get(k, "") for k in _CQ_CSV_COLS])

        output.seek(0)
        date_tag = datetime.now().strftime("%Y%m%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=cost_quality_{date_tag}.csv"},
        )
    except Exception as e:
        logger.error("成本质量导出API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
