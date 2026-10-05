"""部门架构 / 历史消息导入路由。

从 `web/api.py` 抽取（原 3765–3998 行，含部门缓存与 DWS 封装 helpers），
业务逻辑不变。共享符号 logger / _get_project_root 取自 `web.dependencies`；
DWS 封装（_run_dws / _is_token_verified_error / _cache_*）随本模块内聚，
不反向依赖 `web.api`，避免循环导入。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time as _time
import uuid

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from web.dependencies import get_app_instance, get_current_platform, logger
from web.errors import SAFE_OPERATION_FAILED
from src.paths import data_path, get_config_path
from src.constants import SUPPORTED_PLATFORMS

router = APIRouter()

_DEPT_CACHE: dict = {}
_DEPT_CACHE_TTL = 300  # 5 分钟


def _is_token_verified_error(e: Exception) -> bool:
    err_str = str(e)
    return any(code in err_str for code in (
        "TOKEN_VERIFIED_FAILED",
        "该组织尚未开启 CLI 数据访问权限",
        "business error",
    ))


async def _run_dws(args: list, timeout: int = 15) -> dict:
    """在线程池中执行 DWS CLI 命令，返回解析后的 JSON dict。"""
    loop = asyncio.get_event_loop()
    proc = await asyncio.wait_for(
        loop.run_in_executor(
            None,
            lambda: subprocess.run(
                args, capture_output=True, text=True, timeout=timeout
            )
        ),
        timeout=float(timeout),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"DWS 命令失败: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)

def _cache_get(key: str):
    item = _DEPT_CACHE.get(key)
    if not item:
        return None
    if _time.time() - item["ts"] > _DEPT_CACHE_TTL:
        return None
    return item["data"]


def _cache_set(key: str, data):
    _DEPT_CACHE[key] = {"ts": _time.time(), "data": data}


@router.get("/api/departments/tree")
async def get_department_tree():
    """获取钉钉部门架构：顶级部门列表（懒加载，不含子部门/成员）。"""
    try:
        cached = _cache_get("tree_root")
        if cached is not None:
            return {"success": True, "tree": cached, "cached": True}

        data = await _run_dws([
            "dws", "contact", "dept", "list-children",
            "--id", "1", "--format", "json", "--timeout", "15",
        ])
        result = data.get("result") or []
        if not result:
            return {"success": False, "error": "未获取到顶级部门"}

        tree = [
            {
                "id": d.get("deptId"),
                "name": d.get("deptName", "未知部门"),
                "member_count": 0,
                "has_children": True,
                "members": [],
                "children": [],
            }
            for d in result
        ]
        _cache_set("tree_root", tree)
        logger.info(f"[部门架构] 顶级部门 {len(tree)} 个")
        return {"success": True, "tree": tree, "cached": False}
    except asyncio.TimeoutError:
        logger.warning("[部门架构] 获取顶级部门超时")
        return {"success": False, "error": "获取部门列表超时"}
    except Exception as e:
        if _is_token_verified_error(e):
            logger.warning("[部门架构] 无权限访问 contact 接口，跳过")
            return {
                "success": False,
                "error": "当前组织未开启 CLI 数据访问权限，部门架构暂不可用",
                "code": "permission_denied",
            }
        logger.error(f"获取部门架构失败: {e}")
        return {"success": False, "error": SAFE_OPERATION_FAILED}


@router.get("/api/departments/{dept_id}/children")
async def get_department_children(dept_id: int):
    """懒加载：获取指定部门的子部门列表。"""
    try:
        cache_key = f"children_{dept_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {"success": True, "children": cached, "cached": True}

        data = await _run_dws([
            "dws", "contact", "dept", "list-children",
            "--id", str(dept_id), "--format", "json", "--timeout", "15",
        ])
        result = data.get("result") or []
        children = [
            {
                "id": d.get("deptId"),
                "name": d.get("deptName", "未知部门"),
                "member_count": 0,
                "has_children": True,
                "members": [],
                "children": [],
            }
            for d in result
        ]
        _cache_set(cache_key, children)
        return {"success": True, "children": children, "cached": False}
    except asyncio.TimeoutError:
        return {"success": False, "error": "获取子部门超时"}
    except Exception as e:
        if _is_token_verified_error(e):
            return {"success": False, "error": "无权限访问", "code": "permission_denied"}
        logger.error(f"获取部门 {dept_id} 子部门失败: {e}")
        return {"success": False, "error": SAFE_OPERATION_FAILED}


@router.get("/api/departments/{dept_id}/members")
async def get_department_members(dept_id: int):
    """懒加载：获取指定部门的成员列表。"""
    try:
        cache_key = f"members_{dept_id}"
        cached = _cache_get(cache_key)
        if cached is not None:
            return {"success": True, "members": cached, "count": len(cached), "cached": True}

        data = await _run_dws([
            "dws", "contact", "dept", "list-members",
            "--ids", str(dept_id), "--format", "json", "--timeout", "20",
        ])
        user_list = data.get("deptUserList") or []
        members = []
        for item in user_list:
            ui = item.get("userInfo") or {}
            members.append({
                "user_id": ui.get("userId"),
                "name": ui.get("name", "未知"),
                "avatar": ui.get("avatarUrl", ""),
                "title": ui.get("title", ""),
                "email": ui.get("email", ""),
                "mobile": ui.get("mobile", ""),
            })
        _cache_set(cache_key, members)
        return {"success": True, "members": members, "count": len(members), "cached": False}
    except asyncio.TimeoutError:
        return {"success": False, "error": "获取部门成员超时"}
    except Exception as e:
        if _is_token_verified_error(e):
            return {"success": False, "error": "无权限访问", "code": "permission_denied"}
        logger.error(f"获取部门 {dept_id} 成员失败: {e}")
        return {"success": False, "error": SAFE_OPERATION_FAILED}


@router.post("/api/departments/cache/clear")
async def clear_department_cache():
    """清除部门架构缓存。"""
    _DEPT_CACHE.clear()
    return {"success": True, "message": "部门缓存已清除"}


# 状态文件与 src/platform/sync_history.py、web/routers/sync.py 保持一致
# （旧实现另写 import_history_state.json 的脚本已被移除，此处统一复用同步任务状态文件）
_IMPORT_STATUS_FILE = str(data_path("sync_history_status.json"))


def _read_import_status() -> dict:
    """读取导入/同步任务状态文件（在同步上下文中调用，由端点经 run_in_threadpool 包装）。"""
    try:
        if not os.path.exists(_IMPORT_STATUS_FILE):
            return {}
        with open(_IMPORT_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _is_import_status_stale(s: dict) -> bool:
    """状态仍 running/starting 但超过 2 小时未更新 → 视为 stale（线程已死却未刷新状态）。"""
    started = s.get("started_at")
    if not isinstance(started, (int, float)):
        return False
    return (_time.time() - started) > 7200


@router.post("/api/history/import")
async def import_history_messages(full: bool = False):
    """触发历史消息导入（增量或全量）。

    复用 ``src.platform.sync_history.run_sync_history``（与 ``/api/messages/sync-history``
    同一套机制），在**进程内后台线程**执行，不再 spawn 独立子进程。旧实现引用的
    ``import_history.py`` 已被移除（且该子进程方案在 PyInstaller 冻结态不可用）。

    写库走 Web 进程内 SQLiteStore，与 worker 共享写闸门；单进程部署下完全同进程，
    不再引入第三个独立写进程，消除跨进程写锁争用。

    - full=False: 增量导入（最近 30 天）
    - full=True: 全量导入（逐 30 天窗拉取全部历史）
    """
    request_platform = (get_current_platform() or "dingtalk").strip() or "dingtalk"
    if request_platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"未知平台: {request_platform}")

    # 轻量前置校验：平台是否启用（真正的适配器错误由 worker 回报状态文件）
    app_instance = get_app_instance()
    if app_instance is not None and hasattr(app_instance, "platforms"):
        ctx = app_instance.platforms.get(request_platform)
        if ctx is None:
            raise HTTPException(
                status_code=400,
                detail=f"平台 {request_platform} 未启用或未配置，请在 config.yaml 的 platforms 段中添加",
            ) from None
        if not getattr(ctx, "enabled", True):
            raise HTTPException(
                status_code=400,
                detail=f"平台 {request_platform} 已禁用",
            ) from None

    # 并发护栏：已有进行中的导入/同步（且未 stale）时拒绝重复启动
    _cur = await run_in_threadpool(_read_import_status)
    if _cur.get("status") in ("running", "starting") and not _is_import_status_stale(_cur):
        raise HTTPException(
            status_code=409,
            detail=f"已有导入/同步任务进行中（job_id={_cur.get('job_id')}），请等待完成或先取消",
        ) from None

    days = 30
    range_label = "全部历史" if full else "最近30天"
    job_id = f"import_{uuid.uuid4().hex[:12]}"
    resolved_config_path = str(get_config_path())

    # 写初始状态，保证前端首轮轮询就能读到 starting
    try:
        os.makedirs(os.path.dirname(_IMPORT_STATUS_FILE), exist_ok=True)

        def _write_initial_status() -> None:
            with open(_IMPORT_STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "job_id": job_id, "status": "starting", "platform": request_platform,
                    "days": days, "scope": "global", "range": range_label,
                    "progress": "排队中", "result": None,
                    "error": None, "started_at": _time.time(),
                }, f, ensure_ascii=False, indent=2)

        await run_in_threadpool(_write_initial_status)
    except Exception as e:  # noqa: BLE001
        logger.warning("[导入] 写入初始状态失败（不影响启动）: %s", e)

    # 延迟 import 避免 web 启动时无谓加载 dws / sqlite / poller
    from src.platform.sync_history import run_sync_history

    def _runner() -> None:
        try:
            run_sync_history(
                days=days,
                platform=request_platform,
                job_id=job_id,
                scope="global",
                range_label=range_label,
                full=full,
                conversation_id="",
                chat_types=None,
                config_path=resolved_config_path,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("[导入] worker 线程异常: %s", e, exc_info=True)

    t = threading.Thread(target=_runner, name=f"import-history-{job_id}", daemon=False)
    t.start()
    logger.info(
        "[导入] 启动历史消息导入 job_id=%s platform=%s range=%s full=%s",
        job_id, request_platform, range_label, full,
    )
    return {
        "success": True,
        "job_id": job_id,
        "status": "started",
        "platform": request_platform,
        "range": range_label,
        "full": full,
    }


@router.get("/api/history/import/status")
async def get_import_status():
    """获取历史消息导入状态（与同步任务同源，读 sync_history_status.json）。"""
    try:
        state = await run_in_threadpool(_read_import_status)
        if state:
            return {"success": True, "state": state}
        return {"success": True, "state": None, "message": "尚未导入历史消息"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
