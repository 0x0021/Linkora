"""web/routers/skills_marketplace.py 路由单元测试。

用 FastAPI TestClient + mock _ensure_skillhub_cli / subprocess 隔离外部依赖。
"""

import json
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from web.routers.skills_marketplace import router


@pytest.fixture
def client():
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ============ GET /api/skills/marketplace/search ============

class TestSearchMarketplace:
    def test_search_with_keyword(self, client):
        mock_result = json.dumps({
            "query": "weather",
            "results": [{"slug": "weather", "name": "Weather Skill",
                         "description": "获取天气", "version": "1.0",
                         "author": "author1", "installs": 100,
                         "url": "https://example.com"}]
        })
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(True, "")):
            with patch("web.routers.skills_marketplace.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = mock_result
                resp = client.get("/api/skills/marketplace/search?keyword=weather")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["skills"][0]["slug"] == "weather"

    def test_search_empty_keyword_returns_popular(self, client):
        mock_result = json.dumps({
            "results": [{"slug": "popular1", "name": "Popular Skill"}]
        })
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(True, "")):
            with patch("web.routers.skills_marketplace.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = mock_result
                resp = client.get("/api/skills/marketplace/search")
        assert resp.status_code == 200

    def test_no_skills_found(self, client):
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(True, "")):
            with patch("web.routers.skills_marketplace.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "no skills found"
                resp = client.get("/api/skills/marketplace/search?keyword=nonexistent")
        assert resp.status_code == 200
        data = resp.json()
        assert data["skills"] == []
        assert data["total"] == 0

    def test_cli_unavailable(self, client):
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(False, "not installed")):
            resp = client.get("/api/skills/marketplace/search?keyword=x")
        assert resp.status_code == 500

    def test_cli_error(self, client):
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(True, "")):
            with patch("web.routers.skills_marketplace.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stderr = "search error"
                resp = client.get("/api/skills/marketplace/search?keyword=x")
        assert resp.status_code == 500

    def test_invalid_json_with_recovery(self, client):
        """混合输出中的 JSON 提取（正则兜底）。"""
        mixed_output = "Warning: something\n[{\"slug\": \"skill1\", \"name\": \"S1\"}]"
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(True, "")):
            with patch("web.routers.skills_marketplace.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = mixed_output
                resp = client.get("/api/skills/marketplace/search?keyword=x")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1


# ============ GET /api/skills/marketplace/popular ============

class TestPopularSkills:
    def test_returns_same_as_search_empty(self, client):
        mock_result = json.dumps({"results": []})
        with patch("web.routers.skills_marketplace._ensure_skillhub_cli",
                   return_value=(True, "")):
            with patch("web.routers.skills_marketplace.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = mock_result
                resp = client.get("/api/skills/marketplace/popular")
        assert resp.status_code == 200


# ============ GET /api/skills/marketplace/rankings ============

class TestMarketRankings:
    def test_success(self, client):
        mock_rankings = {"sections": [{"id": "trending", "skills": []}]}
        with patch("web.routers.skills_marketplace._fetch_market_rankings",
                   new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_rankings
            resp = client.get("/api/skills/marketplace/rankings")
        assert resp.status_code == 200
        assert resp.json() == mock_rankings

    def test_with_force_param(self, client):
        with patch("web.routers.skills_marketplace._fetch_market_rankings",
                   new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = {"sections": []}
            resp = client.get("/api/skills/marketplace/rankings?force=true")
        assert resp.status_code == 200
        mock_fetch.assert_called_with(force=True)
