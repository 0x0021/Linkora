"""组织 / 跨组织会话路由。

从 `web/api.py` 抽取（原 2494–2534 行），业务逻辑不变。
仅依赖 `get_app_instance`（取自 `web.dependencies`），不反向依赖 `web.api`，避免循环导入。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from web.dependencies import get_app_instance

router = APIRouter()


@router.get("/api/orgs")
async def get_orgs():
    """列出已登录 DWS 的组织 + 当前/目标组织 + 已跳过(跨组织)会话数 + 熔断状态。"""
    try:
        app_instance = get_app_instance()
        poller = app_instance.poller if app_instance and hasattr(app_instance, "poller") else None
        if poller is None:
            raise HTTPException(status_code=503, detail="轮询器未启动")
        orgs = poller.dws.list_orgs()
        current = poller.current_org if hasattr(poller, "current_org") else poller.dws.get_current_org()
        target = getattr(poller, "target_org_corp_id", "") or ""
        skipped = len(poller._inaccessible_conversations)
        return {
            "orgs": orgs,
            "current": current,
            "target": target,
            "skipped_count": skipped,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/clear-cross-org-skips")
async def clear_cross_org_skips():
    """清除跨组织/无效会话跳过名单，下一轮重新探测。"""
    try:
        app_instance = get_app_instance()
        poller = app_instance.poller if app_instance and hasattr(app_instance, "poller") else None
        if poller is None:
            raise HTTPException(status_code=503, detail="轮询器未启动")
        cleared_conv = poller.clear_cross_org_skips()
        return {
            "cleared_conversations": cleared_conv,
            "skipped_count": len(poller._inaccessible_conversations),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
