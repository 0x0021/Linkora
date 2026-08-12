"""结构化 JSON 日志格式化器。

用于统一日志输出格式，便于日志聚合和分析。
"""
from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime
from typing import Any


class JSONFormatter(logging.Formatter):
    """JSON 格式的日志记录器。

    输出示例：
    {
        "timestamp": "2026-08-14T10:30:00+08:00",
        "level": "INFO",
        "logger": "src.platform.memory",
        "message": "清理了 15 个旧记忆",
        "module": "memory_repo",
        "function": "cleanup_old_memories",
        "line": 394,
        "extra": {}
    }
    """

    def __init__(self, include_extra: bool = True, include_stack: bool = False):
        super().__init__()
        self.include_extra = include_extra
        self.include_stack = include_stack

    def format(self, record: logging.LogRecord) -> str:
        """格式化为 JSON 字符串。"""
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # 添加异常信息
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        # 添加线程信息
        log_entry["thread"] = {
            "id": record.thread,
            "name": record.threadName,
        }

        # 添加额外字段
        extra_fields = getattr(record, "extra_fields", None)
        if self.include_extra and extra_fields is not None:
            log_entry["extra"] = extra_fields

        # 添加调用者信息（可选，兼容旧代码）
        try:
            caller = getattr(record, "caller", None)
            if caller:
                log_entry["caller"] = caller
        except Exception:
            pass

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def setup_json_logging(
    level: int = logging.INFO,
    include_stack: bool = False,
    target: Any = None,
) -> None:
    """设置 JSON 日志格式。

    Args:
        level: 日志级别
        include_stack: 是否包含栈信息
        target: 日志输出目标（默认为 stderr）
    """
    handler = logging.StreamHandler(target or sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(JSONFormatter(include_stack=include_stack))

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)


def log_with_extra(logger: logging.Logger, level: int, msg: str, **extra: Any) -> None:
    """发送带额外字段的日志。

    Usage:
        log_with_extra(logger, logging.INFO, "处理消息", user_id="u123", chat_id="c456")
    """
    # 创建临时 LogRecord
    record = logger.makeRecord(
        logger.name, level, "(unknown)", 0, msg, (), None
    )
    record.extra_fields = extra
    logger.handle(record)


def info_with_extra(logger: logging.Logger, msg: str, **extra: Any) -> None:
    """发送 INFO 级别带额外字段的日志。"""
    log_with_extra(logger, logging.INFO, msg, **extra)


def error_with_extra(logger: logging.Logger, msg: str, **extra: Any) -> None:
    """发送 ERROR 级别带额外字段的日志。"""
    log_with_extra(logger, logging.ERROR, msg, **extra)
