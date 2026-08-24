"""告警路由 API 测试。

验证告警 API 端点能正常工作。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from web.api import app
from src.alerts.manager import AlertManager, AlertConfig


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """临时 config.yaml,只含必要最小字段。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "dws:\n"
        "  dry_run: true\n"
        "  cli_path: /usr/bin/echo\n"
        "  profile: test\n"
        "poller:\n"
        "  interval_seconds: 30\n"
        "llm:\n"
        "  model: test-model\n"
        "  base_url: http://localhost\n"
        "  api_key: test\n"
    )
    monkeypatch.setattr("web.api.get_config_path", lambda: str(cfg))
    return str(cfg)


@pytest.fixture
def client(tmp_config):
    """测试客户端。"""
    return TestClient(app)


class TestAlertsAPI:
    """告警 API 测试。"""

    def test_get_alert_stats(self, client, tmp_config):
        """验证获取告警统计 API。"""
        with patch('web.routers.alerts.get_alert_manager') as mock_get_manager:
            mock_manager = MagicMock(spec=AlertManager)
            mock_manager.get_stats.return_value = {
                "total_error_types": 2,
                "errors": {
                    "LLMNetworkError": {
                        "count": 10,
                        "first_seen": 1000.0,
                        "last_seen": 2000.0,
                        "last_alert_time": 1500.0,
                        "samples": ["error1", "error2"],
                    }
                }
            }
            mock_get_manager.return_value = mock_manager

            response = client.get("/api/alerts/stats")
            assert response.status_code == 200
            data = response.json()
            assert data["total_error_types"] == 2
            assert "LLMNetworkError" in data["errors"]

    def test_get_alert_history(self, client, tmp_config):
        """验证获取告警历史 API。"""
        with patch('web.routers.alerts.get_alert_manager') as mock_get_manager:
            mock_manager = MagicMock(spec=AlertManager)
            mock_manager.get_stats.return_value = {
                "total_error_types": 1,
                "errors": {
                    "LLMRateLimitError": {
                        "count": 5,
                        "first_seen": 1000.0,
                        "last_seen": 2000.0,
                        "last_alert_time": 1800.0,
                        "samples": ["rate limit 1", "rate limit 2"],
                    }
                }
            }
            mock_get_manager.return_value = mock_manager

            response = client.get("/api/alerts/history")
            assert response.status_code == 200
            history = response.json()
            assert len(history) == 1
            assert history[0]["error_type"] == "LLMRateLimitError"

    def test_get_alert_history_with_filter(self, client, tmp_config):
        """验证按错误类型过滤告警历史。"""
        with patch('web.routers.alerts.get_alert_manager') as mock_get_manager:
            mock_manager = MagicMock(spec=AlertManager)
            mock_manager.get_stats.return_value = {
                "total_error_types": 2,
                "errors": {
                    "LLMNetworkError": {"count": 10, "first_seen": 1000.0, "last_seen": 2000.0, "last_alert_time": 1500.0, "samples": []},
                    "LLMRateLimitError": {"count": 5, "first_seen": 1000.0, "last_seen": 2000.0, "last_alert_time": 1800.0, "samples": []},
                }
            }
            mock_get_manager.return_value = mock_manager

            response = client.get("/api/alerts/history?error_type=LLMNetworkError")
            assert response.status_code == 200
            history = response.json()
            assert len(history) == 1
            assert history[0]["error_type"] == "LLMNetworkError"

    def test_clear_alert_stats(self, client, tmp_config):
        """验证清空告警统计 API。"""
        with patch('web.routers.alerts.get_alert_manager') as mock_get_manager:
            mock_manager = MagicMock(spec=AlertManager)
            mock_get_manager.return_value = mock_manager

            response = client.post("/api/alerts/clear")
            assert response.status_code == 200
            data = response.json()
            assert data["message"] == "告警统计已清空"
            mock_manager.clear_stats.assert_called_once()

    def test_get_alert_config(self, client, tmp_config):
        """验证获取告警配置 API。"""
        with patch('web.routers.alerts.get_alert_manager') as mock_get_manager:
            mock_manager = MagicMock(spec=AlertManager)
            mock_manager._config = AlertConfig(
                error_threshold=10,
                time_window_seconds=300,
                silence_period_seconds=600,
                monitored_errors=["LLMNetworkError", "LLMRateLimitError"],
            )
            mock_get_manager.return_value = mock_manager

            response = client.get("/api/alerts/config")
            assert response.status_code == 200
            config = response.json()
            assert config["error_threshold"] == 10
            assert config["time_window_seconds"] == 300
            assert config["silence_period_seconds"] == 600

    def test_update_alert_config(self, client, tmp_config):
        """验证更新告警配置 API。"""
        with patch('web.routers.alerts.get_alert_manager') as mock_get_manager:
            mock_manager = MagicMock(spec=AlertManager)
            mock_manager._config = AlertConfig()
            mock_get_manager.return_value = mock_manager

            response = client.put("/api/alerts/config", json={
                "error_threshold": 20,
                "time_window_seconds": 600,
            })
            assert response.status_code == 200
            assert mock_manager._config.error_threshold == 20
            assert mock_manager._config.time_window_seconds == 600
