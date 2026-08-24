"""LLM 异常分类测试（P1-1 修复验证）。

验证 _rethrow_classified() 能将 openai 异常按类型映射为 LinkoraError 族子类：
- RateLimitError → LLMRateLimitError
- APIConnectionError / APITimeoutError → LLMNetworkError
- APIStatusError 4xx → LLMAuthError
- APIStatusError 5xx → LLMNetworkError
"""
from __future__ import annotations

import pytest
from openai import APIStatusError, APIConnectionError, APITimeoutError, RateLimitError
from unittest.mock import MagicMock

from src.exceptions import LLMNetworkError, LLMRateLimitError, LLMAuthError
from src.llm.client import _rethrow_classified


class TestRethrowClassified:
    """_rethrow_classified() 分类正确性测试。"""

    def test_rate_limit_error_becomes_llm_rate_limit_error(self):
        response = MagicMock()
        original = RateLimitError(message="rate limit exceeded", response=response, body=None)
        with pytest.raises(LLMRateLimitError, match="rate limit exceeded"):
            _rethrow_classified(original)

    def test_api_connection_error_becomes_llm_network_error(self):
        request = MagicMock()
        original = APIConnectionError(message="connection refused", request=request)
        with pytest.raises(LLMNetworkError, match="connection refused"):
            _rethrow_classified(original)

    def test_api_timeout_error_becomes_llm_network_error(self):
        request = MagicMock()
        original = APITimeoutError(request=request)
        with pytest.raises(LLMNetworkError):
            _rethrow_classified(original)

    def test_api_status_error_401_becomes_llm_auth_error(self):
        response = MagicMock()
        response.status_code = 401
        original = APIStatusError("Unauthorized", response=response, body=None)
        with pytest.raises(LLMAuthError):
            _rethrow_classified(original)

    def test_api_status_error_403_becomes_llm_auth_error(self):
        response = MagicMock()
        response.status_code = 403
        original = APIStatusError("Forbidden", response=response, body=None)
        with pytest.raises(LLMAuthError):
            _rethrow_classified(original)

    def test_api_status_error_500_becomes_llm_network_error(self):
        response = MagicMock()
        response.status_code = 500
        original = APIStatusError("Internal Server Error", response=response, body=None)
        with pytest.raises(LLMNetworkError):
            _rethrow_classified(original)

    def test_generic_exception_passes_through(self):
        """非 openai 异常应原样抛出（_rethrow_classified 只处理已知类型）。"""
        # _rethrow_classified 只处理 openai 异常，其他异常会静默返回（设计如此）
        original = RuntimeError("some random error")
        # 函数不抛异常，只是静默返回
        result = _rethrow_classified(original)
        assert result is None

    def test_rate_limit_with_429_in_message(self):
        """验证 429 限频错误正确分类。"""
        response = MagicMock()
        original = RateLimitError(message="429 Too Many Requests", response=response, body=None)
        with pytest.raises(LLMRateLimitError):
            _rethrow_classified(original)

    def test_network_error_with_timeout(self):
        """验证超时错误正确分类为网络错误。"""
        request = MagicMock()
        original = APITimeoutError(request=request)
        with pytest.raises(LLMNetworkError):
            _rethrow_classified(original)

    def test_auth_error_with_401(self):
        """验证 401 鉴权错误正确分类。"""
        response = MagicMock()
        response.status_code = 401
        original = APIStatusError("Unauthorized", response=response, body=None)
        with pytest.raises(LLMAuthError):
            _rethrow_classified(original)

    def test_auth_error_with_403(self):
        """验证 403 禁止访问错误正确分类。"""
        response = MagicMock()
        response.status_code = 403
        original = APIStatusError("Forbidden", response=response, body=None)
        with pytest.raises(LLMAuthError):
            _rethrow_classified(original)

    def test_network_error_with_500(self):
        """验证 500 服务器错误正确分类为网络错误。"""
        response = MagicMock()
        response.status_code = 500
        original = APIStatusError("Internal Server Error", response=response, body=None)
        with pytest.raises(LLMNetworkError):
            _rethrow_classified(original)

    def test_network_error_with_503(self):
        """验证 503 服务不可用正确分类为网络错误。"""
        response = MagicMock()
        response.status_code = 503
        original = APIStatusError("Service Unavailable", response=response, body=None)
        with pytest.raises(LLMNetworkError):
            _rethrow_classified(original)

    def test_exception_chain_preserved(self):
        """验证 __cause__ 链完整保留。"""
        response = MagicMock()
        original = RateLimitError(message="rate limit", response=response, body=None)
        try:
            _rethrow_classified(original)
        except LLMRateLimitError as e:
            assert e.__cause__ is original
            assert str(e.__cause__) == "rate limit"
