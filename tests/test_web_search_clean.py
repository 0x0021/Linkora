"""web_search 去噪 + 相关度重排测试（改动②）。

覆盖：
- _is_garbled 正确识别乱码（控制字符 / 大量非中英文奇异字符）
- _clean_and_rank 剔除乱码结果、按 query 相关度重排、截断到 top_n
- execute 回传 ranked 结果且 total_raw 记录原始条数
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.web_search import (  # noqa: E402
    WebSearchTool, _is_garbled, _clean_and_rank, _tokenize,
)


@pytest.fixture(autouse=True)
def _disable_real_network(monkeypatch):
    """防呆：禁用 web_search 的真实网络出口 ssrf_safe_get，避免 execute() 在
    未被 mock 的后端(searxng)上真实发请求（CI 无外网 + urllib3 版本错配会直接抛 TypeError）。
    抛 RuntimeError 使未 mock 的后端被 execute 安全回退为 []。
    """
    import src.tools.web_search as _ws

    def _blocked_ssrf(*args, **kwargs):
        raise RuntimeError("测试中被禁用的真实网络请求（web_search.ssrf_safe_get）")
    monkeypatch.setattr(_ws, "ssrf_safe_get", _blocked_ssrf)


def test_is_garbled_detects_control_chars():
    # 含真实控制字符（NUL / 设备控制符）应判为乱码
    assert _is_garbled("ZK\x00\x01Lu0018i p% E5\x0e dN") is True


def test_is_garbled_detects_foreign_spam():
    # 印地语/西里尔等大量非中英文 → 视为噪音
    assert _is_garbled("जनजातीय कार्य एवं अनुसूचित जाति कल्याण विभाग") is True


def test_is_garbled_accepts_normal_chinese():
    assert _is_garbled("珞石机器人递交招股书，拟赴港上市") is False


def test_tokenize_chinese_bigrams_and_english():
    toks = _tokenize("Rokae 上市 招股")
    assert "rokae" in toks
    assert "上市" in toks
    assert "招股" in toks


def test_clean_and_rank_promotes_relevant_and_drops_garbled():
    results = [
        {"title": "FIFA World Cup 2026 schedule", "url": "https://fifa.com",
         "snippet": "Match schedule fixtures results"},
        {"title": "Instagram", "url": "https://instagram.com",
         "snippet": "Create an account or log in to Instagram"},
        {"title": "珞石机器人 Rokae 递交ipo招股书", "url": "https://sohu.com/a/1",
         "snippet": "珞石机器人拟赴香港上市，中金保荐"},
        {"title": "俄语浏览器扩展", "url": "https://x.ru",
         "snippet": "Решена - Вредоносное расширение в Yandex Browser"},
        {"title": "珞石机器人港交所上市进展", "url": "https://sina.com/a/2",
         "snippet": "珞石机器人 03752 港股 发行价 招股书"},
    ]
    ranked = _clean_and_rank(results, "珞石机器人 上市 招股", top_n=3)
    # 5 条原始里仅 2 条命中 query（相关），其余（FIFA/Instagram/俄语乱码）应被剔除
    assert len(ranked) == 2
    titles = [r["title"] for r in ranked]
    assert "俄语浏览器扩展" not in titles
    assert "Instagram" not in titles
    assert "FIFA World Cup 2026 schedule" not in titles
    # 前两条均为真实相关的“珞石上市”结果（命中更多 query 分词者更靠前）
    assert titles[0].startswith("珞石机器人")
    assert titles[1].startswith("珞石机器人")


def test_clean_and_rank_fallback_when_all_garbled():
    """极端情况：全部乱码时退回原始，避免完全无料。"""
    bad = [{"title": "x", "url": "u", "snippet": "Решена - Вредоносное"}]
    out = _clean_and_rank(bad, "test", top_n=3)
    assert out == bad


def test_execute_strips_internal_debug_fields():
    """execute 不应把内部调试字段(_source/_from_query)或运维指标(total_raw/total_dedup)泄露给 LLM。"""
    tool = WebSearchTool()
    raw = [
        {"title": "Instagram", "url": "https://instagram.com", "snippet": "log in"},
        {"title": "珞石机器人上市", "url": "https://sohu.com", "snippet": "拟赴港上市"},
        {"title": "FIFA", "url": "https://fifa.com", "snippet": "schedule"},
    ]
    # execute 会遍历全部后端（bing + duckduckgo + searxng）并合并，故后端都要 mock，
    # 否则未 mock 的后端会真实联网、结果不可控；_fetch_page 亦需 mock（富集阶段联网）。
    with patch("src.tools.web_search.bing_search", return_value=raw), \
         patch("src.tools.web_search.duckduckgo_search", return_value=[]), \
         patch("src.tools.web_search.searxng_search", return_value=[]), \
         patch("src.tools.web_search._fetch_page", return_value=None):
        r = tool.execute({"query": "珞石机器人 上市", "num_results": 10})
    assert "results" in r
    # total_raw/total_dedup 已移至日志，不应出现在返回给 LLM 的 payload 中
    assert "total_raw" not in r
    assert "total_dedup" not in r
    # 每条结果不应携带内部调试字段
    for item in r["results"]:
        assert "_source" not in item
        assert "_from_query" not in item
    # 去噪后无关项被排到末尾，相关项在前
    assert r["results"][0]["title"] == "珞石机器人上市"
