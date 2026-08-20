"""src/utils/resilience.py 单元测试。"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from src.utils.resilience import (
    get_resilience_metrics,
    report_resilience_failure,
    swallow_recoverable,
)


class TestSwallowRecoverable:
    """装饰器：仅吞声明的可恢复异常。"""

    def test_known_recoverable_returns_default(self):
        @swallow_recoverable(
            recoverable=sqlite3.Error,
            default=[],
            event="test.query",
        )
        def query():
            raise sqlite3.Error("disk full")

        result = query()
        assert result == []

    def test_unknown_exception_propagates(self):
        @swallow_recoverable(
            recoverable=sqlite3.Error,
            default=[],
        )
        def fn():
            raise RuntimeError("logic error")

        with pytest.raises(RuntimeError, match="logic error"):
            fn()

    def test_multiple_recoverable_types(self):
        @swallow_recoverable(
            recoverable=(sqlite3.Error, ValueError),
            default=False,
        )
        def fn():
            raise ValueError("bad input")

        assert fn() is False

    def test_no_exception_passes_through(self):
        @swallow_recoverable(default=None)
        def fn():
            return 42

        assert fn() == 42


class TestReportResilienceFailure:
    def test_unexpected_logs_exception_traceback(self, caplog):
        import src.utils.resilience as mod
        # 强制把 logger 的 level 调到 DEBUG 以捕获所有日志
        mod.logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger="src.utils.resilience"):
            report_resilience_failure(
                "test.unexpected",
                RuntimeError("boom"),
                unexpected=True,
            )
            records = [r for r in caplog.records if "未预期异常被兜底" in r.message]
            assert len(records) >= 1
            assert "test.unexpected" in records[0].message

    def test_known_exception_logs_with_exc_info(self, caplog):
        import src.utils.resilience as mod
        mod.logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger="src.utils.resilience"):
            report_resilience_failure(
                "test.recoverable",
                sqlite3.Error("fail"),
                unexpected=False,
                log_level=logging.WARNING,
            )
            records = [r for r in caplog.records if "兜底 test.recoverable" in r.message]
            assert len(records) >= 1
            assert records[0].exc_info is not None

    def test_bumps_metric(self):
        # 用临时计数器验证埋点
        from src.utils.resilience import _resilience_counters, _resilience_lock
        event = "test.metric"
        with _resilience_lock:
            _resilience_counters[event] = 0
        report_resilience_failure(event, sqlite3.Error("x"))
        with _resilience_lock:
            assert _resilience_counters[event] == 1


class TestMetrics:
    def test_get_resilience_metrics(self):
        # 确保接口返回 dict
        result = get_resilience_metrics()
        assert isinstance(result, dict)
