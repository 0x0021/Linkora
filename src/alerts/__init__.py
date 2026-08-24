"""
Linkora 错误监控告警系统

功能：
1. 监控关键异常类型（LLMNetworkError/LLMRateLimitError/LLMAuthError 等）
2. 在异常频率超过阈值时触发告警
3. 支持多种告警渠道（日志、Webhook、邮件）
4. 防抖动：同一错误类型在静默期内不重复告警

快速开始：
    from src.alerts.manager import get_alert_manager, record_error

    # 记录错误
    record_error("LLMNetworkError", "connection timeout")

    # 获取统计
    manager = get_alert_manager()
    stats = manager.get_stats()
"""
from __future__ import annotations

from src.alerts.manager import (
    AlertConfig,
    AlertManager,
    AlertChannel,
    AlertSeverity,
    get_alert_manager,
    record_error,
)

__all__ = [
    "AlertConfig",
    "AlertManager",
    "AlertChannel",
    "AlertSeverity",
    "get_alert_manager",
    "record_error",
]
