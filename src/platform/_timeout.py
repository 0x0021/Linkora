"""超时保护的子线程执行工具（P0-2026-08-08）。

避免在 ``with ThreadPoolExecutor(...) as ex:`` 块里依赖 ``future.result(timeout=N)``
做硬中断——``with`` 退出时 ``shutdown(wait=True)`` 会阻塞到 worker 真正结束，
使超时保护形同虚设（已复现：超时分支虽触发，主线程仍卡到 worker 跑完，
启动被拖死）。

这里用「显式持有 executor」的模式：超时或异常后立即
``shutdown(wait=False, cancel_futures=True)`` 返回，不阻塞调用方；后台 worker
跑完自行退出。两个超时敏感路径（DB 初始化、openDingTalkId 解析）共用本原语，
保证行为一致、可单测。
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def run_with_timeout(
    fn: Callable[..., Any],
    *,
    timeout: float,
    timeout_value: Any = None,
    error_value: Any = None,
    thread_name: str = "guarded",
) -> tuple[Any, bool, bool]:
    """在独立子线程执行 ``fn``，超时或异常时不阻塞调用方。

    返回值：``(result, timed_out, raised)`` 三元组——

    - 正常完成：``(fn 的返回值, False, False)``
    - 超时（``future.result(timeout)`` 抛 ``TimeoutError``）：``(timeout_value, True, False)``
    - 执行抛异常：``(error_value, False, True)``

    无论哪条路径，退出前都会 ``executor.shutdown(wait=False, cancel_futures=True)``，
    确保调用方不被仍未结束的 worker 拖住（这正是 P0-1 修复的核心：``with`` 块里
    的 ``shutdown(wait=True)`` 会让超时保护失效）。
    """
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1, thread_name_prefix=thread_name)
    future = executor.submit(fn)
    timed_out = False
    raised = False
    result = None
    try:
        result = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        timed_out = True
        logger.error("run_with_timeout: 执行超过 %.1fs，跳过（不阻塞调用方）", timeout)
        result = timeout_value
    except Exception as e:  # noqa: BLE001  异常详情仅记日志，不回传给调用方
        raised = True
        logger.warning("run_with_timeout: 执行异常（已降级）: %s", e)
        result = error_value
    finally:
        # 关键：wait=False + cancel_futures=True —— 立即返回，不等待可能卡死的 worker
        executor.shutdown(wait=False, cancel_futures=True)
    return result, timed_out, raised
