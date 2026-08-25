"""告警路由 API 测试。

验证告警 API 端点能正常工作。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from web.api import app
from src.alerts.manager import AlertManager, AlertConfig, get_alert_manager
from web.auth_middleware import _token_manager


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
    """测试客户端 + 被 dependency_overrides 注入的 AlertManager mock。

    FastAPI 的 Depends 在模块导入时即捕获 callable 引用,直接 patch 模块
    全局属性无法生效,必须用 app.dependency_overrides 注入 mock。
    """
    mock_manager = MagicMock(spec=AlertManager)
    app.dependency_overrides[get_alert_manager] = lambda: mock_manager
    token = _token_manager.generate_token("admin", "admin")
    test_client = TestClient(app)
    test_client.headers["Authorization"] = f"Bearer {token}"
    yield test_client, mock_manager
    app.dependency_overrides.pop(get_alert_manager, None)


class TestAlertsAPI:
    """告警 API 测试。"""

    def test_get_alert_stats(self, client):
        """验证获取告警统计 API。"""
        test_client, mock_manager = client
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
            },
        }

        response = test_client.get("/api/alerts/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_error_types"] == 2
        assert "LLMNetworkError" in data["errors"]

    def test_get_alert_history(self, client):
        """验证获取告警历史 API。"""
        test_client, mock_manager = client
        mock_manager.get_stats.return_value = {
            "total_error_types": 2,
            "errors": {
                "LLMNetworkError": {
                    "count": 10,
                    "first_seen": 1000.0,
                    "last_seen": 2000.0,
                    "last_alert_time": 1500.0,
                    "samples": ["error1", "error2"],
                },
                "LLMRateLimitError": {
                    "count": 5,
                    "first_seen": 1000.0,
                    "last_seen": 2000.0,
                    "last_alert_time": 1800.0,
                    "samples": ["rate limit 1", "rate limit 2"],
                },
            },
        }

        response = test_client.get("/api/alerts/history")
        assert response.status_code == 200
        history = response.json()
        assert len(history) == 2
        error_types = {item["error_type"] for item in history}
        assert error_types == {"LLMNetworkError", "LLMRateLimitError"}

    def test_get_alert_history_with_filter(self, client):
        """验证按错误类型过滤告警历史。"""
        test_client, mock_manager = client
        mock_manager.get_stats.return_value = {
            "total_error_types": 2,
            "errors": {
                "LLMNetworkError": {"count": 10, "first_seen": 1000.0, "last_seen": 2000.0, "last_alert_time": 1500.0, "samples": []},
                "LLMRateLimitError": {"count": 5, "first_seen": 1000.0, "last_seen": 2000.0, "last_alert_time": 1800.0, "samples": []},
            },
        }

        response = test_client.get("/api/alerts/history?error_type=LLMNetworkError")
        assert response.status_code == 200
        history = response.json()
        assert len(history) == 1
        assert history[0]["error_type"] == "LLMNetworkError"

    def test_clear_alert_stats(self, client):
        """验证清空告警统计 API。"""
        test_client, mock_manager = client

        response = test_client.post("/api/alerts/clear")
        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "告警统计已清空"
        mock_manager.clear_stats.assert_called_once()

    def test_get_alert_config(self, client):
        """验证获取告警配置 API。"""
        test_client, mock_manager = client
        mock_manager._config = AlertConfig(
            error_threshold=10,
            time_window_seconds=300,
            silence_period_seconds=600,
            monitored_errors=["LLMNetworkError", "LLMRateLimitError"],
        )

        response = test_client.get("/api/alerts/config")
        assert response.status_code == 200
        config = response.json()
        assert config["error_threshold"] == 10
        assert config["time_window_seconds"] == 300
        assert config["silence_period_seconds"] == 600

    def test_update_alert_config(self, client):
        """验证更新告警配置 API。"""
        test_client, mock_manager = client
        mock_manager._config = AlertConfig()

        response = test_client.put("/api/alerts/config", json={
            "error_threshold": 20,
            "time_window_seconds": 600,
        })
        assert response.status_code == 200
        assert mock_manager._config.error_threshold == 20
        assert mock_manager._config.time_window_seconds == 600
