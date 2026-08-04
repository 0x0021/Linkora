"""SearXNG 后端（searx.space 动态发现 + 轮换 + 冷却）测试。"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.tools import web_search as ws
from src.tools.web_search import (
    _searx_is_challenge,
    _searx_is_index_page,
    _searx_parse_html,
    _searx_parse_json,
    _searx_discover,
    _searx_load_cache,
)


# 清洗缓存，保证每个测试从干净状态开始
@pytest.fixture(autouse=True)
def clear_cache(tmp_path, monkeypatch):
    # 指向临时缓存文件，避免污染真实 data/searx_instances.json
    monkeypatch.setattr(ws, "_SEARXNG_CACHE_PATH", tmp_path / "searx_instances.json")
    if ws._SEARXNG_CACHE_PATH.exists():
        ws._SEARXNG_CACHE_PATH.unlink()
    monkeypatch.setattr(ws, "_searx_state", {"cursor": 0, "cooldown": {}})
    yield
    if ws._SEARXNG_CACHE_PATH.exists():
        ws._SEARXNG_CACHE_PATH.unlink()


# ============ 解析函数 ============

class TestSearxParseJson:
    def test_basic(self):
        text = json.dumps({
            "results": [
                {"title": "<b>Foo</b>", "url": "https://a.com", "content": "hello <i>world</i>"},
                {"title": "Bar", "url": "https://b.com", "content": "x"},
            ]
        })
        out = _searx_parse_json(text)
        assert len(out) == 2
        assert out[0]["title"] == "Foo"
        assert out[0]["url"] == "https://a.com"
        assert "world" in out[0]["snippet"]

    def test_invalid_json(self):
        assert _searx_parse_json("<html>not json</html>") == []


class TestSearxParseHtml:
    def test_article_blocks(self):
        html = """
        <article class="result">
          <h3><a href="https://x.com">Title <b>X</b></a></h3>
          <p class="content">some snippet <i>here</i></p>
        </article>
        <article class="result">
          <h3><a href="https://y.com">Title Y</a></h3>
          <p class="content">another</p>
        </article>
        """
        out = _searx_parse_html(html)
        assert len(out) == 2
        assert out[0]["url"] == "https://x.com"
        assert out[0]["title"] == "Title X"
        assert "snippet" in out[0]["snippet"]

    def test_dedup(self):
        html = (
            '<article class="result"><h3><a href="https://dup.com">A</a></h3></article>'
            '<article class="result"><h3><a href="https://dup.com">A again</a></h3></article>'
        )
        out = _searx_parse_html(html)
        assert len(out) == 1


# ============ 挑战页检测 ============

class TestSearxChallenge:
    def test_anubis(self):
        assert _searx_is_challenge("<title>Making sure you're not a bot!</title>")

    def test_verify_human(self):
        assert _searx_is_challenge("Please verify you are human before continuing")

    def test_normal(self):
        assert not _searx_is_challenge("<article class='result'>real results</article>")


class TestSearxIndexPage:
    def test_searxng_index_meta(self):
        # SearXNG 首页：endpoint=index
        html = '<html><head><meta name="endpoint" content="index"></head><body></body></html>'
        assert _searx_is_index_page(html)

    def test_result_page_not_index(self):
        html = (
            '<html><head><meta name="generator" content="searxng/1.0"></head>'
            '<body><article class="result"><h3><a href="https://x.com">T</a></h3></article></body></html>'
        )
        assert not _searx_is_index_page(html)

    def test_non_searxng_page(self):
        # 不含 searxng 生成器标识 -> 视为非结果页
        assert _searx_is_index_page("<html><body>random</body></html>")

    def test_empty(self):
        assert _searx_is_index_page("")


# ============ 发现 + 缓存 ============

class TestSearxDiscover:
    def test_filter_and_cache(self):
        fake = {
            "instances": {
                "https://good1.example/": {
                    "network_type": "normal", "http": {"status_code": 200},
                    "uptime": {"uptimeWeek": 99.9}, "generator": "searxng/1.0",
                    "timing": {"search": {"success_percentage": 100}}, "main": True,
                },
                "https://good2.example/": {
                    "network_type": "normal", "http": {"status_code": 200},
                    "uptime": {"uptimeWeek": 96.0}, "generator": "searxng/1.0",
                    "timing": {"search": {"success_percentage": 100}}, "main": False,
                },
                "http://insecure.example/": {  # 过滤: http
                    "network_type": "normal", "http": {"status_code": 200},
                    "uptime": {"uptimeWeek": 99}, "generator": "searxng",
                },
                "https://lowuptime.example/": {  # 过滤: 可用率<95
                    "network_type": "normal", "http": {"status_code": 200},
                    "uptime": {"uptimeWeek": 90}, "generator": "searxng",
                },
                "https://torbad.example/": {  # 过滤: tor
                    "network_type": "tor", "http": {"status_code": 200},
                    "uptime": {"uptimeWeek": 99}, "generator": "searxng",
                },
            }
        }
        with patch.object(ws, "ssrf_safe_get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200, json=lambda: fake)
            urls = _searx_discover()
        assert urls == ["https://good1.example/", "https://good2.example/"]
        # 缓存已写
        cache = _searx_load_cache()
        assert cache["urls"] == urls

    def test_discovery_failure_returns_empty(self):
        with patch.object(ws, "ssrf_safe_get", side_effect=requests.Timeout("t")):
            assert _searx_discover() == []


# ============ 轮换 + 冷却 + 搜索 ============

class TestSearxSearch:
    @patch.object(ws, "_searx_discover", return_value=[
        "https://a.example/", "https://b.example/", "https://c.example/",
    ])
    def test_rotates_and_returns_json(self, mock_disc):
        json_body = json.dumps({"results": [
            {"title": "Rokae", "url": "https://acme.com", "content": "机器人公司"},
        ]})
        resp = MagicMock(status_code=200, text=json_body,
                         headers={"Content-Type": "application/json"})
        with patch.object(ws, "ssrf_safe_get", return_value=resp):
            out = ws.searxng_search("Rokae 机器人", num_results=5)
        assert len(out) == 1
        assert out[0]["url"] == "https://acme.com"
        # 第一轮用了 a，cursor 前进
        assert _searx_load_cache()["cursor"] == 1

    @patch.object(ws, "_searx_discover", return_value=[
        "https://a.example/", "https://b.example/",
    ])
    def test_429_triggers_cooldown_and_rotation(self, mock_disc):
        good_body = json.dumps({"results": [
            {"title": "Hit", "url": "https://hit.com", "content": "ok"},
        ]})

        def fake_get(url, **kwargs):
            # 第一个实例 429，第二个实例成功
            if url.startswith("https://a.example"):
                return MagicMock(status_code=429, text="Too Many Requests",
                                  headers={"Content-Type": "text/html"})
            return MagicMock(status_code=200, text=good_body,
                             headers={"Content-Type": "application/json"})

        with patch.object(ws, "ssrf_safe_get", side_effect=fake_get):
            out = ws.searxng_search("test", num_results=5)
        assert len(out) == 1
        assert out[0]["url"] == "https://hit.com"
        # a 进入冷却
        cooldown = _searx_load_cache()["cooldown"]
        assert "https://a.example/" in cooldown

    @patch.object(ws, "_searx_discover", return_value=["https://a.example/"])
    def test_challenge_page_skips(self, mock_disc):
        challenge = "<title>Making sure you're not a bot!</title>"
        with patch.object(ws, "ssrf_safe_get", return_value=MagicMock(
            status_code=200, text=challenge, headers={"Content-Type": "text/html"}
        )):
            assert ws.searxng_search("test") == []
        # a 被冷却
        assert "https://a.example/" in _searx_load_cache()["cooldown"]

    @patch.object(ws, "_searx_discover", return_value=["https://a.example/"])
    def test_index_page_cools_down(self, mock_disc):
        # 实例返回首页（endpoint=index）而非结果 → 视为不可用，冷却
        homepage = '<html><head><meta name="endpoint" content="index"></head><body></body></html>'
        with patch.object(ws, "ssrf_safe_get", return_value=MagicMock(
            status_code=200, text=homepage, headers={"Content-Type": "text/html"}
        )):
            assert ws.searxng_search("test") == []
        assert "https://a.example/" in _searx_load_cache()["cooldown"]

    @patch.object(ws, "_searx_discover", return_value=[])
    def test_no_instances_returns_empty(self, mock_disc):
        assert ws.searxng_search("test") == []

    @patch.object(ws, "_searx_discover", return_value=["https://a.example/"])
    def test_disabled_via_env(self, mock_disc, monkeypatch):
        monkeypatch.setenv("ENABLE_SEARXNG", "0")
        # 重新导入模块级开关（函数内读取全局，已 import 时定下，故用 monkeypatch 改全局）
        monkeypatch.setattr(ws, "_SEARXNG_ENABLED", False)
        try:
            assert ws.searxng_search("test") == []
            mock_disc.assert_not_called()
        finally:
            # 还原，避免污染同文件后续测试（autouse fixture 不重置此开关）
            monkeypatch.setattr(ws, "_SEARXNG_ENABLED", True)
            monkeypatch.delenv("ENABLE_SEARXNG", raising=False)


# ============ 后端注册 ============

class TestSearxBackendRegistered:
    def test_in_backend_list(self):
        names = [n for n, _ in ws._SEARCH_BACKEND_NAMES]
        assert "searxng" in names
        # SearXNG 应排在最后（兜底）
        assert names[-1] == "searxng"

    def test_func_callable(self):
        assert callable(ws.searxng_search)
