"""部门架构 / 历史消息导入路由。

从 `web/api.py` 抽取（原 3765–3998 行，含部门缓存与 DWS 封装 helpers），
业务逻辑不变。共享符号 logger / _get_project_root 取自 `web.dependencies`；
DWS 封装（_run_dws / _is_token_verified_error / _cache_*）随本模块内聚，
不反向依赖 `web.api`，避免循环导入。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time as _time

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from web.dependencies import _get_project_root, logger
from web.errors import SAFE_OPERATION_FAILED

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


@router.post("/api/history/import")
async def import_history_messages(full: bool = False):
    """触发历史消息导入（增量或全量）。

    - full=False: 增量导入（从上次导入时间开始）
    - full=True: 全量导入（拉取过去 90 天）
    """
    try:
        import subprocess

        project_root = _get_project_root()
        script_path = project_root / "import_history.py"
        venv_python = project_root / ".venv" / "bin" / "python"

        if full:
            cmd = [str(venv_python), str(script_path), "--full"]
        else:
            cmd = [str(venv_python), str(script_path)]

        # 异步执行（不阻塞 API 响应）
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=300  # 5 分钟超时
            )
        )

        if result.returncode == 0:
            return {
                "success": True,
                "message": "历史消息导入完成",
                "output": result.stdout[-500:]  # 只返回最后 500 字符
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=f"导入失败: {result.stderr[-500:]}"
            ) from None
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="导入超时（超过 5 分钟）") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/history/import/status")
async def get_import_status():
    """获取历史消息导入状态。"""
    try:
        import json
        from pathlib import Path

        from src.config import DEFAULT_DATA_DIR

        state_file = Path(DEFAULT_DATA_DIR) / "import_history_state.json"
        if state_file.exists():
            def _read_state():
                with open(state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            state = await run_in_threadpool(_read_state)
            return {"success": True, "state": state}
        else:
            return {"success": True, "state": None, "message": "尚未导入历史消息"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
