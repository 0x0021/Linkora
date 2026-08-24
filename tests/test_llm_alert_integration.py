"""LLM 客户端告警集成测试。

验证 LLM 异常分类后能正确触发告警。
"""
from __future__ import annotations

import pytest
from openai import APIStatusError, APIConnectionError, APITimeoutError, RateLimitError
from unittest.mock import MagicMock

from src.exceptions import LLMNetworkError, LLMRateLimitError, LLMAuthError
from src.llm.client import _rethrow_classified
from src.alerts.manager import get_alert_manager, AlertConfig


class TestLLMAlertIntegration:
    """LLM 异常分类告警集成测试。"""

    def setup_method(self):
        """每个测试前清空告警统计。"""
        get_alert_manager().clear_stats()

    def test_rate_limit_error_triggers_alert(self):
        """验证限频错误能触发告警。"""
        manager = get_alert_manager()
        # 触发多次限频错误
        for i in range(10):
            response = MagicMock()
            original = RateLimitError(message=f"rate limit {i}", response=response, body=None)
            try:
                _rethrow_classified(original)
            except LLMRateLimitError:
                manager.record_error("LLMRateLimitError", f"rate limit {i}")

        stats = manager.get_stats()
        assert stats["errors"]["LLMRateLimitError"]["count"] == 10

    def test_network_error_triggers_alert(self):
        """验证网络错误能触发告警。"""
        manager = get_alert_manager()
        for i in range(10):
            request = MagicMock()
            original = APIConnectionError(message=f"connection error {i}", request=request)
            try:
                _rethrow_classified(original)
            except LLMNetworkError:
                manager.record_error("LLMNetworkError", f"connection error {i}")

        stats = manager.get_stats()
        assert stats["errors"]["LLMNetworkError"]["count"] == 10

    def test_auth_error_triggers_alert(self):
        """验证鉴权错误能触发告警。"""
        manager = get_alert_manager()
        for i in range(10):
            response = MagicMock()
            response.status_code = 401
            original = APIStatusError("Unauthorized", response=response, body=None)
            try:
                _rethrow_classified(original)
            except LLMAuthError:
                manager.record_error("LLMAuthError", "auth error")

        stats = manager.get_stats()
        assert stats["errors"]["LLMAuthError"]["count"] == 10

    def test_severity_classification(self):
        """验证严重程度分类正确。"""
        manager = get_alert_manager()
        assert manager._get_severity("LLMAuthError") == "critical"
        assert manager._get_severity("DBBusyError") == "critical"
        assert manager._get_severity("LLMNetworkError") == "error"
        assert manager._get_severity("LLMRateLimitError") == "error"
        assert manager._get_severity("UnknownError") == "warning"
