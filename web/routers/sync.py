"""手动同步历史消息路由（多平台适配）。

从 ``web/api.py`` 抽取（原 876–900 行）。
get_app_instance 取自 web.dependencies。

设计变更：
- ``POST /api/messages/sync-history`` 不在请求线程内同步调用 ``poller.sync_history()``
  （重 I/O + 写库，会卡死事件循环），而是立即启动一个**进程内后台线程**
  （``src.platform.sync_history.run_sync_history``），由该线程完成 bootstrap + 同步，
  并写入 ``data/sync_history_status.json``。端点秒回 ``{success, job_id, status:"started"}``，
  前端据此轮询状态，UI 不再卡死。
- 线程模式下 worker 与 Web 共享进程，WAL + busy_timeout=5000 让 SQLiteStore 并发写安全。
- 取消机制：cancel 端点写 ``CANCEL_FILE``，线程 worker 每个时间窗前读取，窗边界干净退出。
- 旧实现（独立子进程 ``scripts/sync_history_worker.py``）在 PyInstaller 冻结态不可用
  （脚本未被 spec 打包、``sys.executable`` 是二进制本身），已废弃；脚本保留为 CLI 薄壳
  供 dev 手动调试 / 自动化脚本使用。
- ``GET /api/messages/sync-history/status`` 读取状态文件，返回进度/结果。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from web.dependencies import get_app_instance, get_current_platform, logger
from web.errors import SAFE_OPERATION_FAILED
from src.paths import data_path, get_config_path, get_log_dir
from src.constants import SUPPORTED_PLATFORMS

router = APIRouter()


class SyncHistoryRequest(BaseModel):
    """手动同步历史的请求体。

    scope:
        "current" → 仅同步当前打开会话（需带 conversation_id）；
        "global"  → 全局同步（可按 chat_types 筛选）。
    range:
        "7" / "30" / "90" → 最近 N 天；"all" → 全部历史（逐 30 天窗，绕开 20 页上限）。
    chat_types:
        ["single"] / ["group"] / ["single","group"] → 类型筛选；空/None → 全部。
    """

    scope: str = "global"
    conversation_id: Optional[str] = None
    range: str = "7"
    chat_types: Optional[list[str]] = None

# 状态文件：与 src/platform/sync_history.py 中的 STATUS_FILE 保持一致（可重定位）
STATUS_FILE = str(data_path("sync_history_status.json"))

# worker 日志文件：与 src/platform/sync_history.py 中的 LOG_FILE 保持一致（前端实时读取尾部）
WORKER_LOG_FILE = str(get_log_dir() / "sync_history_worker.log")

# 取消信号文件：cancel 端点写入，worker 每个时间窗前读取
CANCEL_FILE = str(data_path("sync_history_cancel.json"))

# 当前正在跑的同步线程（job_id → Thread）。线程模式下唯一可靠的「是否在跑」信号
# ——状态文件可能因崩溃没及时更新，但 thread.is_alive() 永远反映真实状态。
_active_threads: dict[str, threading.Thread] = {}
_active_threads_lock = threading.Lock()


def _thread_alive(job_id: str | None) -> bool:
    """判断指定 job 的 worker 线程是否还活着。"""
    if not job_id:
        return False
    with _active_threads_lock:
        t = _active_threads.get(job_id)
        if t is None:
            return False
        alive = t.is_alive()
        if not alive:
            # 顺手清掉死引用，避免 dict 越来越大
            _active_threads.pop(job_id, None)
        return alive


def _is_running_stale(status: dict) -> bool:
    """状态显示 running/starting 但 worker 已死（线程退出/崩溃未更新状态）→ 视为过期，允许新 job。"""
    if not status or status.get("status") not in ("running", "starting"):
        return False
    return not _thread_alive(status.get("job_id"))

# 允许的合法平台——单一真源见 src/constants.SUPPORTED_PLATFORMS
# （不再在 web 层另持副本，避免加平台时漏改一处导致漂移）。


def _read_sync_status() -> dict:
    try:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("[同步] 读取状态文件失败: %s", e)
    return {"job_id": None, "status": "idle", "platform": "", "days": 0,
            "progress": "", "result": None, "error": None}


@router.post("/api/messages/sync-history")
async def sync_history(req: SyncHistoryRequest, platform: str = Query(default="")):
    """手动同步历史消息（异步：启动进程内后台线程，立即返回 job_id）。

    请求体字段见 :class:`SyncHistoryRequest`。支持三种模式：
    - 仅当前会话（scope=current + conversation_id）
    - 全局最近 N 天（range=7/30/90）
    - 全局全部历史（range=all，逐 30 天窗绕开 20 页上限）

    同步在进程内后台线程执行，不阻塞 Web 事件循环；前端用 job_id 轮询状态。
    dev/frozen 都通用（旧独立子进程方案在 PyInstaller 冻结态不可用，已废弃）。
    """
    request_platform = (platform or get_current_platform()).strip()
    if not request_platform:
        request_platform = "dingtalk"
    if request_platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"未知平台: {request_platform}")
    # 平台启用性校验（仅做轻量前置检查，真正的 DWS/适配器错误由 worker 回报状态文件）
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
                detail=f"平台 {ctx.display_name or request_platform} 已禁用",
            ) from None

    # 解析 range → days / full
    range_raw = (req.range or "7").strip().lower()
    full = range_raw == "all"
    if full:
        days = 30  # full 模式忽略 days，传一个合法默认值即可
        range_label = "全部历史"
    else:
        try:
            days = max(1, min(int(range_raw), 365))
        except ValueError:
            days = 7
        range_label = f"最近{days}天"

    # 解析 scope → conversation_id
    scope_label = "current" if req.scope == "current" else "global"
    conv_id = req.conversation_id.strip() if (req.scope == "current" and req.conversation_id) else ""
    if req.scope == "current" and not conv_id:
        raise HTTPException(status_code=400, detail="同步当前会话需要传入 conversation_id")
    chat_types = [t for t in (req.chat_types or []) if t in ("single", "group")]

    job_id = f"sync_{uuid.uuid4().hex[:12]}"
    # 配置路径走 P0 可重定位解析：dev 态 = cwd/config.yaml（若存在），否则用户数据目录
    resolved_config_path = str(get_config_path())

    # 并发护栏：已有进行中的同步（且 worker 线程仍存活）时拒绝重复启动，
    # 避免状态文件被覆盖、两个 job 并发写库互相干扰。线程已死但未更新状态（stale）则放行新 job。
    _cur = await run_in_threadpool(_read_sync_status)
    if _cur.get("status") in ("running", "starting") and not _is_running_stale(_cur):
        raise HTTPException(
            status_code=409,
            detail=f"已有同步任务进行中（job_id={_cur.get('job_id')}），请等待完成或先取消",
        ) from None

    # 写初始状态，保证前端首轮轮询就能读到 starting
    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        def _write_initial_status():
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "job_id": job_id, "status": "starting", "platform": request_platform,
                    "days": days, "scope": scope_label, "range": range_label,
                    "chat_types": ",".join(chat_types), "progress": "排队中", "result": None,
                    "error": None, "started_at": time.time(),
                }, f, ensure_ascii=False, indent=2)
        await run_in_threadpool(_write_initial_status)
    except Exception as e:  # noqa: BLE001
        logger.warning("[同步] 写入初始状态失败（不影响启动）: %s", e)

    # 启动进程内后台线程：跑 src.platform.sync_history.run_sync_history
    # 线程内创建独立 SQLiteStore 连接（WAL + busy_timeout=5000 与主 poller 并发安全）。
    from src.platform.sync_history import run_sync_history  # 延迟 import 避免 web 启动时无谓加载

    def _runner() -> None:
        try:
            run_sync_history(
                days=days,
                platform=request_platform,
                job_id=job_id,
                scope=scope_label,
                range_label=range_label,
                full=full,
                conversation_id=conv_id,
                chat_types=chat_types or None,
                config_path=resolved_config_path,
            )
        except Exception as e:  # noqa: BLE001
            # run_sync_history 内部已经处理了所有已知异常并写状态；这是双保险
            logger.error("[同步] worker 线程异常: %s", e, exc_info=True)

    t = threading.Thread(
        target=_runner,
        name=f"sync-history-{job_id}",
        daemon=False,  # 非 daemon：等线程自然结束（主进程退出时不会硬切）
    )
    with _active_threads_lock:
        # 清理上一次已死的 thread 引用，避免 dict 累积
        for jid in list(_active_threads.keys()):
            if not _active_threads[jid].is_alive():
                _active_threads.pop(jid, None)
        _active_threads[job_id] = t
    t.start()

    logger.info(
        "[同步] 启动手动同步 job_id=%s thread=%s platform=%s scope=%s range=%s chat_types=%s conv_id=%s",
        job_id, t.name, request_platform, scope_label, range_label,
        ",".join(chat_types) or "全部", conv_id or "-",
    )

    return {
        "success": True,
        "job_id": job_id,
        "status": "started",
        "platform": request_platform,
        "scope": scope_label,
        "range": range_label,
        "conversation_id": conv_id,
        "chat_types": chat_types,
    }


@router.get("/api/messages/sync-history/status")
async def sync_history_status():
    """查询最近一次手动同步的进度/结果（前端轮询）。"""
    return await run_in_threadpool(_read_sync_status)


@router.get("/api/messages/sync-history/log")
async def sync_history_log(lines: int = 80):
    """返回同步 worker 日志文件末尾 N 行，供前端实时展示进度。

    worker 在独立子进程中运行、日志只写 ``logs/sync_history_worker.log``，
    主进程终端看不到。此端点让前端「同步中心」弹窗实时拉取日志尾部，
    用户无需 SSH 到服务器 ``tail -f`` 即可确认同步是否在进行。
    """
    try:
        def _read_log_tail():
            if not os.path.exists(WORKER_LOG_FILE):
                return {"lines": [], "exists": False, "total": 0}
            with open(WORKER_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            all_lines = content.splitlines()
            n = max(1, min(int(lines), 2000))
            tail = all_lines[-n:]
            return {"lines": tail, "exists": True, "total": len(all_lines)}
        return await run_in_threadpool(_read_log_tail)
    except Exception as e:  # noqa: BLE001
        logger.warning("[同步] 读取 worker 日志失败: %s", e)
        return {"lines": [], "exists": False, "error": SAFE_OPERATION_FAILED}


@router.post("/api/messages/sync-history/cancel")
async def sync_history_cancel(platform: str = Query(default="")):
    """取消正在进行的手动同步历史。

    做法：
    1. 写取消标记文件 ``data/sync_history_cancel.json``，worker 每个时间窗前读取，于窗边界干净退出。
    2. 线程模式下不再尝试 ``os.kill``（会误杀 Web 自身进程），仅靠 CANCEL_FILE 机制；旧子进程模式下
       若 status 里有 worker PID 且不是当前进程，再 SIGTERM（兼容性保留）。

    返回 ``{success, cancelled}``：cancelled=True 表示确有进行中的任务被取消；False 表示当前无进行中任务。
    """
    request_platform = (platform or get_current_platform()).strip() or "dingtalk"
    st = await run_in_threadpool(_read_sync_status)
    status = st.get("status")
    if status not in ("running", "starting"):
        return {"success": True, "cancelled": False, "status": status or "idle"}

    # 1) 写取消标记（worker 每窗检测）
    try:
        job_id_for_cancel = st.get("job_id")
        def _write_cancel_marker():
            os.makedirs(os.path.dirname(CANCEL_FILE), exist_ok=True)
            with open(CANCEL_FILE, "w", encoding="utf-8") as f:
                json.dump({"cancel": True, "at": time.time(), "job_id": job_id_for_cancel}, f)
        await run_in_threadpool(_write_cancel_marker)
    except Exception as e:  # noqa: BLE001
        logger.warning("[同步] 写入取消标记失败: %s", e)

    # 2) best-effort kill（仅旧子进程模式有效，线程模式不写 pid，自然跳过此分支）
    pid = st.get("pid")
    killed = False
    thread_alive = _thread_alive(st.get("job_id"))
    if pid and not thread_alive and pid != os.getpid():
        try:
            os.kill(int(pid), 15)  # SIGTERM
            killed = True
        except Exception as e:  # noqa: BLE001
            logger.warning("[同步] kill worker(pid=%s) 失败: %s", pid, e)

    logger.info(
        "[同步] 取消请求 job_id=%s pid=%s thread_alive=%s killed=%s platform=%s",
        st.get("job_id"), pid, thread_alive, killed, request_platform,
    )
    return {"success": True, "cancelled": True, "status": status, "pid": pid, "killed": killed}
