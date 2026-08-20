"""共享韧性（resilience）收敛工具。

用于把散落在全仓的 ``except Exception`` 兜底统一收口到本模块，做到三件事：

1. **仅吞已知可恢复异常**：调用方声明 ``recoverable`` 元组，意外异常仍向上抛
   （或显式标记 ``unexpected=True`` 记录全量 traceback + 埋点），避免掩盖真实 bug。
2. **埋点计数**：所有经韧性兜底的失败计入进程内计数器，监控侧可聚合告警。
3. **统一日志**：``[resilience]`` 前缀 + ``exc_info``，杜绝「静默吞掉、无法诊断」。

约定（与 ``src/platform/resilience.py`` 的 ``init_platform_safe`` 一致）：
- 真正「绝不向调用方崩栈」的安全网才用 broad except + ``# noqa: BLE001``；
- 能声明具体异常类型的地方优先收窄到 ``sqlite3.Error`` / 已知 IM 错误等。
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from typing import Any, Callable, Type

logger = logging.getLogger(__name__)

# 进程内韧性失败计数：event -> 次数。监控侧可经 get_resilience_metrics() 拉取。
_resilience_counters: Counter[str] = Counter()
_resilience_lock = threading.Lock()


def bump_resilience_metric(event: str) -> None:
    """埋点：累加某韧性兜底事件的计数（线程安全）。"""
    with _resilience_lock:
        _resilience_counters[event] += 1


def get_resilience_metrics() -> dict[str, int]:
    """返回当前进程内韧性失败计数快照（供 /metrics 或健康检查暴露）。"""
    with _resilience_lock:
        return dict(_resilience_counters)


def report_resilience_failure(
    event: str,
    exc: BaseException,
    *,
    unexpected: bool = False,
    log_level: int = logging.WARNING,
) -> None:
    """记录一次韧性兜底失败并埋点。

    Args:
        event: 事件名（建议 ``<module>.<func>`` 或 ``<platform>.<op>``）。
        exc: 被捕获的异常。
        unexpected: True 表示这是「未预期的逻辑错误」而非已知可恢复异常，
            会强制记录全量 traceback（``logger.exception``），便于定位真实 bug。
        log_level: 已知可恢复异常的日志级别（默认 WARNING）。
    """
    bump_resilience_metric(event)
    if unexpected:
        logger.exception("[resilience] 未预期异常被兜底 %s: %s", event, exc)
    else:
        logger.log(log_level, "[resilience] 兜底 %s: %s", event, exc, exc_info=True)


def swallow_recoverable(
    *,
    recoverable: Type[BaseException] | tuple[Type[BaseException], ...] = Exception,
    default: Any = None,
    event: str | None = None,
    metric: bool = True,
    log_level: int = logging.WARNING,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器：仅吞「已知可恢复异常」，其余仍向上抛（不掩盖真实 bug）。

    用于「失败则返回兜底值」的函数（如查询类返回 ``[]`` / ``None`` / ``False``）。
    被吞的异常记录 ``[resilience]`` 日志 + 埋点；非 ``recoverable`` 异常直接穿透。

    Args:
        recoverable: 允许吞掉的异常类型（默认 ``Exception``=兜底所有；收窄时传
            具体类型如 ``(sqlite3.Error, ValueError)``）。
        default: 捕获时返回的兜底值。
        event: 埋点/日志事件名，默认取 ``func.__qualname__``。
        metric: 是否埋点计数。
        log_level: 已知可恢复异常的日志级别。
    """
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = event or fn.__qualname__

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except recoverable as exc:  # noqa: BLE001 - 仅吞声明过的可恢复异常
                if metric:
                    bump_resilience_metric(name)
                logger.log(log_level, "[resilience] 兜底 %s: %s", name, exc, exc_info=True)
                return default

        return wrapper

    return deco


class RecoverableError(Exception):
    """标记「此异常属于已知可恢复失败」的基类。

    适配器/工具层可抛 ``RecoverableError`` 子类，让调用方的 broad 韧性网
    明确区分「已知业务降级」与「未预期逻辑错误」。
    """
