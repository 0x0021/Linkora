"""实时日志路由。

从 `web/api.py` 抽取（原 559–580 行），业务逻辑不变。
- get_log_buffer 取自 src.utils.logger；
- _LEVEL_MAP 常量随本模块一并迁入（仅被本路由使用）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.utils.logger import get_log_buffer

router = APIRouter()

_LEVEL_MAP = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


@router.get("/api/logs")
async def get_logs(level: str = "info", since: int = 0, limit: int = 200,
                   platform: str = "all"):
    """实时日志：返回内存缓冲区中自 since 之后的日志（按 level 与 platform 过滤）。

    platform 来自 Web 平台上下文（前端 api.fetch 自动追加 ?platform=当前平台），
    默认 "all" 表示不过滤（兼容非 Web/直连调用）。按平台隔离时，中性（共享核心/LLM
    等）日志始终保留，仅隐藏其它平台的专属适配器日志，减少跨平台噪声。
    """
    limit = max(1, min(limit, 500))
    buf = get_log_buffer()
    # 防御性：确保 buffer handler 已挂载到 root
    # （防止 uvicorn 启动时重置 logging 配置导致 handler 丢失）
    root = logging.getLogger()
    if buf not in root.handlers:
        buf.setLevel(logging.DEBUG)
        root.addHandler(buf)
    lvl = _LEVEL_MAP.get(level.upper(), 20)
    try:
        logs = buf.get_records(level_no=lvl, since_id=since, limit=limit, platform=platform)
        buf_total = buf.count(level_no=lvl, platform=platform)
        max_id = buf.max_id()
    except Exception as e:
        return JSONResponse({"logs": [], "total": 0, "buffer_total": 0, "max_id": 0, "error": str(e)})
    return JSONResponse({"logs": logs, "total": len(logs), "buffer_total": buf_total, "max_id": max_id})
