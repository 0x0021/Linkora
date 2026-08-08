"""F-H6：/api/dashboard/stream-data 聚合端点测试。

验证三路数据（logs / decisions / messages）被合并到单端点，且增量游标
（last_message_id / last_log_id）正确工作，从而让前端可由多路轮询收敛为单通道。
"""

import json

import pytest
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from web.api import app
from web.routers import dashboard_live as dl


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_api.db"
    monkeypatch.setattr("web.dependencies.DEFAULT_DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
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
        "embedding:\n"
        "  enabled: false\n"
        "tools:\n"
        "  available: []\n"
        "rules:\n"
        "  intent_filter: {}\n"
        "web:\n"
        "  port: 8000\n"
        "  auth_enabled: false\n"
        "  auth_username: admin\n"
        "  auth_password: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("web.api.CONFIG_PATH", str(cfg))
    return str(cfg)


def _async_ret(value):
    """返回一个 async 函数，恒返回 value（用于 mock 被 await 的 handler）。"""
    async def _f(**kw):
        return value
    return _f


class TestDashboardStreamData:
    def test_stream_data_aggregates_three_sources(self, tmp_config, tmp_db, monkeypatch):
        """三路 handlers 的返回被合并到单响应，顶层含 logs/decisions/messages/max_message_id。"""
        monkeypatch.setattr(
            dl, "_get_logs",
            _async_ret(JSONResponse({"logs": [{"id": 1}], "total": 1, "buffer_total": 1, "max_id": 1})),
        )
        monkeypatch.setattr(
            dl, "_recent_decisions", _async_ret({"decisions": [{"msg_id": "m1"}], "total": 1}),
        )
        monkeypatch.setattr(
            dl, "_messages_handler", _async_ret({"messages": [{"id": 7}, {"id": 5}], "current_user_name": "x"}),
        )
        client = TestClient(app)
        r = client.get("/api/dashboard/stream-data?last_message_id=0&last_log_id=0")
        assert r.status_code == 200
        d = r.json()
        assert set(d.keys()) >= {"logs", "decisions", "messages", "max_message_id"}
        assert d["logs"]["logs"] == [{"id": 1}]
        assert d["decisions"]["decisions"][0]["msg_id"] == "m1"
        assert d["messages"] == [{"id": 7}, {"id": 5}]
        assert d["max_message_id"] == 7

    def test_stream_data_incremental_message_cursor(self, tmp_config, tmp_db, monkeypatch):
        """last_message_id 游标：只回传 id 大于游标的新消息，且 max_message_id 为最大值。"""
        monkeypatch.setattr(dl, "_get_logs", _async_ret(JSONResponse({"logs": [], "max_id": 0})))
        monkeypatch.setattr(dl, "_recent_decisions", _async_ret({"decisions": []}))
        monkeypatch.setattr(
            dl, "_messages_handler",
            _async_ret({"messages": [{"id": 9}, {"id": 8}, {"id": 7}, {"id": 6}]}),
        )
        client = TestClient(app)
        r = client.get("/api/dashboard/stream-data?last_message_id=7")
        d = r.json()
        # id>7 的为 9、8；6 不大于 7 被过滤
        assert [m["id"] for m in d["messages"]] == [9, 8]
        assert d["max_message_id"] == 9

    def test_stream_data_passes_cursor_to_logs(self, tmp_config, tmp_db, monkeypatch):
        """last_log_id 应透传给 get_logs（增量），decisions_platform 透传给 recent_decisions。"""
        captured = {}

        async def fake_logs(**kw):
            captured.update(kw)
            return JSONResponse({"logs": [], "max_id": 5})

        async def fake_decisions(**kw):
            captured.update({"dec_platform": kw.get("platform")})
            return {"decisions": []}

        monkeypatch.setattr(dl, "_get_logs", fake_logs)
        monkeypatch.setattr(dl, "_recent_decisions", fake_decisions)
        monkeypatch.setattr(dl, "_messages_handler", _async_ret({"messages": []}))
        client = TestClient(app)
        r = client.get(
            "/api/dashboard/stream-data?last_log_id=42&decisions_platform=dingtalk&platform=all"
        )
        assert r.status_code == 200
        assert captured.get("since") == 42
        assert captured.get("platform") == "all"
        assert captured.get("dec_platform") == "dingtalk"
