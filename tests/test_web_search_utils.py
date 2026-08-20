"""web_search 模块的辅助函数和 WebSearchTool 测试。"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.tools.web_search import (
    _http_get,
    _strip_html,
    _decode_ddg_url,
    _is_garbled,
    _is_dictionary_noise,
    _tokenize,
    _score_result,
    _clean_and_rank,
    _fetch_page,
    _FETCH_PAGE_MAX_CHARS,
    WebSearchTool,
    bing_search,
    duckduckgo_search,
)


@pytest.fixture(autouse=True)
def _disable_real_network(monkeypatch):
    """防呆：本模块内任何「未显式 mock」的网络请求都立即失败，避免 CI/本地因代理或
    SSL 握手挂死（典型如 searxng 后端未被 mock 时，execute() 仍会真实发请求）。

    各单元测试仍可用 `with patch("requests.get")` / `with patch("src.tools.web_search.ssrf_safe_get")`
    覆盖本 fixture，以验证真实网络行为；但凡漏 mock 的后端都会在这里快速抛异常，
    而不是卡 60s+ 拖垮整条流水线，或被 CI 环境的 urllib3 版本错配（PoolKey 参数冲突）击穿。
    """
    import requests as _req

    def _blocked(*args, **kwargs):
        raise _req.ConnectionError("测试中被禁用的真实网络请求（存在未 mock 的网络调用）")
    monkeypatch.setattr(_req, "get", _blocked)

    # web_search 后端走 ssrf_safe_get(Session)，requests.get 不被使用，单独禁用避免漏网。
    # 抛 RuntimeError（execute 的 try 捕获 RuntimeError/ValueError）使未 mock 的后端安全回退 []，
    import src.tools.web_search as _ws

    def _blocked_ssrf(*args, **kwargs):
        raise RuntimeError("测试中被禁用的真实网络请求（web_search.ssrf_safe_get）")
    monkeypatch.setattr(_ws, "ssrf_safe_get", _blocked_ssrf)


# ============ _strip_html ============

class TestStripHtml:
    def test_remove_tags(self):
        assert _strip_html("<b>hello</b>") == "hello"

    def test_entities(self):
        assert _strip_html("a&nbsp;b &amp; c &lt;d&gt; &quot;e&quot;") == 'a b & c <d> "e"'

    def test_numeric_entities(self):
        assert _strip_html("&#12345;text") == "text"

    def test_empty(self):
        assert _strip_html("") == ""


# ============ _decode_ddg_url ============

class TestDecodeDdgUrl:
    def test_decode_uddg(self):
        assert _decode_ddg_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpath&rut=xx") == "https://example.com/path"

    def test_protocol_relative(self):
        assert _decode_ddg_url("//example.com/page") == "https://example.com/page"

    def test_passthrough(self):
        assert _decode_ddg_url("https://already.good.com") == "https://already.good.com"

    def test_empty(self):
        assert _decode_ddg_url("") == ""


# ============ _is_garbled ============

class TestIsGarbled:
    def test_normal_cn(self):
        assert _is_garbled("这是一段正常的中文介绍") is False

    def test_normal_en(self):
        assert _is_garbled("This is a normal English sentence.") is False

    def test_normal_mixed(self):
        assert _is_garbled("Rokae 珞石机器人完成 C 轮融资") is False

    def test_control_chars(self):
        assert _is_garbled("text\x00here") is True

    def test_high_ratio_weird(self):
        # 大部分非中英文且非 isalnum 的符号字符
        assert _is_garbled("★☆♠♣♥♦※←↑→↓⇒⇔") is True

    def test_empty(self):
        assert _is_garbled("") is False


# ============ _is_dictionary_noise ============

class TestIsDictionaryNoise:
    def test_title_hit(self):
        assert _is_dictionary_noise({"title": "珞（汉语文字）_百度百科", "snippet": ""}) is True

    def test_dict_terms_in_snippet(self):
        assert _is_dictionary_noise({
            "title": "珞",
            "snippet": "康熙字典记载：笔画10，部首王，集韻云..."
        }) is True

    def test_single_char_baike(self):
        assert _is_dictionary_noise({"title": "珞_百度百科", "snippet": "基本释义"}) is True

    def test_not_dict_noise(self):
        assert _is_dictionary_noise({
            "title": "珞石机器人获数亿元融资",
            "snippet": "珞石机器人完成新一轮融资"
        }) is False

    def test_han_dian(self):
        assert _is_dictionary_noise({"title": "珞的解释_汉典", "snippet": ""}) is True

    def test_regex_match_non_word_baike(self):
        """非单词字符+百科触发 regex 匹配分支 (line 224-225)。"""
        # ★ 不是 \w（不像 CJK 字符被 isalnum 命中），触发 [^\w\s] 路径
        assert _is_dictionary_noise({"title": "★（百科", "snippet": ""}) is True
        assert _is_dictionary_noise({"title": "#_百科词条", "snippet": ""}) is True


# ============ _tokenize ============

class TestTokenize:
    def test_english(self):
        assert _tokenize("hello world") == ["hello", "world"]

    def test_chinese(self):
        assert _tokenize("珞石机器人") == ["珞石", "石机", "机器", "器人"]

    def test_single_chinese(self):
        assert _tokenize("珞") == ["珞"]

    def test_mixed(self):
        toks = _tokenize("Rokae 珞石")
        assert "rokae" in toks
        assert "珞石" in toks

    def test_empty(self):
        assert _tokenize("") == []


# ============ _score_result ============

class TestScoreResult:
    def test_full_match(self):
        item = {"title": "珞石机器人融资", "snippet": "珞石完成C轮融资"}
        score = _score_result(item, _tokenize("珞石"))
        assert score > 0

    def test_no_match(self):
        item = {"title": "FIFA World Cup", "snippet": "football"}
        score = _score_result(item, _tokenize("珞石"))
        assert score == 0

    def test_no_snippet(self):
        item = {"title": "珞石机器人官网"}
        score = _score_result(item, _tokenize("珞石"))
        assert score > 0


# ============ _clean_and_rank ============

class TestCleanAndRank:
    def test_basic(self):
        results = [
            {"title": "FIFA World Cup", "snippet": "football tournament"},
            {"title": "珞石机器人官网", "snippet": "珞石完成融资"},
            {"title": "珞石招聘", "snippet": "加入我们"},
        ]
        cleaned = _clean_and_rank(results, "珞石", top_n=5)
        titles = [r["title"] for r in cleaned]
        # 珞石相关应排在前面
        assert "珞石机器人官网" in titles[:2]

    def test_removes_garbled(self):
        results = [
            {"title": "Good", "snippet": "normal text"},
            {"title": "Bad", "snippet": "\x00garbled\x1f"},
        ]
        cleaned = _clean_and_rank(results, "Good")
        assert len(cleaned) == 1
        assert cleaned[0]["title"] == "Good"

    def test_removes_dict_noise(self):
        results = [
            {"title": "珞（汉语文字）_百度百科", "snippet": "康熙字典"},
            {"title": "珞石公司", "snippet": "机器人企业"},
        ]
        cleaned = _clean_and_rank(results, "珞石", top_n=5)
        assert len(cleaned) == 1
        assert cleaned[0]["title"] == "珞石公司"

    def test_fallback_when_dict_noise_only(self):
        results = [
            {"title": "珞（汉语文字）_百度百科", "snippet": "康熙字典"},
        ]
        cleaned = _clean_and_rank(results, "珞石", top_n=5)
        # 退回原始结果
        assert len(cleaned) == 1

    def test_empty_results(self):
        assert _clean_and_rank([], "test") == []

    def test_only_zero_score(self):
        results = [
            {"title": "Unrelated A", "snippet": "foo bar baz"},
            {"title": "Unrelated B", "snippet": "qux quux corge"},
        ]
        cleaned = _clean_and_rank(results, "珞石", top_n=5)
        # 全部 0 分，保留全部
        assert len(cleaned) == 2

    def test_skip_empty_title_and_snippet(self):
        """标题和摘要均为空的结果应被跳过 (line 264)。"""
        results = [
            {"title": "", "snippet": ""},
            {"title": "Valid", "snippet": "content"},
        ]
        cleaned = _clean_and_rank(results, "test")
        assert len(cleaned) == 1
        assert cleaned[0]["title"] == "Valid"

    def test_multi_query_takes_max_score_across_queries(self):
        """多 query（list[str]）下，结果取与任一 query 的最高分词命中分。

        验证统一后的单一实现：一条仅命中第二个 query 的结果不应被当作 0 分丢弃。
        """
        results = [
            # 仅命中 "Rokae 招股" 的第二条 query
            {"title": "Rokae 招股书", "snippet": "Rokae 递交招股书"},
            # 仅命中 "珞石 上市" 的第一条 query
            {"title": "珞石上市", "snippet": "珞石拟赴港上市"},
            # 两条都不命中
            {"title": "FIFA", "snippet": "world cup"},
        ]
        cleaned = _clean_and_rank(results, ["珞石 上市", "Rokae 招股"], top_n=10)
        titles = [r["title"] for r in cleaned]
        assert "FIFA" not in titles
        assert "Rokae 招股书" in titles
        assert "珞石上市" in titles

    def test_accepts_single_query_string_backward_compat(self):
        """兼容旧的单 query 字符串调用（execute 改造前既有测试契约）。"""
        results = [
            {"title": "珞石机器人官网", "snippet": "珞石完成融资"},
            {"title": "Unrelated", "snippet": "foo bar"},
        ]
        cleaned = _clean_and_rank(results, "珞石")
        assert cleaned[0]["title"] == "珞石机器人官网"


# ============ _fetch_page ============

class TestFetchPage:
    def test_article_tag(self):
        html = "<html><body><main>正文区域有很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长长长长长长长长长长长长长长</main></body></html>"
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=html, headers={"Content-Type": "text/html; charset=utf-8"})
            result = _fetch_page("https://example.com")
        assert result is not None
        assert "正文区域" in result

    def test_main_tag(self):
        html = "<html><body><main>Main content area with significant text content that is long enough to pass the minimum threshold check.</main></body></html>"
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=html, headers={"Content-Type": "text/html"})
            result = _fetch_page("https://example.com")
        assert result is not None
        assert "Main content" in result

    def test_div_content_class(self):
        html = '<html><body><div class="content">' + "X" * 200 + "</div></body></html>"
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=html, headers={"Content-Type": "text/html"})
            result = _fetch_page("https://example.com")
        assert result is not None
        assert len(result) <= _FETCH_PAGE_MAX_CHARS

    def test_fallback_to_body(self):
        html = "<html><body><p>" + "abc " * 50 + "</p></body></html>"
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=html, headers={"Content-Type": "text/html"})
            result = _fetch_page("https://example.com")
        assert result is not None
        assert "abc" in result

    def test_network_error(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.side_effect = requests.ConnectionError("timeout")
            result = _fetch_page("https://example.com")
        assert result is None

    def test_not_html(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(
                status_code=200, text="binary stuff",
                headers={"Content-Type": "application/octet-stream"}
            )
            result = _fetch_page("https://example.com")
        assert result is None

    def test_no_body_tag(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(
                status_code=200, text="no html here",
                headers={"Content-Type": "text/html"}
            )
            result = _fetch_page("https://example.com")
        assert result is None

    def test_truncate_long(self):
        long_text = "X" * (_FETCH_PAGE_MAX_CHARS + 500)
        html = f"<html><body><article>{long_text}</article></body></html>"
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=html, headers={"Content-Type": "text/html"})
            result = _fetch_page("https://example.com")
        assert result is not None
        assert len(result) <= _FETCH_PAGE_MAX_CHARS


# ============ _http_get ============

class TestHttpGet:
    def test_success_first_try(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200)
            _http_get("https://example.com", timeout=5)
            assert m.call_count == 1

    def test_retry_on_error(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.side_effect = [
                requests.ConnectionError("first"),
                MagicMock(status_code=200),
            ]
            _http_get("https://example.com", timeout=5, retries=3)
            assert m.call_count == 2

    def test_exhaust_retries(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.side_effect = requests.ConnectionError("fail")
            with pytest.raises(requests.ConnectionError):
                _http_get("https://example.com", timeout=5, retries=2)
            assert m.call_count == 2


# ============ bing_search / duckduckgo_search ============

class TestSearchFunctions:
    BING_HTML = """
    <html><body><ol>
    <li class="b_algo"><h2><a href="https://rok ac.cn">Rokae 官网</a></h2>
        <div class="b_caption"><p>珞石机器人，新一代智能机器人专家</p></div>
    </li>
    <li class="b_algo"><h2><a href="https://news.example.com/worldcup">World Cup 2026</a></h2>
        <div class="b_caption"><p>FIFA World Cup news</p></div>
    </li>
    </ol></body></html>
    """

    DDG_HTML = """
    <html><body>
    <div class="result result--a">
        <a class="result__a" href="https://example.com/article">Example Title</a>
        <div class="result__snippet">Example snippet text</div>
    </div>
    </body></html>
    """

    # --- 覆盖未达行的 HTML 变体 ---

    BING_HTML_NO_TITLE = """<html><body><ol>
<li class="b_algo"><div>no h2 here</div></li>
<li class="b_algo"><h2><a href="https://example.com">Valid</a></h2>
    <div class="b_caption"><p>snippet</p></div>
</li>
</ol></body></html>"""

    BING_HTML_NO_LINK = """<html><body><ol>
<li class="b_algo"><h2><a>No Href</a></h2>
    <div class="b_caption"><p>snippet</p></div>
</li>
<li class="b_algo"><h2><a href="https://example.com">Valid</a></h2>
    <div class="b_caption"><p>snippet</p></div>
</li>
</ol></body></html>"""

    BING_HTML_DUPLICATE = """<html><body><ol>
<li class="b_algo"><h2><a href="https://example.com">First</a></h2>
    <div class="b_caption"><p>snippet</p></div>
</li>
<li class="b_algo"><h2><a href="https://example.com">Dup</a></h2>
    <div class="b_caption"><p>snippet</p></div>
</li>
<li class="b_algo"><h2><a href="https://other.com">Other</a></h2>
    <div class="b_caption"><p>snippet</p></div>
</li>
</ol></body></html>"""

    BING_HTML_MANY = (
        "<html><body><ol>"
        + "".join(
            '<li class="b_algo"><h2><a href="https://x.com/%d">R%d</a></h2>'
            '<div class="b_caption"><p>s%d</p></div></li>' % (i, i, i)
            for i in range(10)
        )
        + "</ol></body></html>"
    )

    DDG_HTML_NO_MATCH = """<html><body>
<div class="result result--a">
    <div class="result__snippet">no link here</div>
</div>
<div class="result result--a">
    <a class="result__a" href="https://example.com">Valid</a>
    <div class="result__snippet">snippet</div>
</div>
</body></html>"""

    DDG_HTML_NO_LINK = """<html><body>
<div class="result result--a">
    <a class="result__a">No Href</a>
    <div class="result__snippet">snippet</div>
</div>
<div class="result result--a">
    <a class="result__a" href="https://example.com">Valid</a>
    <div class="result__snippet">snippet</div>
</div>
</body></html>"""

    DDG_HTML_DUPLICATE = """<html><body>
<div class="result result--a">
    <a class="result__a" href="https://dup.com">First</a>
    <div class="result__snippet">snippet</div>
</div>
<div class="result result--a">
    <a class="result__a" href="https://dup.com">Dup</a>
    <div class="result__snippet">snippet</div>
</div>
<div class="result result--a">
    <a class="result__a" href="https://other.com">Other</a>
    <div class="result__snippet">snippet</div>
</div>
</body></html>"""

    DDG_HTML_MANY = (
        "<html><body>"
        + "".join(
            '<div class="result result--a">'
            '<a class="result__a" href="https://x.com/%d">R%d</a>'
            '<div class="result__snippet">s%d</div></div>' % (i, i, i)
            for i in range(10)
        )
        + "</body></html>"
    )

    def test_bing_search(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.BING_HTML)
            results = bing_search("珞石", num_results=5)
        assert len(results) >= 1

    def test_bing_network_error(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.side_effect = requests.ConnectionError("fail")
            results = bing_search("test")
        assert results == []

    def test_bing_http_error(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            resp = MagicMock(status_code=500)
            resp.raise_for_status.side_effect = requests.HTTPError("500")
            m.return_value = resp
            results = bing_search("test")
        assert results == []

    def test_duckduckgo_search(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.DDG_HTML)
            results = duckduckgo_search("example")
        assert len(results) >= 1

    def test_duckduckgo_error(self):
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.side_effect = requests.ConnectionError("fail")
            results = duckduckgo_search("test")
        assert results == []

    # --- Bing HTML 解析未覆盖路径 ---

    def test_bing_no_title(self):
        """block 无 h2 → continue (line 89)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.BING_HTML_NO_TITLE)
            results = bing_search("test", num_results=5)
        assert len(results) >= 1

    def test_bing_no_link(self):
        """block 有 h2 但无 href → continue (line 98)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.BING_HTML_NO_LINK)
            results = bing_search("test", num_results=5)
        assert len(results) >= 1

    def test_bing_duplicate_link(self):
        """重复链接跳过 (line 98)，只保留第一条 + 其他链接。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.BING_HTML_DUPLICATE)
            results = bing_search("test", num_results=5)
        assert len(results) == 2  # First + Other, Dup 跳过

    def test_bing_break_at_limit(self):
        """结果数达 num_results 时 break (line 111)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.BING_HTML_MANY)
            results = bing_search("test", num_results=3)
        assert len(results) == 3

    # --- DDG HTML 解析未覆盖路径 ---

    def test_ddg_no_match(self):
        """block 无 result__a → continue (line 137)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.DDG_HTML_NO_MATCH)
            results = duckduckgo_search("test")
        assert len(results) >= 1

    def test_ddg_no_link(self):
        """result__a 无 href → _decode_ddg_url 返回空 → continue (line 141)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.DDG_HTML_NO_LINK)
            results = duckduckgo_search("test")
        assert len(results) >= 1

    def test_ddg_duplicate_link(self):
        """重复链接跳过 (line 141)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.DDG_HTML_DUPLICATE)
            results = duckduckgo_search("test")
        assert len(results) == 2  # First + Other, Dup 跳过

    def test_ddg_break_at_limit(self):
        """结果数达 num_results 时 break (line 151)。"""
        with patch("src.tools.web_search.ssrf_safe_get") as m:
            m.return_value = MagicMock(status_code=200, text=self.DDG_HTML_MANY)
            results = duckduckgo_search("test", num_results=4)
        assert len(results) == 4


# ============ WebSearchTool ============

class TestWebSearchTool:
    def test_execute_no_query(self):
        tool = WebSearchTool()
        result = tool.execute({})
        assert result.get("error")

    def test_execute_with_bing(self):
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.return_value = [
                {"title": "珞石机器人", "url": "https://example.com", "snippet": "好公司"}
            ]
            with patch("src.tools.web_search._fetch_page", return_value="正文..."):
                tool = WebSearchTool()
                result = tool.execute({"query": "珞石"})
        assert result["source"] == "bing"
        assert len(result["results"]) > 0

    def test_execute_bing_fails_fallback_to_ddg(self):
        with patch("src.tools.web_search.bing_search", return_value=[]):
            with patch("src.tools.web_search.duckduckgo_search") as m_ddg:
                m_ddg.return_value = [
                    {"title": "Result", "url": "https://x.com", "snippet": "ok"}
                ]
                with patch("src.tools.web_search._fetch_page", return_value=None):
                    tool = WebSearchTool()
                    result = tool.execute({"query": "test"})
        # bing 无结果 → merged_sources 仅含 duckduckgo
        assert result["source"] == "duckduckgo"
        assert "duckduckgo" in result["merged_sources"]
        assert "bing" not in result["merged_sources"]

    def test_execute_all_fail(self):
        with patch("src.tools.web_search.bing_search", return_value=[]):
            with patch("src.tools.web_search.duckduckgo_search", return_value=[]):
                with patch("src.tools.web_search.searxng_search", return_value=[]):
                    tool = WebSearchTool()
                    result = tool.execute({"query": "test"})
        assert "error" in result
        # 三个后端（必应→DuckDuckGo→SearXNG 兜底）均被尝试
        assert result.get("tried") == ["bing", "duckduckgo", "searxng"]

    def test_execute_fetch_page_enriches(self):
        """top 2 结果应被 _fetch_page 富化。"""
        with patch("src.tools.web_search.bing_search") as m:
            m.return_value = [
                {"title": f"R{i}", "url": f"https://x.com/{i}", "snippet": "test"}
                for i in range(5)
            ]
            with patch("src.tools.web_search._fetch_page") as m_fetch:
                m_fetch.return_value = "content here"
                tool = WebSearchTool()
                result = tool.execute({"query": "test"})
        # 前2条应有 content 字段
        assert result["results"][0].get("content") == "content here"
        assert result["results"][1].get("content") == "content here"
        # 第3条不应该被 fetch
        assert "content" not in result["results"][2]

    def test_execute_bing_exception_fallback(self):
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.side_effect = RuntimeError("boom")
            with patch("src.tools.web_search.duckduckgo_search") as m_ddg:
                m_ddg.return_value = [{"title": "ok", "url": "https://x.com", "snippet": "ok"}]
                with patch("src.tools.web_search._fetch_page", return_value=None):
                    tool = WebSearchTool()
                    result = tool.execute({"query": "test"})
        assert result["source"] == "duckduckgo"

    def test_execute_missing_backend_fn(self):
        """globals().get(fn_name) 返回 None 时 continue (line 378)。"""
        with patch("src.tools.web_search._SEARCH_BACKEND_NAMES", [("fake", "nonexistent_fn")]):
            tool = WebSearchTool()
            result = tool.execute({"query": "test"})
        assert "error" in result


class TestWebSearchToolMultiQuery:
    """web_search 多 query 数组支持 — 现场「周星驰最新电影」场景修复
    (commit: 必应单 query「周星驰 最新上映 2025」返回「周朝字源」,
    3 个角度 queries 一次返回 5 条功夫女足真信息)
    """

    def test_queries_array_takes_precedence(self):
        """queries 数组被使用,query 字符串会与 queries 合并去重。"""
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.side_effect = lambda q, **kw: [
                {"title": f"result for {q}", "url": f"https://x.com/{q}", "snippet": q}
            ]
            with patch("src.tools.web_search.duckduckgo_search", return_value=[]):
                with patch("src.tools.web_search._fetch_page", return_value=None):
                    tool = WebSearchTool()
                    result = tool.execute({
                        "queries": ["q1", "q2", "q3"],
                        "query": "ignored",
                    })
        # query 字符串会与 queries 合并(向后兼容)
        assert result["queries"] == ["q1", "q2", "q3", "ignored"]
        assert result["query"] == "q1"  # primary 是第一个
        # bing 调了 4 次(3 query + 1 fallback 字符串)
        assert m_bing.call_count == 4

    def test_queries_fallback_to_single_query_string(self):
        """无 queries 数组时,fallback 到单 query 字符串(向后兼容)。"""
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.return_value = [{"title": "t", "url": "https://x.com", "snippet": "s"}]
            with patch("src.tools.web_search.duckduckgo_search", return_value=[]):
                with patch("src.tools.web_search._fetch_page", return_value=None):
                    tool = WebSearchTool()
                    result = tool.execute({"query": "single"})
        assert result["queries"] == ["single"]
        assert result["query"] == "single"

    def test_queries_dedup_keeps_order(self):
        """queries 数组去重保序(避免 LLM 重复 query 浪费)。"""
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.return_value = [{"title": "t", "url": "https://x.com", "snippet": "s"}]
            with patch("src.tools.web_search.duckduckgo_search", return_value=[]):
                with patch("src.tools.web_search._fetch_page", return_value=None):
                    tool = WebSearchTool()
                    result = tool.execute({
                        "queries": ["a", "b", "a", "c", "b"],
                    })
        assert result["queries"] == ["a", "b", "c"]
        # bing 调了 3 次(去重后)
        assert m_bing.call_count == 3

    def test_queries_capped_at_4(self):
        """queries 上限 4,超过部分截断(防 LLM 滥用)。"""
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.return_value = []
            with patch("src.tools.web_search.duckduckgo_search", return_value=[]):
                tool = WebSearchTool()
                result = tool.execute({
                    "queries": ["a", "b", "c", "d", "e", "f", "g"],
                })
        assert len(result["queries"]) == 4
        assert m_bing.call_count == 4

    def test_queries_none_falls_back(self):
        """queries=null 不报错,fallback 到单 query。"""
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.return_value = [{"title": "t", "url": "https://x.com", "snippet": "s"}]
            with patch("src.tools.web_search.duckduckgo_search", return_value=[]):
                with patch("src.tools.web_search._fetch_page", return_value=None):
                    tool = WebSearchTool()
                    result = tool.execute({"queries": None, "query": "fallback"})
        assert result["queries"] == ["fallback"]

    def test_queries_cross_backend_merging(self):
        """多 query × 多后端:bing+DDG 都跑,合并去重。"""
        with patch("src.tools.web_search.bing_search") as m_bing:
            m_bing.side_effect = lambda q, **kw: [
                {"title": f"bing-{q}", "url": f"https://bing.com/{q}", "snippet": q},
            ]
            with patch("src.tools.web_search.duckduckgo_search") as m_ddg:
                m_ddg.side_effect = lambda q, **kw: [
                    {"title": f"ddg-{q}", "url": f"https://ddg.com/{q}", "snippet": q},
                ]
                with patch("src.tools.web_search.searxng_search", return_value=[]):
                    with patch("src.tools.web_search._fetch_page", return_value=None):
                        tool = WebSearchTool()
                        result = tool.execute({"queries": ["q1", "q2"]})
        # 2 query × 2 后端 = 4 raw(URL 不同,不去重)；total_raw/total_dedup 已移至日志,不进 payload
        assert "total_raw" not in result
        assert "total_dedup" not in result
        titles = {r["title"] for r in result["results"]}
        assert titles == {"bing-q1", "bing-q2", "ddg-q1", "ddg-q2"}
        # 合并了两个后端
        assert "bing" in result["merged_sources"]
        assert "duckduckgo" in result["merged_sources"]
        # 内部调试字段已剥离,不泄露给 LLM
        for r in result["results"]:
            assert "_from_query" not in r
            assert "_source" not in r

    def test_queries_no_query_no_string_returns_error(self):
        """既无 queries 也无 query 字符串时,返回 error 不炸。"""
        tool = WebSearchTool()
        result = tool.execute({})
        assert "error" in result
