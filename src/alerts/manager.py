"""Linkora 错误监控告警模块。

用途：
1. 监控关键异常类型（LLMNetworkError/LLMRateLimitError/LLMAuthError 等）
2. 在异常频率超过阈值时触发告警
3. 支持多种告警渠道（日志、Webhook、邮件）

设计原则：
- 非阻塞：告警逻辑不影响主回复链路
- 可配置：阈值、渠道、静默期均可通过配置调整
- 防抖动：同一错误类型在静默期内不重复告警
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("linkora.alerts")


class AlertSeverity(Enum):
    """告警严重程度。"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertChannel(Enum):
    """告警渠道。"""
    LOG = "log"
    WEBHOOK = "webhook"
    EMAIL = "email"


@dataclass
class AlertConfig:
    """告警配置。"""
    # 阈值配置
    error_threshold: int = 10  # 错误次数阈值
    time_window_seconds: int = 300  # 时间窗口（5 分钟）
    silence_period_seconds: int = 600  # 静默期（10 分钟）

    # 渠道配置
    channels: list[AlertChannel] = field(default_factory=lambda: [AlertChannel.LOG])
    webhook_url: str = ""
    email_recipients: list[str] = field(default_factory=list)

    # 监控的错误类型
    monitored_errors: list[str] = field(default_factory=lambda: [
        "LLMNetworkError",
        "LLMRateLimitError",
        "LLMAuthError",
        "DBBusyError",
        "IMAdapterRateLimitError",
    ])


@dataclass
class ErrorCount:
    """错误计数器。"""
    error_type: str
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_alert_time: float = 0.0
    samples: list[str] = field(default_factory=list)


class AlertManager:
    """错误监控告警管理器。"""

    def __init__(self, config: AlertConfig | None = None):
        self._config = config or AlertConfig()
        self._error_counts: dict[str, ErrorCount] = defaultdict(lambda: ErrorCount(error_type="unknown"))
        self._lock = threading.Lock()
        self._alert_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._running = False
        self._thread: threading.Thread | None = None

    def register_callback(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """注册告警回调函数。"""
        self._alert_callbacks.append(callback)

    def record_error(self, error_type: str, error_message: str = "") -> None:
        """记录错误，触发阈值检查。"""
        with self._lock:
            now = time.time()
            error_count = self._error_counts[error_type]
            error_count.error_type = error_type

            # 检查是否在时间窗口内
            if now - error_count.last_seen > self._config.time_window_seconds:
                # 新窗口，重置计数
                error_count.count = 0
                error_count.first_seen = now

            error_count.count += 1
            error_count.last_seen = now

            # 保存样本（最多 5 个）
            if len(error_count.samples) < 5:
                error_count.samples.append(f"{error_message[:100]}..." if error_message else error_type)

            # 检查是否触发告警
            if error_count.count >= self._config.error_threshold:
                # 检查静默期
                if now - error_count.last_alert_time > self._config.silence_period_seconds:
                    self._trigger_alert(error_count)
                    error_count.last_alert_time = now

    def _trigger_alert(self, error_count: ErrorCount) -> None:
        """触发告警。"""
        alert_data = {
            "timestamp": datetime.now().isoformat(),
            "error_type": error_count.error_type,
            "count": error_count.count,
            "time_window": self._config.time_window_seconds,
            "severity": self._get_severity(error_count.error_type),
            "samples": error_count.samples,
            "message": self._format_alert_message(error_count),
        }

        # 记录日志
        logger.warning("🚨 告警触发: %s", alert_data["message"])

        # 调用回调
        for callback in self._alert_callbacks:
            try:
                callback(alert_data)
            except Exception as e:
                logger.error("告警回调执行失败: %s", e)

    def _get_severity(self, error_type: str) -> str:
        """根据错误类型确定严重程度。"""
        critical_types = {
            "LLMAuthError",
            "DBBusyError",
            "IMAdapterRateLimitError",
        }
        error_types = {
            "LLMNetworkError",
            "LLMRateLimitError",
        }

        if error_type in critical_types:
            return AlertSeverity.CRITICAL.value
        elif error_type in error_types:
            return AlertSeverity.ERROR.value
        return AlertSeverity.WARNING.value

    def _format_alert_message(self, error_count: ErrorCount) -> str:
        """格式化告警消息。"""
        return (
            f"[{error_count.error_type}] 在 {self._config.time_window_seconds}s 内发生 "
            f"{error_count.count} 次错误（阈值: {self._config.error_threshold}）"
        )

    def get_stats(self) -> dict[str, Any]:
        """获取当前统计信息。"""
        with self._lock:
            return {
                "total_error_types": len(self._error_counts),
                "errors": {
                    error_type: {
                        "count": ec.count,
                        "first_seen": ec.first_seen,
                        "last_seen": ec.last_seen,
                        "last_alert_time": ec.last_alert_time,
                        "samples": ec.samples,
                    }
                    for error_type, ec in self._error_counts.items()
                    if ec.count > 0
                },
            }

    def clear_stats(self) -> None:
        """清空统计信息。"""
        with self._lock:
            self._error_counts.clear()


# 全局单例
_alert_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    """获取全局告警管理器实例。"""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager


def record_error(error_type: str, error_message: str = "") -> None:
    """记录错误的便捷函数。"""
    get_alert_manager().record_error(error_type, error_message)
