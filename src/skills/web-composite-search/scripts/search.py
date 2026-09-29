#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web复合搜索 v2.0 - Playwright 版本
参考内置搜索技能实现方式，使用浏览器渲染绕过反爬

核心改进:
1. 使用 Playwright 替代 requests (模拟浏览器行为)
2. 添加 Google 引擎支持
3. 参考内置技能的完整引擎列表
4. 实现浏览器指纹模拟
"""

import json
import random
import time
import re
import warnings
from typing import List, Dict, Optional
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse
from pathlib import Path

# 过滤 BeautifulSoup XML 解析警告
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

# 尝试导入 Playwright
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("[警告] Playwright 未安装，将回退到 requests 模式")
    print("[提示] 运行: pip install playwright && python -m playwright install chromium")


@dataclass
class SearchResult:
    """搜索结果数据类"""
    title: str
    url: str
    snippet: str
    source_engine: str
    confidence_score: float = 0.5
    rank: int = 0


class CompositeSearch:
    """
    复合搜索引擎 - Playwright 版本

    参考内置搜索技能实现:
    - 使用浏览器渲染绕过反爬
    - 完整的引擎配置 (CN Web Search + 多引擎搜索)
    - 智能意图路由
    """

    # 完整的引擎配置 (25个引擎，参考内置技能)
    ENGINES = {
        # ===== 中文综合 (6个) =====
        "baidu": {
            "name": "百度",
            "search_url": "https://www.baidu.com/s?wd={query}",
            "result_selectors": [".result", ".c-container", "#content_left .result"],
            "type": "general",
            "requires_browser": True,
            "wait_for": "#content_left",
            "timeout": 15000
        },
        "so_360": {
            "name": "360搜索",
            "search_url": "https://m.so.com/s?q={query}",
            "result_selectors": [".result", ".g-card"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },
        "sogou": {
            "name": "搜狗",
            "search_url": "https://www.sogou.com/web?query={query}",
            "result_selectors": [".result", ".vrwrap", ".rb"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },
        "bing_cn": {
            "name": "必应中文",
            "search_url": "https://cn.bing.com/search?q={query}&ensearch=0",
            "result_selectors": [".b_algo", ".b_ans"],
            "type": "general",
            "requires_browser": True,
            "wait_for": "#b_content",
            "timeout": 12000
        },
        "bing_intl": {
            "name": "必应国际",
            "search_url": "https://www.bing.com/search?q={query}",
            "result_selectors": [".b_algo", ".b_ans"],
            "type": "general",
            "requires_browser": True,
            "wait_for": "#b_content",
            "timeout": 12000
        },
        "toutiao": {
            "name": "头条搜索",
            "search_url": "https://so.toutiao.com/search?keyword={query}",
            "result_selectors": [".result-content", ".cs-view", ".search-result"],
            "type": "general",
            "requires_browser": True,
            "timeout": 15000
        },

        # ===== Google (2个) =====
        "google": {
            "name": "Google",
            "search_url": "https://www.google.com/search?q={query}",
            "result_selectors": [".g", ".yuRUbf"],
            "type": "general",
            "requires_browser": True,
            "wait_for": "#rso, .g, #search",  # 更新选择器，优先 #rso (搜索结果区域)
            "timeout": 20000,
            "retry": 2
        },
        "google_hk": {
            "name": "Google HK",
            "search_url": "https://www.google.com.hk/search?q={query}",
            "result_selectors": [".g", ".yuRUbf"],
            "type": "general",
            "requires_browser": True,
            "wait_for": "#rso, .g, #search",
            "timeout": 20000,
            "retry": 2
        },

        # ===== 公众号/社交 (2个) =====
        "sogou_wechat": {
            "name": "搜狗微信",
            "search_url": "https://weixin.sogou.com/weixin?type=2&query={query}&page=1",
            "result_selectors": [".news-list li", ".txt-box", ".weui_media_box"],
            "type": "social",
            "requires_browser": False,
            "timeout": 10000
        },
        "bing_wechat": {
            "name": "必应公众号",
            "search_url": "https://cn.bing.com/search?q=site:mp.weixin.qq.com+{query}",
            "result_selectors": [".b_algo"],
            "type": "social",
            "requires_browser": True,
            "timeout": 12000
        },

        # ===== 英文/国际 (7个) =====
        "duckduckgo": {
            "name": "DuckDuckGo",
            "search_url": "https://lite.duckduckgo.com/lite/?q={query}",
            "result_selectors": [".result-link", ".links_main", ".result"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },
        "brave": {
            "name": "Brave Search",
            "search_url": "https://search.brave.com/search?q={query}",
            "result_selectors": [".snippet", ".result", ".card"],
            "type": "general",
            "requires_browser": True,
            "wait_for": "#results",
            "timeout": 15000
        },
        "qwant": {
            "name": "Qwant",
            "search_url": "https://www.qwant.com/?q={query}&t=web",
            "result_selectors": [".result"],
            "type": "general",
            "requires_browser": True,
            "timeout": 15000
        },
        "startpage": {
            "name": "Startpage",
            "search_url": "https://www.startpage.com/do/search?q={query}",
            "result_selectors": [".result", ".w-gl__result"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },
        "yahoo": {
            "name": "Yahoo",
            "search_url": "https://search.yahoo.com/search?p={query}",
            "result_selectors": [".algo", ".searchCenterMiddle"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },
        "mojeek": {
            "name": "Mojeek",
            "search_url": "https://www.mojeek.com/search?q={query}",
            "result_selectors": [".result"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },
        "ecosia": {
            "name": "Ecosia",
            "search_url": "https://www.ecosia.org/search?q={query}",
            "result_selectors": [".result"],
            "type": "general",
            "requires_browser": False,
            "timeout": 10000
        },

        # ===== 技术社区 (2个) =====
        "github": {
            "name": "GitHub",
            "search_url": "https://github.com/search?q={query}&type=repositories",
            "result_selectors": [".repo-list-item", "[data-testid='results-list'] > div"],
            "type": "tech",
            "requires_browser": True,
            "wait_for": "[data-testid='results-list']",
            "timeout": 20000
        },
        "stackoverflow": {
            "name": "Stack Overflow",
            # 使用 Google site: 搜索绕过 Cloudflare
            "search_url": "https://www.google.com/search?q=site:stackoverflow.com+{query}",
            "result_selectors": [".g", ".yuRUbf", ".Gx5Zad"],
            "type": "tech",
            "requires_browser": True,
            "wait_for": "#search, #rso, .g",
            "timeout": 20000,
            "retry": 2,
            "note": "通过Google site:搜索间接获取Stack Overflow内容，绕过Cloudflare"
        },

        # ===== 学术 (1个) =====
        "arxiv": {
            "name": "ArXiv",
            "search_url": "http://export.arxiv.org/api/query?search_query=all:{query}&max_results=10",
            "result_selectors": ["entry"],
            "type": "academic",
            "requires_browser": False,
            "is_api": True,
            "timeout": 20000
        },

        # ===== 财经 (3个) =====
        "eastmoney": {
            "name": "东方财富",
            # 东方财富搜索页面加载极慢且有反爬，改用必应 site: 搜索
            "search_url": "https://cn.bing.com/search?q=site:eastmoney.com+{query}",
            "result_selectors": [".b_algo", ".b_ans"],
            "type": "finance",
            "requires_browser": True,
            "wait_for": "#b_content",
            "timeout": 15000,
            "note": "通过必应site:搜索间接获取东方财富内容，绕过反爬"
        },
        "jisilu": {
            "name": "集思录",
            "search_url": "https://www.jisilu.cn/explore/?keyword={query}",
            "result_selectors": [".aw-question", ".aw-item"],
            "type": "finance",
            "requires_browser": True,
            "timeout": 15000
        },
        "caixin": {
            "name": "财新",
            # 财新原搜索页解析困难，改用必应 site: 搜索
            "search_url": "https://cn.bing.com/search?q=site:caixin.com+{query}",
            "result_selectors": [".b_algo", ".b_ans"],
            "type": "finance",
            "requires_browser": True,
            "wait_for": "#b_content",
            "timeout": 15000,
            "note": "通过必应site:搜索间接获取财新内容"
        },

        # ===== 知识百科 (4个) =====
        "wolfram": {
            "name": "Wolfram Alpha",
            "search_url": "https://www.wolframalpha.com/input?i={query}",
            "result_selectors": [".pod", "._3fR4V", "#main_pods"],
            "type": "knowledge",
            "requires_browser": True,
            "wait_for": "#main_pods",
            "timeout": 20000
        },
        "wikipedia_zh": {
            "name": "维基百科(中文)",
            "search_url": "https://zh.wikipedia.org/w/index.php?search={query}&title=Special:Search",
            "result_selectors": [".mw-search-result", "#mw-content-text"],
            "type": "knowledge",
            "requires_browser": False,
            "timeout": 10000
        },
        "wikipedia_en": {
            "name": "Wikipedia(EN)",
            "search_url": "https://en.wikipedia.org/w/index.php?search={query}&title=Special:Search",
            "result_selectors": [".mw-search-result"],
            "type": "knowledge",
            "requires_browser": False,
            "timeout": 10000
        },
        "ddg_api": {
            "name": "DDG Instant Answer",
            "search_url": "https://api.duckduckgo.com/?q={query}&format=json&no_html=1",
            "result_selectors": [],
            "type": "knowledge",
            "requires_browser": False,
            "is_api": True,
            "timeout": 10000
        }
    }

    # 搜索意图配置
    INTENT_CONFIG = {
        "general": {
            "engines": ["baidu", "so_360", "sogou", "bing_cn", "bing_intl", "toutiao", "google", "google_hk"],
            "max_concurrent": 4
        },
        "tech": {
            "engines": ["github", "stackoverflow", "bing_intl", "google", "sogou"],
            "max_concurrent": 3
        },
        "academic": {
            "engines": ["arxiv", "bing_intl", "google", "google_hk"],
            "max_concurrent": 2
        },
        "finance": {
            "engines": ["eastmoney", "jisilu", "caixin", "bing_cn", "google"],
            "max_concurrent": 3
        },
        "social": {
            "engines": ["sogou_wechat", "bing_wechat", "sogou"],
            "max_concurrent": 2
        },
        "privacy": {
            "engines": ["duckduckgo", "brave", "startpage", "qwant", "mojeek", "ecosia"],
            "max_concurrent": 3
        },
        "knowledge": {
            "engines": ["wikipedia_zh", "wikipedia_en", "wolfram", "ddg_api"],
            "max_concurrent": 3
        },
        "advanced": {
            "engines": ["baidu", "so_360", "sogou", "bing_cn", "bing_intl", "google", "google_hk"],
            "max_concurrent": 4
        }
    }

    # 浏览器指纹池
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0"
    ]

    def __init__(self):
        self.last_request_time = {}
        self.playwright_available = PLAYWRIGHT_AVAILABLE

    def search(self, query: str, intent: str | None = None, max_results: int = 10) -> Dict:
        """
        执行搜索

        Args:
            query: 搜索关键词
            intent: 搜索意图 (general/tech/academic/finance/social/privacy/knowledge/advanced)
            max_results: 最大返回结果数

        Returns:
            搜索结果字典
        """
        start_time = time.time()

        # 检测意图
        if not intent:
            intent = self._detect_intent(query)

        # 获取引擎列表
        intent_config = self.INTENT_CONFIG.get(intent, self.INTENT_CONFIG["general"])
        engine_ids = intent_config["engines"]
        max_concurrent = intent_config.get("max_concurrent", 3)

        print(f"[分析] 查询意图: {intent}")
        print(f"[分析] 选用引擎: {', '.join(self.ENGINES[e]['name'] for e in engine_ids)}\n")

        # 并行搜索
        all_results = []
        failed_engines = []

        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            future_to_engine = {
                executor.submit(self._search_single_engine, eid, query): eid
                for eid in engine_ids
            }

            for future in as_completed(future_to_engine):
                engine_id = future_to_engine[future]
                try:
                    results = future.result(timeout=45)
                    if results:
                        all_results.extend(results)
                        print(f"[成功] {self.ENGINES[engine_id]['name']}: 获取 {len(results)} 条结果")
                    else:
                        failed_engines.append(engine_id)
                        print(f"[警告] {self.ENGINES[engine_id]['name']}: 无结果")
                except Exception as e:
                    failed_engines.append(engine_id)
                    print(f"[错误] {self.ENGINES[engine_id]['name']}: {str(e)[:80]}")

        # 聚合排序
        aggregated = self._aggregate_results(all_results, max_results)

        execution_time = int((time.time() - start_time) * 1000)

        return {
            "query_analysis": {
                "original_query": query,
                "detected_intent": intent,
                "engines_selected": engine_ids,
                "engines_failed": failed_engines,
                "execution_time_ms": execution_time
            },
            "aggregated_results": [self._result_to_dict(r) for r in aggregated],
            "metadata": {
                "total_fetched": len(all_results),
                "after_deduplication": len(aggregated),
                "fallback_used": len(failed_engines) > 0,
                "playwright_available": self.playwright_available
            }
        }

    def _search_single_engine(self, engine_id: str, query: str) -> List[SearchResult]:
        """搜索单个引擎"""
        engine = self.ENGINES[engine_id]
        url = engine["search_url"].format(query=quote(query))

        # 应用请求频率限制
        self._apply_rate_limit(engine_id)

        try:
            # 根据引擎类型选择搜索方式
            if engine.get("requires_browser") and self.playwright_available:
                return self._search_with_browser(engine_id, query, url)
            else:
                return self._search_with_requests(engine_id, query, url)
        except Exception as e:
            print(f"[{engine_id}] 搜索失败: {str(e)[:100]}")
            return []

    def _search_with_browser(self, engine_id: str, query: str, url: str) -> List[SearchResult]:
        """使用 Playwright 浏览器搜索（带重试机制）"""
        engine = self.ENGINES[engine_id]
        max_retries = engine.get("retry", 1)
        timeout = engine.get("timeout", 15000)

        for attempt in range(max_retries):
            try:
                results = self._search_with_browser_single(engine_id, query, url, timeout)
                if results:
                    return results
                elif attempt < max_retries - 1:
                    print(f"[{engine_id}] 第 {attempt + 1} 次尝试无结果，{2 ** attempt}秒后重试...")
                    time.sleep(2 ** attempt)  # 指数退避
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"[{engine_id}] 第 {attempt + 1} 次尝试失败: {str(e)[:60]}，重试中...")
                    time.sleep(2 ** attempt)
                else:
                    raise

        return []

    def _search_with_browser_single(self, engine_id: str, query: str, url: str, timeout: int) -> List[SearchResult]:
        """单次浏览器搜索"""
        engine = self.ENGINES[engine_id]

        with sync_playwright() as p:
            # 启动浏览器
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process'
                ]
            )

            # 创建上下文 (模拟真实浏览器)
            context = browser.new_context(
                user_agent=random.choice(self.USER_AGENTS),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                permissions=[]
            )

            # 反检测脚本
            context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                window.chrome = { runtime: {} };
            """)

            page = context.new_page()

            try:
                # 根据引擎配置选择加载策略
                load_strategy = engine.get("load_strategy", "domcontentloaded")

                # 访问搜索页
                page.goto(url, wait_until=load_strategy, timeout=timeout)

                # 等待结果加载
                wait_for = engine.get("wait_for")
                if wait_for:
                    # 支持逗号分隔的多个选择器
                    selectors = [s.strip() for s in wait_for.split(",")]
                    last_error = None
                    for selector in selectors:
                        try:
                            page.wait_for_selector(selector, timeout=timeout)
                            break  # 成功则跳出
                        except Exception as e:
                            last_error = e
                            continue
                    else:
                        # 所有选择器都失败，但继续尝试解析页面
                        print(f"[{engine_id}] 等待选择器超时（最后错误: {last_error}），尝试直接解析...")
                else:
                    page.wait_for_load_state("networkidle", timeout=min(timeout, 15000))

                # 额外等待让JS渲染完成
                time.sleep(1.5)

                # 获取页面内容
                content = page.content()

                # 解析结果
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(content, "html.parser")
                results = self._parse_results(soup, engine_id)

                return results

            finally:
                browser.close()

    def _search_with_requests(self, engine_id: str, query: str, url: str) -> List[SearchResult]:
        """使用 requests 搜索 (用于不需要浏览器的引擎)"""
        import requests

        engine = self.ENGINES[engine_id]
        headers = {
            "User-Agent": random.choice(self.USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }

        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=(5, engine.get("timeout", 15000) / 1000),
                allow_redirects=True
            )
            response.raise_for_status()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, "html.parser")
            return self._parse_results(soup, engine_id)

        except Exception as e:
            print(f"[requests] {engine_id} 失败: {str(e)[:80]}")
            return []

    def _parse_results(self, soup, engine_id: str) -> List[SearchResult]:
        """解析搜索结果"""
        engine = self.ENGINES[engine_id]
        selectors = engine.get("result_selectors", [".result"])

        # 特殊处理 ArXiv XML API
        if engine_id == "arxiv":
            return self._parse_arxiv_results(soup)

        results = []

        for selector in selectors:
            items = soup.select(selector)
            for item in items[:10]:  # 每个引擎最多10条
                try:
                    result = self._extract_item(item, engine_id)
                    if result:
                        results.append(result)
                except Exception:
                    continue

        return results

    def _parse_arxiv_results(self, soup) -> List[SearchResult]:
        """专门解析 ArXiv Atom XML 格式"""
        results = []

        # ArXiv Atom XML 结构: <entry><title>...</title><id>...</id><summary>...</summary></entry>
        entries = soup.find_all("entry")

        for entry in entries[:10]:
            try:
                title_elem = entry.find("title")
                id_elem = entry.find("id")
                summary_elem = entry.find("summary")

                title = title_elem.get_text(strip=True) if title_elem else ""
                url = id_elem.get_text(strip=True) if id_elem else ""
                snippet = summary_elem.get_text(strip=True) if summary_elem else ""

                if title and url:
                    results.append(SearchResult(
                        title=title[:200],
                        url=url[:500],
                        snippet=snippet[:300],
                        source_engine="ArXiv",
                        confidence_score=0.5
                    ))
            except Exception:
                continue

        return results

    def _extract_item(self, item, engine_id: str) -> Optional[SearchResult]:
        """提取单个结果"""
        try:
            # 尝试多种选择器提取标题
            title_elem = (
                item.select_one("h3") or
                item.select_one("a") or
                item.select_one(".title") or
                item.select_one("[data-testid='result-title']")
            )

            # 尝试多种选择器提取链接
            link_elem = item.select_one("a[href]")

            # 尝试多种选择器提取摘要
            snippet_elem = (
                item.select_one(".content-right") or
                item.select_one(".snippet") or
                item.select_one(".abstract") or
                item.select_one("p") or
                item.select_one(".content")
            )

            title = title_elem.get_text(strip=True) if title_elem else ""
            url = link_elem["href"] if link_elem else ""
            snippet = snippet_elem.get_text(strip=True) if snippet_elem else ""

            # 清理 URL
            if url.startswith("/"):
                # 相对路径，需要补全
                pass

            if title and url:
                return SearchResult(
                    title=title[:200],
                    url=url[:500],
                    snippet=snippet[:300],
                    source_engine=self.ENGINES[engine_id]["name"],
                    confidence_score=0.5
                )
        except Exception:
            pass

        return None

    def _apply_rate_limit(self, engine_id: str):
        """应用请求频率限制"""
        now = time.time()
        last_time = self.last_request_time.get(engine_id, 0)
        interval = 0.5  # 默认间隔

        elapsed = now - last_time
        if elapsed < interval:
            time.sleep(interval - elapsed)

        self.last_request_time[engine_id] = time.time()

    def _detect_intent(self, query: str) -> str:
        """检测搜索意图"""
        query_lower = query.lower()

        # 高级语法检测
        if any(x in query for x in ["site:", "filetype:", "intitle:", "inurl:"]):
            return "advanced"

        # 学术意图
        academic_keywords = ["论文", "paper", "arxiv", "学术", "期刊", "research", "study", "journal"]
        if any(k in query_lower for k in academic_keywords):
            return "academic"

        # 技术意图
        tech_keywords = ["python", "javascript", "代码", "github", "stackoverflow", "编程", "开发", "code"]
        if any(k in query_lower for k in tech_keywords):
            return "tech"

        # 财经意图
        finance_keywords = ["股票", "基金", "财报", "a股", "投资", "理财", "股市", "stock", "finance"]
        if any(k in query_lower for k in finance_keywords):
            return "finance"

        # 社交/公众号
        social_keywords = ["公众号", "微信", "知乎", "微博", "小红书", "wechat"]
        if any(k in query_lower for k in social_keywords):
            return "social"

        return "general"

    def _aggregate_results(self, results: List[SearchResult], max_results: int) -> List[SearchResult]:
        """
        三重去重聚合排序

        去重层级 (从严格到宽松):
        1. URL规范化去重 - 完全相同URL只保留一个
        2. 标题相似度去重 - 相似度超过阈值认为是重复内容
        3. 内容指纹去重 - 基于正文摘要的相似度比对
        """
        import yaml
        from difflib import SequenceMatcher

        # 加载去重配置
        config_path = Path(__file__).parent.parent / "config.yaml"
        dedup_config = {
            "title_similarity_threshold": 0.82,
            "content_similarity_threshold": 0.78,
            "min_title_length": 8,
            "use_snippet_for_fingerprint": True
        }
        try:
            if config_path.exists():
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    if config and 'deduplication' in config:
                        dedup_config.update(config['deduplication'])
        except Exception:
            pass

        # 配置参数
        TITLE_THRESH = dedup_config.get('title_similarity_threshold', 0.82)
        CONTENT_THRESH = dedup_config.get('content_similarity_threshold', 0.78)
        MIN_TITLE_LEN = dedup_config.get('min_title_length', 8)
        USE_SNIPPET = dedup_config.get('use_snippet_for_fingerprint', True)

        def normalize_title(title: str) -> str:
            """标题标准化"""
            return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', '', title.lower())

        def similarity(a: str, b: str) -> float:
            """计算两个字符串的相似度 (0.0-1.0)"""
            if not a or not b:
                return 0.0
            return SequenceMatcher(None, a, b).ratio()

        # 按置信度排序
        sorted_results = sorted(results, key=lambda x: x.confidence_score, reverse=True)

        # 第一层：URL规范化去重
        seen_urls = set()
        url_unique_results = []
        for r in sorted_results:
            try:
                parsed = urlparse(r.url)
                # 规范化URL: 域名+路径，去除参数和尾部斜杠
                netloc = parsed.netloc.replace('www.', '')
                path = parsed.path.rstrip('/')
                url_key = f"{netloc}{path}".lower()

                if url_key not in seen_urls and len(url_key) > 10:
                    seen_urls.add(url_key)
                    url_unique_results.append(r)
            except Exception:
                url_unique_results.append(r)

        # 第二层：标题相似度去重
        title_unique_results = []
        seen_titles = []  # 存储已保留的标题（标准化后）

        for r in url_unique_results:
            norm_title = normalize_title(r.title)

            # 短标题跳过相似度检查
            if len(norm_title) < MIN_TITLE_LEN:
                title_unique_results.append(r)
                seen_titles.append(norm_title)
                continue

            # 检查是否与已保留的标题相似
            is_duplicate = False
            for existing_title in seen_titles:
                sim = similarity(norm_title, existing_title)
                if sim >= TITLE_THRESH:
                    is_duplicate = True
                    break

            if not is_duplicate:
                title_unique_results.append(r)
                seen_titles.append(norm_title)

        # 第三层：内容指纹去重（基于摘要）
        final_results = []
        seen_fingerprints = []

        for r in title_unique_results:
            if not USE_SNIPPET or not r.snippet:
                final_results.append(r)
                continue

            # 生成内容指纹（取摘要的关键词）
            snippet_norm = normalize_title(r.snippet[:200])  # 取前200字符

            # 短片段跳过
            if len(snippet_norm) < 20:
                final_results.append(r)
                continue

            # 检查内容相似度
            is_content_dup = False
            for fp in seen_fingerprints:
                content_sim = similarity(snippet_norm, fp)
                if content_sim >= CONTENT_THRESH:
                    is_content_dup = True
                    break

            if not is_content_dup:
                final_results.append(r)
                seen_fingerprints.append(snippet_norm)

        # 添加排名
        for i, r in enumerate(final_results[:max_results], 1):
            r.rank = i

        # 输出去重统计（调试用）
        if len(results) > 0:
            print(f"[去重统计] URL去重: {len(results)} → {len(url_unique_results)}, "
                  f"标题去重: → {len(title_unique_results)}, "
                  f"内容去重: → {len(final_results)}")

        return final_results[:max_results]

    def _result_to_dict(self, result: SearchResult) -> Dict:
        """转换结果为字典"""
        return {
            "rank": result.rank,
            "title": result.title,
            "url": result.url,
            "snippet": result.snippet,
            "source_engine": result.source_engine,
            "confidence_score": result.confidence_score
        }


# ===== CLI 入口 =====

def evaluate_search_outcome(results: Dict) -> "tuple[int, Optional[str]]":
    """判断搜索结果是否构成「有效交付」，返回 (退出码, 错误信息)。

    错误信息为 None 表示成功（退出码 0）；否则为明确的失败原因，调用方
    （SkillTool / LLM）应据此诚实告知用户，而非把空输出当成「成功」重试。

    - ``aggregated_results`` 非空 → 成功。
    - 为空且 Playwright 不可用 → 后端能力缺失（搜索必败），明确告知「不可用」。
    - 为空但 Playwright 可用 → 已尽力搜索但无果（查询过冷门/被反爬），诚实告知。

    设计动机：原实现无论是否拿到结果都 exit 0，导致 SkillTool 把空 stdout
    当作「成功」回给 LLM，LLM 误以为工具可用而反复重试并臆造噪声 query，
    浪费大量 token 却查不到结果。
    """
    aggregated = results.get("aggregated_results") or []
    if aggregated:
        return 0, None
    metadata = results.get("metadata") or {}
    playwright_available = metadata.get("playwright_available", False)
    if not playwright_available:
        msg = (
            "搜索后端不可用：Playwright 未安装，所有搜索引擎回退 requests 模式被反爬拦截，"
            "未返回任何有效结果。请改用其他信息来源，或安装 Playwright 后重试。"
        )
    else:
        msg = (
            "搜索执行完成，但所有引擎均未返回有效结果"
            "（关键词可能过冷门、或结果被反爬/地区限制拦截）。"
        )
    return 3, msg


def main():
    """命令行入口"""
    import argparse
    import sys
    import io

    # 设置Windows控制台UTF-8编码
    if sys.platform == 'win32':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description="web复合搜索 v2.0 - Playwright版本")
    parser.add_argument("query", help="搜索关键词")
    parser.add_argument("--intent", "-i",
                       choices=["general", "tech", "academic", "finance", "social", "privacy", "knowledge", "advanced"],
                       help="搜索意图")
    parser.add_argument("--max-results", "-n", type=int, default=10, help="最大返回结果数")
    parser.add_argument("--json", "-j", action="store_true", help="输出JSON格式")
    parser.add_argument("--output", "-o", help="输出结果到文件")

    args = parser.parse_args()

    # 检查 Playwright（信息性提示输出到 stderr，避免污染成功路径的 stdout）
    if not PLAYWRIGHT_AVAILABLE:
        print("="*60, file=sys.stderr)
        print("[警告] Playwright 未安装，将使用 requests 模式", file=sys.stderr)
        print("[提示] 如需完整功能，请运行:", file=sys.stderr)
        print("       pip install playwright", file=sys.stderr)
        print("       python -m playwright install chromium", file=sys.stderr)
        print("="*60 + "\n", file=sys.stderr)

    searcher = CompositeSearch()
    results = searcher.search(args.query, args.intent, args.max_results)

    # 诚实化（P0-1）：零结果必须让调用方知道是「能力缺失」还是「搜了没找到」，
    # 输出到 stderr 并以非零退出，使 SkillTool 将其判定为失败而非「假成功」。
    code, err = evaluate_search_outcome(results)
    if err:
        print(err, file=sys.stderr)
        sys.exit(code)

    # 构建输出
    if args.json:
        output = json.dumps(results, ensure_ascii=False, indent=2)
    else:
        lines = []
        lines.append("\n" + "="*60)
        lines.append("web复合搜索 v2.0 - Playwright版本")
        lines.append(f"查询: {results['query_analysis']['original_query']}")
        lines.append(f"意图: {results['query_analysis']['detected_intent']}")
        lines.append(f"用时: {results['query_analysis']['execution_time_ms']}ms")
        lines.append(f"引擎: {len(results['query_analysis']['engines_selected'])}个")
        lines.append(f"成功: {len(results['query_analysis']['engines_selected']) - len(results['query_analysis']['engines_failed'])}个")
        lines.append(f"去重前/后: {results['metadata']['total_fetched']}/{results['metadata']['after_deduplication']}")
        lines.append("="*60 + "\n")

        for r in results["aggregated_results"]:
            lines.append(f"{r['rank']}. {r['title']}")
            lines.append(f"   来源: {r['source_engine']} | 置信度: {r['confidence_score']}")
            lines.append(f"   {r['url']}")
            if r['snippet']:
                snippet = r['snippet'][:150] + "..." if len(r['snippet']) > 150 else r['snippet']
                lines.append(f"   {snippet}")
            lines.append("")

        output = "\n".join(lines)

    # 输出
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f"\n结果已保存到: {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
