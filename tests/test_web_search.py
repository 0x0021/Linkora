"""联网搜索工具单元测试。

覆盖核心逻辑：
- num_results 参数健壮解析（LLM 可能传中文数字/带单位/浮点字符串，
  直接 int() 会抛 ValueError 使工具崩溃）
- execute 输入校验（空 query）
- num_results 越界裁剪到 [1, 10]
"""
from __future__ import annotations

from unittest.mock import patch

from src.tools.web_search import WebSearchTool, _safe_int


class TestSafeInt:
    """_safe_int 容错解析。"""

    def test_valid_numbers(self):
        assert _safe_int("5", 5) == 5
        assert _safe_int(5, 5) == 5
        assert _safe_int("  7  ", 5) == 7

    def test_float_truncated(self):
        assert _safe_int(3.7, 5) == 3
        assert _safe_int("3.7", 5) == 3

    def test_invalid_falls_back(self):
        assert _safe_int("五", 5) == 5
        assert _safe_int("3条", 5) == 5
        assert _safe_int("", 5) == 5
        assert _safe_int(None, 5) == 5
        assert _safe_int("abc", 9) == 9


class TestWebSearchExecute:
    """WebSearchTool.execute 健壮性。"""

    def _mk_results(self):
        return [{"title": "t", "url": "http://x", "snippet": "s"}]

    def test_chinese_num_does_not_crash(self):
        """LLM 传中文数字 '五' 时工具不崩溃，回退默认 5。"""
        tool = WebSearchTool()
        with patch("src.tools.web_search.bing_search", return_value=self._mk_results()) as m:
            r = tool.execute({"query": "测试", "num_results": "五"})
        assert "results" in r
        assert m.call_args.kwargs.get("num_results") == 5

    def test_float_string_num(self):
        tool = WebSearchTool()
        with patch("src.tools.web_search.bing_search", return_value=self._mk_results()) as m:
            tool.execute({"query": "测试", "num_results": "3.7"})
        assert m.call_args.kwargs.get("num_results") == 3

    def test_num_clamped_to_range(self):
        """越界值裁剪到 [1, 10]。"""
        tool = WebSearchTool()
        with patch("src.tools.web_search.bing_search", return_value=self._mk_results()) as m:
            tool.execute({"query": "测试", "num_results": 100})
            assert m.call_args.kwargs.get("num_results") == 10
            tool.execute({"query": "测试", "num_results": -3})
            assert m.call_args.kwargs.get("num_results") == 1

    def test_empty_query_returns_error(self):
        tool = WebSearchTool()
        r = tool.execute({"query": "   "})
        assert r.get("error")

    def test_no_results_note(self):
        """所有后端都无结果时，返回空结果并带错误提示（而非伪装成正常结果）。"""
        tool = WebSearchTool()
        with patch("src.tools.web_search.bing_search", return_value=[]), \
             patch("src.tools.web_search.duckduckgo_search", return_value=[]), \
             patch("src.tools.web_search.searxng_search", return_value=[]):
            r = tool.execute({"query": "无结果查询"})
        assert r["results"] == []
        assert "note" in r
        assert "error" in r
        # 三个后端（必应→DuckDuckGo→SearXNG 兜底）均被尝试
        assert r.get("tried") == ["bing", "duckduckgo", "searxng"]


# ============================================================================
# _searx_is_index_page 内部函数
# ============================================================================
class TestSearxIsIndexPage:
    def test_empty_html(self):
        from src.tools.web_search import _searx_is_index_page
        assert _searx_is_index_page("") is True

    def test_endpoint_index(self):
        from src.tools.web_search import _searx_is_index_page
        html = '<html><meta name="endpoint" content="index"></html>'
        assert _searx_is_index_page(html) is True

    def test_no_searxng_generator(self):
        from src.tools.web_search import _searx_is_index_page
        html = '<html><body>just a page</body></html>'
        assert _searx_is_index_page(html) is True

    def test_searxng_without_results(self):
        """有 generator 但无 article/result class → 首页。"""
        from src.tools.web_search import _searx_is_index_page
        html = '<meta name="generator" content="searxng"><body>index</body>'
        assert _searx_is_index_page(html) is True

    def test_searxng_with_results(self):
        """有 generator 且有 result class → 正常结果页。"""
        from src.tools.web_search import _searx_is_index_page
        html = '<meta name="generator" content="searxng"><div class="result">hit</div>'
        assert _searx_is_index_page(html) is False

    def test_searxng_with_article(self):
        """有 generator 且有 article 标签 → 正常结果页。"""
        from src.tools.web_search import _searx_is_index_page
        html = '<meta name="generator" content="searxng"><article>hit</article>'
        assert _searx_is_index_page(html) is False


# ============================================================================
# _is_garbled 乱码检测
# ============================================================================
class TestIsGarbled:
    def test_empty_text(self):
        from src.tools.web_search import _is_garbled
        assert _is_garbled("") is False

    def test_only_spaces(self):
        """全空白文本 → total>0 但无 weird → 不判定乱码。"""
        from src.tools.web_search import _is_garbled
        assert _is_garbled("   \t\n  ") is False

    def test_control_chars(self):
        from src.tools.web_search import _is_garbled
        assert _is_garbled("hello\x00world") is True

    def test_normal_text(self):
        from src.tools.web_search import _is_garbled
        assert _is_garbled("这是正常的中文文本123abc") is False

    def test_high_weird_ratio(self):
        """高比例奇异字符 → 乱码。"""
        from src.tools.web_search import _is_garbled
        # \x00-\x08 是控制字符但会被 _CONTROL_CHARS 命中，这里用非字母数字的
        # 普通标点 + 少量正常文本，形成低信噪比
        assert _is_garbled("a\u2603\u2604\u2620\u2621\u2622") is True


# ============================================================================
# _searx_load_cache / _searx_save_cache
# ============================================================================
class TestSearxCache:
    def test_load_cache_file_not_exists(self, tmp_path):
        from src.tools import web_search
        # 临时替换缓存路径
        old = web_search._SEARXNG_CACHE_PATH
        try:
            web_search._SEARXNG_CACHE_PATH = tmp_path / "nonexistent.json"
            assert web_search._searx_load_cache() == {}
        finally:
            web_search._SEARXNG_CACHE_PATH = old

    def test_load_cache_corrupted_json(self, tmp_path):
        from src.tools import web_search
        p = tmp_path / "corrupt.json"
        p.write_text("not json{")
        old = web_search._SEARXNG_CACHE_PATH
        try:
            web_search._SEARXNG_CACHE_PATH = p
            assert web_search._searx_load_cache() == {}
        finally:
            web_search._SEARXNG_CACHE_PATH = old

    def test_load_cache_expired(self, tmp_path):
        from src.tools import web_search
        import json
        p = tmp_path / "expired.json"
        p.write_text(json.dumps({"fetched_at": 0, "instances": []}))
        old = web_search._SEARXNG_CACHE_PATH
        try:
            web_search._SEARXNG_CACHE_PATH = p
            assert web_search._searx_load_cache() == {}
        finally:
            web_search._SEARXNG_CACHE_PATH = old

    def test_save_cache_ok(self, tmp_path):
        from src.tools import web_search
        old = web_search._SEARXNG_CACHE_PATH
        try:
            web_search._SEARXNG_CACHE_PATH = tmp_path / "save.json"
            web_search._searx_save_cache({"fetched_at": 99999999999, "instances": ["http://a"]})
            data = web_search._searx_load_cache()
            assert data["instances"] == ["http://a"]
        finally:
            web_search._SEARXNG_CACHE_PATH = old
