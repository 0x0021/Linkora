"""决策追踪 / 历史 / 统计路由。

从 `web/api.py` 抽取（原 2985–3057 行），业务逻辑不变。
- get_store 取自 `web.dependencies`；
- recent_decisions 仍于 handler 内 `from src.decision_tracker import tracker`
  （测试 patch 目标 `src.decision_tracker.tracker` 不变）。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from src.memory.decisions_repo import DecisionsRepo
from web.dependencies import get_store, logger

router = APIRouter()


@router.get("/api/decisions")
async def recent_decisions(n: int = 50, platform: str = ""):
    """返回最近 n 条消息处理决策（意图判定 + 处理动作 + 工具路由）。

    用于管理端「意图 & 路由」面板实时观测：某条消息为何被跳过 / 调了哪些工具。
    数据来自进程内决策追踪器（有界队列，重启即清空）。

    Args:
        platform: 平台 ID（如 dingtalk/feishu），指定时从对应平台 store 读取持久化数据；
            为空时返回内存中所有数据（向后兼容）。
    """
    try:
        def _work():
            # 指定 platform 时 tracker.recent 会回落到 store 读持久化数据（同步 DB），故整体入线程池。
            from src.decision_tracker import tracker
            result = tracker.recent(n, platform)
            return {"decisions": result, "total": len(result)}
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("决策追踪API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/decisions/history")
async def decisions_history(
    page: int = 1,
    page_size: int = 20,
    sender_name: str = "",
    conversation_id: str = "",
    intent: str = "",
    action: str = "",
    platform: str = "",
    time_filter: str = "",
):
    """分页查询持久化决策历史，支持按 sender_name / conversation_id / intent / action / time_filter 过滤。

    数据来自 SQLite decisions 表，进程重启不丢失。
    time_filter: ''（全部）| 'today'（今天）| 'month'（本月）
    """
    try:
        def _work():
            store = get_store(platform)
            return store._decisions_repo.get_decisions(
                page=page,
                page_size=min(page_size, 100),
                sender_name=sender_name or None,
                conversation_id=conversation_id or None,
                intent=intent or None,
                action=action or None,
                time_filter=time_filter or None,
            )
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("决策历史API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/decisions/stats")
async def decisions_stats(platform: str = ""):
    """决策统计：各意图/动作计数，供子页面概览。"""
    try:
        def _work():
            store = get_store(platform)
            stats = store._decisions_repo.get_decisions_stats()
            # 补充 sender / intent / action 列表供筛选下拉
            options = store._decisions_repo.get_filter_options()
            return {**stats, **options}
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("决策统计API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 导出决策记录为 CSV ─────────────────────────────────────────────────
# 列定义与顺序以 DecisionsRepo.EXPORT_COLUMNS 为准，避免表头与查询列漂移。
_DECISION_CSV_COLS = list(DecisionsRepo.EXPORT_COLUMNS)


@router.get("/api/decisions/export")
async def export_decisions(
    sender_name: str = "",
    conversation_id: str = "",
    intent: str = "",
    action: str = "",
    time_filter: str = "",
    platform: str = "",
    limit: int = 10000,
):
    """导出决策记录为 CSV（utf-8-sig BOM，Excel 兼容）。"""
    try:
        limit = max(1, min(limit, 20000))

        def _work():
            store = get_store(platform)
            return store._decisions_repo.export_decisions(
                sender_name=sender_name or None,
                conversation_id=conversation_id or None,
                intent=intent or None,
                action=action or None,
                time_filter=time_filter or None,
                limit=limit,
            )

        rows = await run_in_threadpool(_work)

        output = io.StringIO()
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(_DECISION_CSV_COLS)
        for r in rows:
            writer.writerow([r[k] for k in _DECISION_CSV_COLS])

        output.seek(0)
        date_tag = datetime.now().strftime("%Y%m%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=decisions_{date_tag}.csv"},
        )
    except Exception as e:
        logger.error("决策导出API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
