"""web/routers/departments.py 路由单元测试。

用 FastAPI TestClient + mock _run_dws / _get_project_root 隔离外部依赖。
"""

from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from web.routers.departments import router, _DEPT_CACHE


@pytest.fixture(autouse=True)
def clear_dept_cache():
    _DEPT_CACHE.clear()
    yield
    _DEPT_CACHE.clear()


@pytest.fixture
def client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ============ /api/departments/tree ============

class TestDepartmentTree:
    def test_success(self, client):
        mock_data = {"result": [{"deptId": 1, "deptName": "研发部"}]}
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.return_value = mock_data
            resp = client.get("/api/departments/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["tree"]) == 1
        assert data["tree"][0]["name"] == "研发部"

    def test_empty_result(self, client):
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.return_value = {"result": []}
            resp = client.get("/api/departments/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False

    def test_cache_hit(self, client):
        mock_data = {"result": [{"deptId": 1, "deptName": "缓存部"}]}
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.return_value = mock_data
            client.get("/api/departments/tree")
            resp2 = client.get("/api/departments/tree")
        assert resp2.json()["cached"] is True
        assert mock_dws.call_count == 1

    def test_permission_denied(self, client):
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.side_effect = RuntimeError("TOKEN_VERIFIED_FAILED")
            resp = client.get("/api/departments/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["code"] == "permission_denied"

    def test_timeout(self, client):
        import asyncio
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.side_effect = asyncio.TimeoutError()
            resp = client.get("/api/departments/tree")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "超时" in data["error"]


# ============ /api/departments/{dept_id}/children ============

class TestDepartmentChildren:
    def test_success(self, client):
        mock_data = {"result": [{"deptId": 10, "deptName": "前端组"}]}
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.return_value = mock_data
            resp = client.get("/api/departments/1/children")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["children"][0]["name"] == "前端组"


# ============ /api/departments/{dept_id}/members ============

class TestDepartmentMembers:
    def test_success(self, client):
        mock_data = {
            "deptUserList": [{
                "userInfo": {
                    "userId": "u1",
                    "name": "张三",
                    "title": "工程师",
                    "avatarUrl": "",
                    "email": "zhang@test.com",
                    "mobile": "13800138000",
                }
            }]
        }
        with patch("web.routers.departments._run_dws", new_callable=AsyncMock) as mock_dws:
            mock_dws.return_value = mock_data
            resp = client.get("/api/departments/1/members")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["members"][0]["name"] == "张三"
        assert data["count"] == 1


# ============ /api/departments/cache/clear ============

class TestClearCache:
    def test_clear(self, client):
        _DEPT_CACHE["key"] = {"ts": 0, "data": []}
        resp = client.post("/api/departments/cache/clear")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert len(_DEPT_CACHE) == 0


# ============ /api/history/import/status ============

class TestImportStatus:
    def test_no_state_file(self, client):
        with patch("web.routers.departments._get_project_root") as mock_root:
            import tempfile
            tmp = tempfile.mkdtemp()
            mock_root.return_value = __import__("pathlib").Path(tmp)
            with patch("src.config.DEFAULT_DATA_DIR", tmp):
                resp = client.get("/api/history/import/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["state"] is None
