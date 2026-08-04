"""web/routers/drafts.py 路由单元测试。

用 FastAPI TestClient + mock store / get_app_instance 隔离外部依赖。
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from web.routers.drafts import router


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
    ai = MagicMock()
    ai.llm_agent = MagicMock()
    return ai


# ============ GET /api/drafts ============

class TestListDrafts:
    def test_success(self, client, mock_store):
        mock_store._draft_repo.list_drafts.return_value = (
            [{"draft_id": "d1", "status": "pending"}], 1
        )
        mock_store._draft_repo.count_pending_drafts.return_value = 5
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["total"] == 1
        assert data["pending_count"] == 5

    def test_with_status_filter(self, client, mock_store):
        mock_store._draft_repo.list_drafts.return_value = ([], 0)
        mock_store._draft_repo.count_pending_drafts.return_value = 0
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts?status=approved&limit=10&offset=5")
        assert resp.status_code == 200
        mock_store._draft_repo.list_drafts.assert_called_with(
            status="approved", platform=None, limit=10, offset=5)

    def test_limit_clamped(self, client, mock_store):
        mock_store._draft_repo.list_drafts.return_value = ([], 0)
        mock_store._draft_repo.count_pending_drafts.return_value = 0
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts?limit=1000")
        assert resp.status_code == 200
        call_args = mock_store._draft_repo.list_drafts.call_args
        assert call_args[1]["limit"] == 500  # capped

    def test_db_error(self, client, mock_store):
        mock_store._draft_repo.list_drafts.side_effect = RuntimeError("db down")
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts")
        assert resp.status_code == 500


# ============ GET /api/drafts/count ============

class TestCountPendingDrafts:
    def test_success(self, client, mock_store):
        mock_store._draft_repo.count_pending_drafts.return_value = 3
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts/count")
        assert resp.status_code == 200
        assert resp.json()["pending_count"] == 3


# ============ GET /api/drafts/{draft_id} ============

class TestGetDraft:
    def test_found(self, client, mock_store):
        mock_store._draft_repo.get_draft.return_value = {"draft_id": "d1", "status": "pending"}
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts/d1")
        assert resp.status_code == 200
        assert resp.json()["draft"]["draft_id"] == "d1"

    def test_not_found(self, client, mock_store):
        mock_store._draft_repo.get_draft.return_value = None
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.get("/api/drafts/notexist")
        assert resp.status_code == 404


# ============ POST /api/drafts/{draft_id}/discard ============

class TestDiscardDraft:
    def test_success(self, client, mock_store):
        mock_store._draft_repo.get_draft.return_value = {"draft_id": "d1", "status": "pending"}
        mock_store._draft_repo.resolve_draft.return_value = True
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.post("/api/drafts/d1/discard")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_already_processed(self, client, mock_store):
        mock_store._draft_repo.get_draft.return_value = {"draft_id": "d1", "status": "approved"}
        with patch("web.routers.drafts.get_store", return_value=mock_store):
            resp = client.post("/api/drafts/d1/discard")
        assert resp.status_code == 400
