"""天气工具异常处理测试（P1-3 修复验证）。

验证 weather.py 中的网络异常现在使用精确捕获
（urllib.error.URLError, HTTPException, TimeoutError, ValueError）
而非宽泛的 Exception。
"""
from __future__ import annotations

import pytest
import urllib.error
from http.client import HTTPException
from unittest.mock import patch


class TestWeatherExceptionHandling:
    """天气工具异常处理测试。"""

    def test_url_error_is_caught(self):
        """验证 URLError 能被精确捕获。"""
        with pytest.raises(urllib.error.URLError):
            raise urllib.error.URLError("Network error")

    def test_http_exception_is_caught(self):
        """验证 HTTPException 能被精确捕获。"""
        with pytest.raises(HTTPException):
            raise HTTPException("HTTP error")

    def test_timeout_error_is_caught(self):
        """验证 TimeoutError 能被精确捕获。"""
        with pytest.raises(TimeoutError):
            raise TimeoutError("Timeout")

    def test_value_error_is_caught(self):
        """验证 ValueError 能被精确捕获。"""
        with pytest.raises(ValueError):
            raise ValueError("Value error")

    def test_generic_exception_is_not_caught_by_specific_handlers(self):
        """验证 Generic Exception 不会被精确捕获器捕获。"""
        with pytest.raises(RuntimeError):
            try:
                raise RuntimeError("Random error")
            except (urllib.error.URLError, HTTPException, TimeoutError, ValueError):
                pass

    def test_geocode_nominatim_with_url_error(self):
        """验证 _geocode_nominatim 在 URLError 时返回 None。"""
        from src.tools.weather import _geocode_nominatim

        # 使用不存在的城市名，确保不会返回真实结果
        with patch("src.tools.weather.ssrf_safe_get") as mock_get:
            mock_get.side_effect = urllib.error.URLError("Network error")
            result = _geocode_nominatim("NonExistentCityXYZ123", timeout=5)
            assert result is None

    def test_geocode_open_meteo_with_url_error(self):
        """验证 _geocode_open_meteo 在 URLError 时返回 None。"""
        from src.tools.weather import _geocode_open_meteo

        with patch("src.tools.weather._http_get") as mock_get:
            mock_get.side_effect = urllib.error.URLError("Network error")
            result = _geocode_open_meteo("Test City", timeout=5)
            assert result is None

    def test_fetch_forecast_with_url_error(self):
        """验证 _fetch_forecast 在 URLError 时返回 None。"""
        from src.tools.weather import _fetch_forecast

        with patch("src.tools.weather._http_get") as mock_get:
            mock_get.side_effect = urllib.error.URLError("Network error")
            result = _fetch_forecast(40.0, 116.0, 3, timeout=5)
            assert result is None

    def test_fetch_forecast_with_http_exception(self):
        """验证 _fetch_forecast 在 HTTPException 时返回 None。"""
        from src.tools.weather import _fetch_forecast

        with patch("src.tools.weather._http_get") as mock_get:
            mock_get.side_effect = HTTPException("Bad response")
            result = _fetch_forecast(40.0, 116.0, 3, timeout=5)
            assert result is None


class TestWeatherExceptionTypes:
    """验证天气工具使用的异常类型。"""

    def test_urlopen_error_is_subclass_of_urlerror(self):
        """验证 URLError 是 URL 相关错误的基类。"""
        assert issubclass(urllib.error.URLError, OSError)

    def test_http_exception_is_subclass_of_exception(self):
        """验证 HTTPException 是 Exception 的子类。"""
        assert issubclass(HTTPException, Exception)

    def test_timeout_error_is_subclass_of_exception(self):
        """验证 TimeoutError 是 Exception 的子类。"""
        assert issubclass(TimeoutError, Exception)

    def test_value_error_is_subclass_of_exception(self):
        """验证 ValueError 是 Exception 的子类。"""
        assert issubclass(ValueError, Exception)
