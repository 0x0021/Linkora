"""路由质量记录 / 统计路由。

从 `web/api.py` 抽取（原 3059–3103 行），业务逻辑不变。
get_store 取自 `web.dependencies`。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from web.dependencies import get_store, logger

router = APIRouter()


@router.get("/api/routing-quality")
async def routing_quality(
    page: int = 1,
    page_size: int = 20,
    primary_skill: str = "",
    primary_source: str = "",
    time_filter: str = "",
    blocked_filter: str = "",
    platform: str = "",
):
    """分页查询路由质量记录（Phase 4 收敛 + 组合 + 语义路由可观测）。

    time_filter: ''（全部）| 'today'（今天）| isodatetime（从此时间开始）
    blocked_filter: ''（全部）| 'blocked'（有屏蔽）| 'unblocked'（无屏蔽）
    """
    try:
        def _work():
            store = get_store()
            return store._routing_quality_repo.get_routing_quality(
                page=page,
                page_size=min(page_size, 100),
                primary_skill=primary_skill or None,
                primary_source=primary_source or None,
                time_filter=time_filter or None,
                blocked_filter=blocked_filter or None,
            )
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("路由质量API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/routing-quality/stats")
async def routing_quality_stats(platform: str = ""):
    """路由质量聚合统计。"""
    try:
        def _work():
            store = get_store()
            stats = store._routing_quality_repo.get_routing_quality_stats()
            # 补充筛选选项
            options = store._routing_quality_repo.get_filter_options()
            return {**stats, **options}
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("路由质量统计API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/routing-quality/aggregate")
async def routing_quality_aggregate(platform: str = ""):
    """系统级路由聚合：各阶段均值/瓶颈、健康分布、来源构成、延迟分桶、收敛/组合触发数。

    供「路由追踪」混合指挥中心视图的聚合链路流与可观测面板使用。
    """
    try:
        def _work():
            store = get_store()
            return store._routing_quality_repo.get_routing_quality_aggregate()
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("路由质量聚合API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/routing-quality/{rq_id}")
async def routing_quality_detail(rq_id: int):
    """单条路由追踪详情（含全链路瀑布 stages_json）用于路由追踪页弹窗。"""
    try:
        def _work():
            store = get_store()
            rec = store._routing_quality_repo.get_routing_quality_detail(rq_id)
            if rec is None:
                raise HTTPException(status_code=404, detail="记录不存在")
            return rec
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("路由质量详情API错误: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
