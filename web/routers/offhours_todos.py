"""工作时间外未回复消息 → 待办 的 Web API。

提供：筛选规则查看、未回复消息列表、待办创建记录、统计概览、手动触发扫描 /
同步状态、单条建单 / 忽略。所有阻塞操作经 ``run_sync`` 下线程池，避免卡死事件循环。

鉴权由 ``web/api.py`` 的全局中间件统一处理，本路由不重复实现。
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import web.api as _api
from src.im_adapter.errors import IMAdapterError
from src.offhours_todo import OffhoursTodoService
from web.dependencies import get_app_instance, get_current_platform, run_sync

logger = logging.getLogger("web.api")

router = APIRouter()


class ScanBody(BaseModel):
    create_todos: bool | None = None  # None=跟随配置 auto_create
    dry_run: bool = False


class KeyBody(BaseModel):
    dedupe_key: str


def _get_service() -> OffhoursTodoService:
    """取（或惰性构造）编排服务；每次同步运行期配置，使 Web 改规则即时生效。"""
    app = get_app_instance()
    if app is None:
        raise HTTPException(status_code=503, detail="应用实例不可用")
    cfg = _api._get_cfg()
    todo_cfg = getattr(cfg, "unreplied_todo", None)
    if todo_cfg is None:
        raise HTTPException(status_code=503, detail="配置未加载")
    service = getattr(app, "offhours_todo_service", None)
    if service is None:
        service = OffhoursTodoService(todo_cfg, app, app.platforms)
        app.offhours_todo_service = service
    else:
        service.cfg = todo_cfg
    return service


_CLI_ERRORS = (
    IMAdapterError, sqlite3.Error, OSError, ValueError, TypeError,
    RuntimeError, subprocess.SubprocessError,
)


@router.get("/api/offhours-todos/rules")
async def get_rules():
    """查看当前筛选规则（工作时间 / 工作日 / 节假日 / 通知策略）。"""
    try:
        return {"success": True, "rules": _get_service().get_rules()}
    except _CLI_ERRORS as e:
        logger.error("读取未回复待办规则失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/offhours-todos/unanswered")
async def list_unanswered(
    status: str = "all", chat_id: str = "",
    limit: int = 100, offset: int = 0,
):
    """分页列出当前平台的未回复消息（按消息时间倒序）。"""
    try:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        service = _get_service()
        result = await run_sync(
            service.list_unanswered,
            status=status, platform=get_current_platform(), chat_id=chat_id,
            limit=limit, offset=offset,
        )
        return {
            "success": True,
            "items": result["items"],
            "total": result["total"],
            "count": len(result["items"]),
        }
    except _CLI_ERRORS as e:
        logger.error("读取未回复消息失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/offhours-todos/records")
async def list_records(limit: int = 100, offset: int = 0):
    """分页列出当前平台的待办创建记录（含降级为仅通知的企微记录）。"""
    try:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        service = _get_service()
        result = await run_sync(
            service.list_records, platform=get_current_platform(), limit=limit, offset=offset
        )
        return {
            "success": True,
            "items": result["items"],
            "total": result["total"],
            "count": len(result["items"]),
        }
    except _CLI_ERRORS as e:
        logger.error("读取待办创建记录失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/offhours-todos/stats")
async def stats():
    """当前平台状态计数概览。"""
    try:
        service = _get_service()
        platform = get_current_platform()
        result = await run_sync(service.get_stats)
        return {"success": True, "stats": {platform: result.get(platform, {})}}
    except _CLI_ERRORS as e:
        logger.error("读取未回复待办统计失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/offhours-todos/scan")
async def trigger_scan(body: ScanBody | None = None):
    """手动触发扫描（与后台调度共用同一入口，靠去重键保证不重复建单）。"""
    payload = body or ScanBody()
    try:
        service = _get_service()
        result = await run_sync(
            service.scan_all,
            platform=get_current_platform(),
            create_todos=payload.create_todos,
            dry_run=payload.dry_run,
        )
        return {"success": not bool(result.get("error")), "result": result}
    except _CLI_ERRORS as e:
        logger.error("手动触发未回复待办扫描失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/offhours-todos/sync")
async def sync_status():
    """同步状态：重新扫描一遍并回填已由真人回复的记录（dry-run 不建单）。"""
    try:
        service = _get_service()
        result = await run_sync(
            service.scan_all, platform=get_current_platform(),
            dry_run=False, create_todos=False,
        )
        return {"success": True, "result": result}
    except _CLI_ERRORS as e:
        logger.error("同步未回复状态失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/offhours-todos/create")
async def create_todo_for_message(body: KeyBody):
    """对单条未回复消息手动建单（忽略 auto_create 开关）。"""
    try:
        service = _get_service()
        result = await run_sync(service.create_for_key, body.dedupe_key)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "建单失败"))
        return {"success": True, **result}
    except HTTPException:
        raise
    except _CLI_ERRORS as e:
        logger.error("手动建单失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/offhours-todos/ignore")
async def ignore_message(body: KeyBody):
    """忽略单条未回复消息（不再建单）。"""
    try:
        service = _get_service()
        result = await run_sync(service.ignore_for_key, body.dedupe_key)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "忽略失败"))
        return {"success": True}
    except HTTPException:
        raise
    except _CLI_ERRORS as e:
        logger.error("忽略未回复消息失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
