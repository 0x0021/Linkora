"""综合健康检查路由。

从 `web/api.py` 抽取（原 3627–3670 行），业务逻辑不变。
- get_store 取自 `web.dependencies`；
- 配置经 `_api._get_cfg()` 读取（单例优先 + 磁盘 mtime 缓存兜底，统一真源）；
  tests 会 monkeypatch 该全局，必须取实时值而非导入期绑定。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from web.dependencies import get_store, get_current_platform
from web.errors import SAFE_OPERATION_FAILED
import web.api as _api

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health():
    """综合健康检查端点，返回系统各组件状态。"""
    result = {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "components": {},
    }

    # 1. 数据库连接检查
    try:
        def _db_check():
            store = get_store()
            store._message_repo.count_dedup_messages(platform=get_current_platform())
        await run_in_threadpool(_db_check)
        result["components"]["database"] = {"status": "healthy"}
    except Exception as e:
        logger.error("数据库健康检查失败: %s", e)
        result["components"]["database"] = {"status": "unhealthy", "error": SAFE_OPERATION_FAILED}
        result["status"] = "degraded"

    # 2. 配置文件可读性检查
    try:
        _api._get_cfg()
        result["components"]["config"] = {"status": "readable"}
    except Exception as e:
        logger.error("配置可读性检查失败: %s", e)
        result["components"]["config"] = {"status": "unreadable", "error": SAFE_OPERATION_FAILED}
        result["status"] = "degraded"

    # 4. 最近消息处理时间（从 dedup_messages 表取最新记录）
    try:
        def _last_msg():
            store = get_store()
            return store._message_repo.get_last_processed_at(platform=get_current_platform())
        result["last_message_processed"] = await run_in_threadpool(_last_msg)
    except Exception:
        result["last_message_processed"] = None

    return result
