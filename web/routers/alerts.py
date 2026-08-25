"""告警日志 API 路由。

提供告警历史查询、实时告警推送、告警统计等功能。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from src.alerts.manager import AlertManager, get_alert_manager
from web.auth_middleware import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alerts", tags=["告警"])


@router.get("/stats")
async def get_alert_stats(
    manager: AlertManager = Depends(get_alert_manager),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取告警统计信息。

    Returns:
        告警统计，包含各错误类型的计数、严重程度等。
    """
    return manager.get_stats()


@router.get("/history")
async def get_alert_history(
    limit: int = 50,
    error_type: str | None = None,
    manager: AlertManager = Depends(get_alert_manager),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """获取告警历史记录。

    Args:
        limit: 返回数量限制
        error_type: 按错误类型过滤

    Returns:
        告警历史记录列表
    """
    stats = manager.get_stats()
    history = []

    for error_type_key, data in stats.get("errors", {}).items():
        if error_type and error_type_key != error_type:
            continue

        history.append({
            "error_type": error_type_key,
            "count": data["count"],
            "first_seen": datetime.fromtimestamp(data["first_seen"]).isoformat() if data["first_seen"] else None,
            "last_seen": datetime.fromtimestamp(data["last_seen"]).isoformat() if data["last_seen"] else None,
            "last_alert_time": datetime.fromtimestamp(data["last_alert_time"]).isoformat() if data["last_alert_time"] else None,
            "samples": data.get("samples", []),
        })

    # 按最后告警时间排序
    history.sort(key=lambda x: x["last_seen"] or "", reverse=True)
    return history[:limit]


@router.post("/clear")
async def clear_alert_stats(
    manager: AlertManager = Depends(get_alert_manager),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """清空告警统计。

    Returns:
        操作结果
    """
    manager.clear_stats()
    return {"message": "告警统计已清空"}


@router.get("/config")
async def get_alert_config(
    manager: AlertManager = Depends(get_alert_manager),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取告警配置。

    Returns:
        当前告警配置
    """
    config = manager._config
    return {
        "error_threshold": config.error_threshold,
        "time_window_seconds": config.time_window_seconds,
        "silence_period_seconds": config.silence_period_seconds,
        "monitored_errors": config.monitored_errors,
    }


@router.put("/config")
async def update_alert_config(
    new_config: dict[str, Any],
    manager: AlertManager = Depends(get_alert_manager),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, str]:
    """更新告警配置。

    Args:
        new_config: 新的告警配置

    Returns:
        操作结果
    """
    # 更新配置
    if "error_threshold" in new_config:
        manager._config.error_threshold = new_config["error_threshold"]
    if "time_window_seconds" in new_config:
        manager._config.time_window_seconds = new_config["time_window_seconds"]
    if "silence_period_seconds" in new_config:
        manager._config.silence_period_seconds = new_config["silence_period_seconds"]

    return {"message": "告警配置已更新"}
