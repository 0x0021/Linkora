"""web/routers/dead_letters.py 单元测试（P1-2026-08-08 脱敏回归）。

验证批量重放中单条重放抛异常时，响应体 ``detail[].error`` 使用 ``safe_detail``
常量文案，不再把异常内部文本（可能含路径 / 密钥 / 堆栈片段）回传给客户端——
这正是修复「异常文本泄露进 200 响应体」的核心回归。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from web.errors import SAFE_INTERNAL_ERROR
from web.routers.dead_letters import router


@pytest.fixture
def client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def mock_store():
    store = MagicMock()
    store._draft_repo = MagicMock()
    return store


@pytest.fixture
def mock_app_instance():
    return MagicMock()


class TestBatchReplaySanitization:
    def test_failed_replay_does_not_leak_exception(self, client, mock_store, mock_app_instance):
        # 待重放死信列表含 1 条
        mock_store._draft_repo.list_dead_letters.return_value = ([{"id": 1}], 1)
        # 重放单条时抛异常（含敏感内部路径 / 密钥 / 堆栈片段）
        def _replay(dl_id, platform=None):
            raise RuntimeError("/home/secret/.env: PSWD=xxx traceback line 42")
        mock_app_instance.replay_dead_letter.side_effect = _replay

        with patch("web.routers.dead_letters.get_store", return_value=mock_store), \
             patch("web.routers.dead_letters.get_app_instance", return_value=mock_app_instance), \
             patch("web.routers.dead_letters.get_current_platform", return_value="dingtalk"):
            resp = client.post("/api/dead-letters/batch-replay")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["failed"] == 1
        assert data["replayed"] == 0
        detail = data["detail"]
        assert len(detail) == 1
        err = detail[0]["error"]
        # 关键断言：错误文案为安全常量，不含任何异常内部文本
        assert err == SAFE_INTERNAL_ERROR
        assert "/home/secret" not in err
        assert "PSWD" not in err
        assert "traceback" not in err

    def test_successful_replay_reports_replayed(self, client, mock_store, mock_app_instance):
        mock_store._draft_repo.list_dead_letters.return_value = ([{"id": 1}, {"id": 2}], 2)

        def _replay(dl_id, platform=None):
            return {"success": True}
        mock_app_instance.replay_dead_letter.side_effect = _replay

        with patch("web.routers.dead_letters.get_store", return_value=mock_store), \
             patch("web.routers.dead_letters.get_app_instance", return_value=mock_app_instance), \
             patch("web.routers.dead_letters.get_current_platform", return_value="dingtalk"):
            resp = client.post("/api/dead-letters/batch-replay")

        assert resp.status_code == 200
        data = resp.json()
        assert data["replayed"] == 2
        assert data["failed"] == 0

    def test_empty_list(self, client, mock_store, mock_app_instance):
        mock_store._draft_repo.list_dead_letters.return_value = ([], 0)

        with patch("web.routers.dead_letters.get_store", return_value=mock_store), \
             patch("web.routers.dead_letters.get_app_instance", return_value=mock_app_instance):
            resp = client.post("/api/dead-letters/batch-replay")

        assert resp.status_code == 200
        assert resp.json()["total"] == 0
