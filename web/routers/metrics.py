"""运行指标路由：防抖 / 背压 / 向量模型加载状态 + 可观测性指标。

从 `web/api.py` 抽取（原 3106–3164 行），业务逻辑不变。
get_app_instance 取自 `web.dependencies`。

=== 前端已消费端点 ===
- /api/debounce-metrics        — 仪表盘 loadReliabilityMetrics（dashboard.js）
- /api/backpressure-metrics    — 仪表盘 loadReliabilityMetrics（dashboard.js）
- /api/embedding-status        — 向量状态页 loadEmbeddingStatus（vector_status.js）
- /api/poller-status           — 仪表盘 loadPollerMetrics（dashboard.js）
- /api/llm-metrics             — 指标页 loadMetricsPage（metrics.js）

=== backend-only（前端当前未消费，标注 # backend-only）===
- /api/metrics                 — 全量指标快照（聚合所有平台）
- /api/metrics/tools           — 工具调用统计（含 P50/P95/P99）
- /api/metrics/tools/failures  — 最近 N 次工具调用失败详情
- /api/metrics/routing         — 路由准确率看板
- /api/metrics/blacklist       — 黑名单趋势
- /api/metrics/tokens          — Token 消费追踪
- /api/metrics/realtime        — 实时指标聚合（最近 N 秒）
- /api/metrics/by-rid/{rid}    — 按 request_id 查全链路记录
- /api/metrics/tool-staleness  — 工具结果过期/失效统计
"""

from __future__ import annotations

import csv
import io
import json as _json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from web.dependencies import get_app_instance, logger

router = APIRouter()


@router.get("/api/debounce-metrics")
async def debounce_metrics():
    """防抖「纯数据/不完整」批次监控指标（P1-C）。

    返回拉长窗口触发次数、累计额外等待、以及「窗口内是否收到后续请求」的命中情况，
    用于判断 60s 等待策略是否生效、是否导致偏慢回复。
    """
    try:
        app_instance = get_app_instance()
        if app_instance is None or not hasattr(app_instance, "get_debounce_metrics"):
            return {"available": False, "reason": "bot 未就绪"}
        return {"available": True, **app_instance.get_debounce_metrics()}
    except Exception as e:
        logger.error("防抖指标API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/backpressure-metrics")
async def backpressure_metrics():
    """积压回放背压监控指标（P1-E）。

    返回单轮派发上限、累计派发/被限速延迟条数、上一轮实际派发/延迟条数，
    以及并发回复上限，用于判断重启/突发时背压限速是否生效、是否仍有积压堆积。
    """
    try:
        app_instance = get_app_instance()
        if app_instance is None or not hasattr(app_instance, "get_backpressure_metrics"):
            return {"available": False, "reason": "bot 未就绪"}
        return {"available": True, **app_instance.get_backpressure_metrics()}
    except Exception as e:
        logger.error("背压指标API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/embedding-status")
async def embedding_status():
    """向量模型加载状态（含下载进度），供前端轮询展示。"""
    try:
        app_instance = get_app_instance()
        client = getattr(app_instance, "embedding_client", None) if app_instance else None
        if client is None:
            return {
                "state": "unknown",
                "progress": 0.0,
                "downloaded": 0,
                "total": 0,
                "message": "嵌入客户端未初始化",
                "enabled": False,
                "model": None,
                "offline": None,
            }
        status = client.get_load_status()
        status["enabled"] = client.enabled
        status["model"] = getattr(client.config, "model", None)
        status["offline"] = bool(getattr(client.config, "offline", False))
        return status
    except Exception as e:
        logger.error("嵌入状态API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/poller-status")
async def poller_status():
    """轮询器综合可观测状态（最近轮询时间 / 异常 / 队列深度 / 派发统计）。"""
    try:
        app_instance = get_app_instance()
        if app_instance is None or not hasattr(app_instance, "get_poller_status"):
            return {"available": False, "reason": "bot 未就绪"}
        return {"available": True, **app_instance.get_poller_status()}
    except Exception as e:
        logger.error("轮询状态API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _llm_metrics_sync():
    """LLM 推理与路由质量指标（多平台聚合）—— 同步实现，供线程池调用。

    返回路由质量表中的聚合统计，包括平均 LLM 延迟、平均总延迟、最大总延迟、
    技能命中分布、路由来源分布等，用于性能监控与瓶颈分析。
    """
    try:
        app_instance = get_app_instance()
        if app_instance is None or not hasattr(app_instance, "platforms"):
            return {"available": False, "reason": "bot 未就绪"}

        aggregated = {
            "available": True,
            "total": 0,
            "by_skill": {},
            "by_source": {},
            "total_combo": 0,
            "total_convergence": 0,
            "avg_score": 0.0,
            "avg_candidates": 0.0,
            "avg_llm_ms": 0.0,
            "avg_total_ms": 0.0,
            "max_total_ms": 0.0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "avg_input_tokens": 0,
            "avg_output_tokens": 0,
            "avg_total_tokens": 0,
            "platforms": {},
        }

        platforms = app_instance.platforms
        total_weight = 0
        for platform_id, ctx in platforms.items():
            store = getattr(ctx, "store", None)
            if store is None or not hasattr(store, "_routing_quality_repo"):
                continue
            try:
                stats = store._routing_quality_repo.get_routing_quality_stats()
            except Exception:
                logger.warning("平台 %s 路由质量统计读取失败", platform_id, exc_info=True)
                continue

            aggregated["platforms"][platform_id] = stats
            aggregated["total"] += stats.get("total", 0)
            aggregated["total_combo"] += stats.get("total_combo", 0)
            aggregated["total_convergence"] += stats.get("total_convergence", 0)

            for skill, cnt in stats.get("by_skill", {}).items():
                aggregated["by_skill"][skill] = aggregated["by_skill"].get(skill, 0) + cnt
            for source, cnt in stats.get("by_source", {}).items():
                aggregated["by_source"][source] = aggregated["by_source"].get(source, 0) + cnt

            n = stats.get("total", 0)
            if n > 0:
                total_weight += n
                aggregated["avg_llm_ms"] += stats.get("avg_llm_ms", 0.0) * n
                aggregated["avg_total_ms"] += stats.get("avg_total_ms", 0.0) * n
                aggregated["avg_score"] += stats.get("avg_score", 0.0) * n
                aggregated["avg_candidates"] += stats.get("avg_candidates", 0.0) * n
                aggregated["avg_input_tokens"] += stats.get("avg_input_tokens", 0) * n
                aggregated["avg_output_tokens"] += stats.get("avg_output_tokens", 0) * n
                aggregated["avg_total_tokens"] += stats.get("avg_total_tokens", 0) * n
            aggregated["max_total_ms"] = max(
                aggregated["max_total_ms"], stats.get("max_total_ms", 0.0)
            )
            aggregated["total_input_tokens"] += stats.get("total_input_tokens", 0)
            aggregated["total_output_tokens"] += stats.get("total_output_tokens", 0)
            aggregated["total_tokens"] += stats.get("total_tokens", 0)
            aggregated["total_cost_usd"] += stats.get("total_cost_usd", 0.0)

        if total_weight > 0:
            aggregated["avg_llm_ms"] = round(aggregated["avg_llm_ms"] / total_weight, 1)
            aggregated["avg_total_ms"] = round(aggregated["avg_total_ms"] / total_weight, 1)
            aggregated["avg_score"] = round(aggregated["avg_score"] / total_weight, 3)
            aggregated["avg_candidates"] = round(aggregated["avg_candidates"] / total_weight, 1)
            aggregated["avg_input_tokens"] = round(aggregated["avg_input_tokens"] / total_weight)
            aggregated["avg_output_tokens"] = round(aggregated["avg_output_tokens"] / total_weight)
            aggregated["avg_total_tokens"] = round(aggregated["avg_total_tokens"] / total_weight)
        else:
            aggregated["avg_llm_ms"] = 0.0
            aggregated["avg_total_ms"] = 0.0
            aggregated["avg_score"] = 0.0
            aggregated["avg_candidates"] = 0.0
            aggregated["avg_input_tokens"] = 0
            aggregated["avg_output_tokens"] = 0
            aggregated["avg_total_tokens"] = 0
        aggregated["total_cost_usd"] = round(aggregated["total_cost_usd"], 4)

        return aggregated
    except Exception as e:
        logger.error("LLM指标API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/llm-metrics")
async def llm_metrics():
    """LLM 推理与路由质量指标（多平台聚合）。

    实现见 `_llm_metrics_sync`；内部逐平台读 routing_quality 表（同步 DB），
    平台多/数据量大时耗时明显，整体放线程池执行，避免阻塞事件循环。
    """
    return await run_in_threadpool(_llm_metrics_sync)


@router.get("/api/metrics/realtime")
async def metrics_realtime(window: int = 300):
    """实时指标聚合（最近 window 秒，默认 5min）。
    # backend-only — 前端未消费，供未来实时面板或外部集成用。

    数据来源：MetricsAggregator（来自 src/utils/metrics.py）。
    用于前端仪表盘的快速 KPI 卡片展示。
    """
    try:
        from src.utils.metrics import MetricsAggregator
        agg = MetricsAggregator.instance()
        summary = agg.summary(window_seconds=window)
        lifetime = agg.lifetime()
        recent = agg.recent_requests(n=30)
        return {
            "available": True,
            "summary": summary,
            "lifetime": lifetime,
            "recent": recent,
        }
    except Exception as e:
        logger.error("实时指标API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/metrics/by-rid/{request_id:path}")
async def metrics_by_rid(request_id: str):
    """按 request_id 查询该请求的全链路记录（用于故障排查时 grep 一个 id）。
    # backend-only — 前端未消费，可通过 curl / 临时调试使用。

    返回该 rid 关联的所有 decisions 记录（从决策表里查），前端可展示完整 trace。
    """
    try:
        def _work():
            from web.dependencies import get_app_instance
            app_instance = get_app_instance()
            if app_instance is None or not hasattr(app_instance, "platforms"):
                return {"available": False, "reason": "bot 未就绪"}
            results = []
            for platform_id, ctx in app_instance.platforms.items():
                store = getattr(ctx, "store", None)
                if store is None or not hasattr(store, "_decisions_repo"):
                    continue
                try:
                    rows = store._decisions_repo.query_decisions_by_rid(request_id=request_id, limit=50)
                    for r in rows:
                        r["_platform"] = platform_id
                    results.extend(rows)
                except Exception:
                    logger.warning("按 rid 查询失败 platform=%s", platform_id, exc_info=True)
            return {"available": True, "request_id": request_id, "count": len(results), "items": results}
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("按 rid 查询 API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/metrics/tool-staleness")
async def tool_staleness():
    """工具结果过期/失效统计。
    # backend-only — 前端未消费，保留供监控脚本或未来工具健康面板用。

    返回各工具的调用次数、成功率、平均耗时，以及最近过期检测命中的次数。
    用于前端指标监控页的工具健康度面板。
    """
    try:
        def _work():
            app_instance = get_app_instance()
            if app_instance is None or not hasattr(app_instance, "platforms"):
                return {"available": False, "reason": "bot 未就绪"}

            tool_stats: dict[str, dict] = {}
            for platform_id, ctx in app_instance.platforms.items():
                store = getattr(ctx, "store", None)
                if store is None or not hasattr(store, "conn"):
                    continue
                try:
                    for row in store.get_tool_call_health():
                        name = row["tool_name"]
                        if name not in tool_stats:
                            tool_stats[name] = {
                                "tool_name": name,
                                "total_calls": 0,
                                "success_count": 0,
                                "avg_duration_ms": 0.0,
                                "last_call": "",
                                "platforms": [],
                            }
                        s = tool_stats[name]
                        s["total_calls"] += row["total"] or 0
                        s["success_count"] += row["ok"] or 0
                        avg_ms = row["avg_ms"] or 0
                        if avg_ms > 0:
                            s["avg_duration_ms"] = round(avg_ms, 1)
                        last = row["last_call"] or ""
                        if last > s["last_call"]:
                            s["last_call"] = last
                        if platform_id not in s["platforms"]:
                            s["platforms"].append(platform_id)

                    for s in tool_stats.values():
                        s["success_rate"] = round(
                            s["success_count"] / s["total_calls"] * 100, 1
                        ) if s["total_calls"] > 0 else 0.0
                except Exception:
                    logger.warning("平台 %s 工具统计读取失败", platform_id, exc_info=True)

            # 工具 TTL 配置（从 agent 的 _TOOL_RESULT_TTL 获取）
            ttl_config = {}
            try:
                agent = getattr(app_instance, "agent", None)
                if agent and hasattr(agent, "_TOOL_RESULT_TTL"):
                    ttl_config = {
                        k: v for k, v in agent._TOOL_RESULT_TTL.items() if v > 0
                    }
            except Exception:
                pass

            return {
                "available": True,
                "tools": list(tool_stats.values()),
                "ttl_config": ttl_config,
                "default_ttl": getattr(agent, "_TOOL_RESULT_DEFAULT_TTL", 600) if agent else 600,
            }
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("工具过期统计 API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Phase 3: 可观测性指标端点 ──────────────────────────────────────────


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


# ── 0. 全量指标快照 ────────────────────────────────────────────────────

@router.get("/api/metrics")
async def metrics_snapshot(
    time_range_hours: int = Query(default=24, ge=1, le=720),
):
    """全量可观测性指标快照（聚合所有平台）。
    # backend-only — 前端未消费，供 report_logger 对比校验或未来外部集成用。

    返回工具统计、路由准确率、黑名单趋势、Token 消费的完整快照，
    按平台分拆，用于仪表盘一次性拉取或 report_logger 对比校验。
    """
    try:
        from src.metrics.collector import MetricsCollector

        platforms_data: dict = {}
        totals: dict = {
            "tool": {"total_calls": 0, "success_count": 0},
            "routing": {"total": 0},
            "blacklist": {"total": 0},
            "token": {
                "total_tokens": 0, "total_input": 0, "total_output": 0, "total_cost_usd": 0.0,
            },
        }

        for pid, store in _iter_platform_stores():
            c = MetricsCollector(store)
            platforms_data[pid] = {
                "tool_stats": c.tool_stats(time_range_hours=time_range_hours),
                "tool_recent_failures": c.tool_recent_failures(limit=10),
                "routing_accuracy": c.routing_accuracy(),
                "blacklist_trends": c.blacklist_trends(),
                "token_stats": c.token_stats(time_range_hours=time_range_hours),
            }
            # Aggregate totals
            for t in platforms_data[pid]["tool_stats"]["tools"]:
                totals["tool"]["total_calls"] += t["total_calls"]
                totals["tool"]["success_count"] += t["success_count"]
            totals["routing"]["total"] += platforms_data[pid]["routing_accuracy"]["total"]
            totals["blacklist"]["total"] += platforms_data[pid]["blacklist_trends"]["total"]
            ts = platforms_data[pid]["token_stats"]
            totals["token"]["total_tokens"] += ts["total_tokens"]
            totals["token"]["total_input"] += ts["total_input_tokens"]
            totals["token"]["total_output"] += ts["total_output_tokens"]
            totals["token"]["total_cost_usd"] += ts["total_cost_usd"]

        totals["token"]["total_cost_usd"] = round(totals["token"]["total_cost_usd"], 6)

        return {
            "available": bool(platforms_data),
            "totals": totals,
            "platforms": platforms_data,
        }
    except Exception as e:
        logger.error("metrics snapshot API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 1. 工具调用统计 ────────────────────────────────────────────────────

@router.get("/api/metrics/tools")
async def metrics_tools(
    time_range_hours: int = Query(default=24, ge=0, le=720),
    limit: int = Query(default=30, ge=1, le=100),
):
    """工具调用统计（含 P50/P95/P99 延迟）。
    # backend-only — 前端当前消费 /api/stats/tools（stats.py），本端点未使用。

    - time_range_hours=0 表示不限时间范围
    """
    try:
        from src.metrics.collector import MetricsCollector

        result: dict[str, list] = {}
        for pid, store in _iter_platform_stores():
            c = MetricsCollector(store)
            tr = time_range_hours if time_range_hours > 0 else None
            result[pid] = c.tool_stats(time_range_hours=tr, limit=limit)["tools"]

        return {"available": bool(result), "time_range_hours": time_range_hours, "platforms": result}
    except Exception as e:
        logger.error("metrics tools API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/metrics/tools/failures")
async def metrics_tool_failures(
    limit: int = Query(default=20, ge=1, le=200),
):
    """最近 N 次工具调用失败详情。
    # backend-only — 前端未消费。
    """
    try:
        from src.metrics.collector import MetricsCollector

        result: dict[str, list] = {}
        for pid, store in _iter_platform_stores():
            c = MetricsCollector(store)
            result[pid] = c.tool_recent_failures(limit=limit)

        return {"available": bool(result), "platforms": result}
    except Exception as e:
        logger.error("metrics tool failures API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 2. 路由准确率看板 ──────────────────────────────────────────────────

@router.get("/api/metrics/routing")
async def metrics_routing(
    low_score_threshold: float = Query(default=0.5, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=200),
):
    """路由准确率看板。
    # backend-only — 前端当前消费 /api/routing-quality（routing_quality.py），本端点未使用。

    - 按 primary_source 统计准确率
    - 最近 N 条低分 decision 详情
    """
    try:
        from src.metrics.collector import MetricsCollector

        result: dict[str, dict] = {}
        for pid, store in _iter_platform_stores():
            c = MetricsCollector(store)
            result[pid] = c.routing_accuracy(
                low_score_threshold=low_score_threshold, limit=limit
            )

        return {"available": bool(result), "platforms": result}
    except Exception as e:
        logger.error("metrics routing API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 3. 黑名单趋势 ──────────────────────────────────────────────────────

@router.get("/api/metrics/blacklist")
async def metrics_blacklist(
    recent_days: int = Query(default=7, ge=1, le=365),
):
    """黑名单趋势。
    # backend-only — 前端未消费。

    - 永久 vs 临时黑名单数量对比
    - 按 reason 分类统计
    - 最近 N 天新增趋势
    """
    try:
        from src.metrics.collector import MetricsCollector

        result: dict[str, dict] = {}
        for pid, store in _iter_platform_stores():
            c = MetricsCollector(store)
            result[pid] = c.blacklist_trends(recent_days=recent_days)

        return {"available": bool(result), "platforms": result}
    except Exception as e:
        logger.error("metrics blacklist API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 4. Token 消费追踪 ──────────────────────────────────────────────────

@router.get("/api/metrics/tokens")
async def metrics_tokens(
    time_range_hours: int = Query(default=24, ge=0, le=720),
):
    """Token 消费追踪。
    # backend-only — 前端当前消费 /api/llm-metrics 中的 token 字段，本端点未使用。

    - 按时间聚合（最近 24h 逐小时）
    - 按 chat_id 聚合（Top 20）
    - time_range_hours=0 表示不限
    """
    try:
        from src.metrics.collector import MetricsCollector

        result: dict[str, dict] = {}
        for pid, store in _iter_platform_stores():
            c = MetricsCollector(store)
            tr = time_range_hours if time_range_hours > 0 else None
            result[pid] = c.token_stats(time_range_hours=tr)

        return {"available": bool(result), "time_range_hours": time_range_hours, "platforms": result}
    except Exception as e:
        logger.error("metrics tokens API 错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── 导出路由质量数据为 CSV ─────────────────────────────────────────────
_RQ_CSV_COLS = [
    "id", "sender_id", "sender_name", "conversation_id", "content_preview",
    "primary_skill", "primary_score", "primary_source", "combo_count",
    "combo_skills", "convergence_zone_size", "convergence_applied",
    "goal_fit_details", "tools_exposed", "routing_mode", "candidates_count",
    "intent_disposition", "intent_action", "intent_actions",
    "blocked_by_disabled_skill", "message_type", "llm_model",
    "llm_rounds", "llm_latency_ms", "total_latency_ms", "reply_len",
    "stages_json", "created_at",
]


@router.get("/api/metrics/export")
def export_metrics(time_range_hours: int = Query(default=0, ge=0), limit: int = Query(default=10000, le=20000)):
    """导出路由质量数据为 CSV（utf-8-sig BOM，Excel 兼容）。

    声明为同步 `def`：函数体全是阻塞的 DB 读取 + CSV 序列化（最多 2 万行），
    Starlette 会自动把同步路由放进线程池执行，不阻塞事件循环。
    """
    try:
        app = get_app_instance()
        if not app:
            raise HTTPException(status_code=503, detail="应用未就绪")

        limit = max(1, min(limit, 20000))
        all_rows = []

        for _pid, ctx in app.platforms.items():
            store = getattr(ctx, "store", None)
            if store is None or not hasattr(store, "_routing_quality_repo"):
                continue
            try:
                result = store._routing_quality_repo.get_routing_quality(
                    page=1, page_size=limit, primary_source="",
                )
                for r in result.get("items", []):
                    r["platform_id"] = _pid
                    all_rows.append(r)
            except Exception:
                logger.warning("平台 %s 路由质量数据读取失败", _pid, exc_info=True)

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(_RQ_CSV_COLS + ["platform_id"])
        for r in all_rows:
            # serialize JSON columns back to string
            row = []
            for k in _RQ_CSV_COLS:
                v = r.get(k, "")
                if k in ("combo_skills", "goal_fit_details", "tools_exposed", "stages_json", "blocked_by_disabled_skill", "intent_actions"):
                    if isinstance(v, (list, dict)):
                        v = _json.dumps(v, ensure_ascii=False)
                elif not isinstance(v, str):
                    v = str(v) if v != "" else ""
                row.append(v)
            row.append(r.get("platform_id", ""))
            writer.writerow(row)

        output.seek(0)
        date_tag = datetime.now().strftime("%Y%m%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=routing_quality_{date_tag}.csv"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("指标导出API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
