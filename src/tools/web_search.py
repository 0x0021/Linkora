from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from src.tools.base import BaseTool
from src.tools.utils import safe_int as _safe_int
from src.utils.net import ssrf_safe_get

# SearXNG 实例缓存（从 searx.space 动态发现，本地落盘 + 轮换 + 冷却）
_SEARXNG_CACHE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "searx_instances.json"
)
_SEARXNG_DISCOVERY_URL = "https://searx.space/data/instances.json"
_SEARXNG_CACHE_TTL_SECONDS = 24 * 3600  # 实例列表每天刷新一次
_SEARXNG_INSTANCE_COOLDOWN = 10 * 60  # 某实例 429/挑战后冷却 10 分钟
_SEARXNG_TRY_PER_QUERY = 2  # 单次搜索最多尝试几个实例
_SEARXNG_ENABLED = os.environ.get("ENABLE_SEARXNG", "1") != "0"  # 默认开启，可关

# 内存态：实例轮换游标 + 冷却到期时间（进程内，重启后从缓存恢复）
_searx_state: dict = {"cursor": 0, "cooldown": {}}
# 缓存读写锁：避免 _searx_pick_instance（推进游标）与 _searx_mark_bad（写冷却）
# 并发 read-modify-write 时互相覆盖（典型丢更新：mark_bad 的冷却被 pick 写回的旧 cooldown 顶掉，
# 导致坏实例未被真正冷却而反复重试）。用 RLock 以支持同一线程重入（discover 内也会写缓存）。
_searx_cache_lock = threading.RLock()


def _searx_load_cache() -> dict:
    """读取本地 searx 实例缓存；无/过期则返回空触发重新发现。"""
    if not _SEARXNG_CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(_SEARXNG_CACHE_PATH.read_text(encoding="utf-8"))
        if time.time() - data.get("fetched_at", 0) > _SEARXNG_CACHE_TTL_SECONDS:
            return {}
        return data
    except Exception:
        logger.debug("SearXNG 缓存读取失败，返回空缓存")
        return {}


def _searx_save_cache(data: dict) -> None:
    """原子写缓存：先写临时文件再 os.replace，避免写一半崩溃导致缓存损坏。"""
    try:
        _SEARXNG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8",
            dir=str(_SEARXNG_CACHE_PATH.parent),
            prefix=".searx_", suffix=".tmp", delete=False,
        )
        try:
            json.dump(data, tmp, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.replace(tmp.name, _SEARXNG_CACHE_PATH)
    except Exception as e:
        logger.debug("保存 searx 缓存失败: %s", e)


def _searx_discover() -> list[str]:
    """从 searx.space 抓取实例列表，筛选健康实例（https + 在线 + 高可用）。

    返回按可用度排序的实例 URL 列表。失败时返回空（调用方回退到其它后端）。
    """
    try:
        resp = ssrf_safe_get(
            _SEARXNG_DISCOVERY_URL, headers=_HEADERS, timeout=15, allow_redirects=True
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("searx.space 实例列表拉取失败: %s", e)
        return []

    instances = payload.get("instances") or {}
    candidates = []
    for url, meta in instances.items():
        if not url.startswith("https://"):
            continue
        if meta.get("network_type") == "tor":
            continue
        http = meta.get("http") or {}
        if http.get("status_code") != 200:
            continue
        # 主实例优先、周可用率>=95、生成器为 searxng
        uptime_week = (meta.get("uptime", {}) or {}).get("uptimeWeek", 0) or 0
        if uptime_week < 95:
            continue
        if meta.get("generator") and "searxng" not in meta["generator"].lower():
            continue
        search = (meta.get("timing", {}) or {}).get("search", {})
        success = search.get("success_percentage", 100) or 100
        candidates.append((url, uptime_week, success, bool(meta.get("main"))))

    # 排序：主实例 > 年度可用率 > 搜索成功率
    candidates.sort(key=lambda c: (c[3], c[1], c[2]), reverse=True)
    urls = [c[0] for c in candidates]
    if urls:
        _searx_save_cache({
            "fetched_at": int(time.time()),
            "urls": urls,
            "cursor": 0,
            "cooldown": {},
        })
        logger.info("searx.space 发现 %d 个可用实例", len(urls))
    return urls


def _searx_pick_instance() -> Optional[str]:
    """轮换选一个未冷却的实例；返回 None 表示暂时无可用实例。"""
    with _searx_cache_lock:
        cache = _searx_load_cache()
        urls = cache.get("urls") or []
        if urls:
            # 缓存有实例：直接在锁内挑选（read-modify-write 受锁保护）
            return _searx_select_from_cache(cache, urls)

    # 缓存无实例：先解锁再做网络发现（15s 超时），避免阻塞其它线程的搜索
    fresh = _searx_discover()
    if fresh:
        with _searx_cache_lock:
            cache = {
                "fetched_at": int(time.time()),
                "urls": fresh,
                "cursor": 0,
                "cooldown": {},
            }
            _searx_save_cache(cache)
            _searx_state["current_urls"] = fresh
            return _searx_select_from_cache(cache, fresh)

    # 发现失败：再读一次缓存，也许其它线程已写好
    with _searx_cache_lock:
        cache = _searx_load_cache()
        urls = cache.get("urls") or []
        if urls:
            return _searx_select_from_cache(cache, urls)
    return None


def _searx_select_from_cache(cache: dict, urls: list) -> Optional[str]:
    """在已持锁且缓存含 urls 的前提下，轮换挑选一个未冷却实例并持久化游标。

    调用方必须已持有 _searx_cache_lock（本函数内会写回缓存）。
    """
    cooldown = cache.get("cooldown", {}) or {}
    now = time.time()
    # 清理过期冷却
    cooldown = {k: v for k, v in cooldown.items() if v > now}
    n = len(urls)
    start = int(cache.get("cursor", 0)) % n
    for i in range(n):
        idx = (start + i) % n
        url = urls[idx]
        if url not in cooldown:
            # 更新游标（下次从下一个开始，均匀分散压力）
            cache["cursor"] = (idx + 1) % n
            cache["cooldown"] = cooldown
            _searx_save_cache(cache)
            return url
    return None


def _searx_mark_bad(url: str) -> None:
    """标记实例冷却（429/JS 挑战后）。"""
    with _searx_cache_lock:
        cache = _searx_load_cache()
        # 缓存缺失时（首次未落盘）用内存态兜底，避免丢 urls/cursor
        cache.setdefault("urls", list(_searx_state.get("current_urls", [])))
        cache.setdefault("cursor", 0)
        cooldown = cache.get("cooldown", {}) or {}
        cooldown[url] = time.time() + _SEARXNG_INSTANCE_COOLDOWN
        cache["cooldown"] = cooldown
        _searx_save_cache(cache)


def _searx_is_challenge(html: str) -> bool:
    """检测 Anubis / 反爬 JS 挑战页（这些页面不是搜索结果，需换实例）。"""
    low = html.lower()
    return (
        "not a bot" in low
        or "anubis" in low
        or "making sure you" in low
        or "verify you are human" in low
    )


def _searx_is_index_page(html: str) -> bool:
    """检测实例是否返回了首页而非搜索结果页。

    公共 SearXNG 实例对匿名请求常直接返回首页（<meta endpoint=index>）
    或导航页（不含任何结果条目），这类响应不是搜索结果、且实例基本不可用，
    应视为"实例不可用"触发冷却+轮换，避免反复浪费请求。
    """
    if not html:
        return True
    low = html.lower()
    # SearXNG 首页标记
    if 'name="endpoint" content="index"' in low:
        return True
    if '<meta name="generator" content="searxng' not in low:
        # 连 searxng 标识都没有，多半不是结果页
        return True
    # 结果页应至少含一个 article/结果条目；纯首页不含
    if 'class="result' not in low and '<article' not in low:
        # 但仍可能是 JSON 误判——这里仅对 HTML 生效
        return True
    return False


def _searx_parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except Exception as _exc:
        logger.warning("_searx_parse_json: 解析失败: %s", _exc)
        return []
    out: list[dict] = []
    for r in data.get("results", []):
        title = _strip_html(r.get("title", ""))
        url = r.get("url", "")
        snippet = _strip_html(r.get("content", "") or "")
        if url:
            out.append({"title": title, "url": url, "snippet": snippet})
    return out


def _searx_parse_html(html: str) -> list[dict]:
    """解析 SearXNG HTML 结果（容错多种模板变体）。"""
    out: list[dict] = []
    seen: set[str] = set()
    # 每条结果在 <article class="result"> 内
    blocks = re.split(r'<article[^>]*class="[^"]*result', html)
    for b in blocks[1:]:
        # 标题 + 链接：SearXNG 主链接通常为 <h3><a href=...> 或 <a class="url_header" href=...>
        am = re.search(
            r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', b, re.DOTALL
        )
        if not am:
            continue
        url = am.group(1)
        title = _strip_html(am.group(2))
        if not url or url in seen:
            continue
        snippet = ""
        sm = re.search(r'<p[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</p>', b, re.DOTALL)
        if sm:
            snippet = _strip_html(sm.group(1))
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": snippet})
    return out


def searxng_search(query: str, num_results: int = 5, timeout: int = 10) -> list[dict]:
    """通过 searx.space 动态发现的 SearXNG 实例搜索（第三兜底后端）。

    设计：从 searx.space 发现健康实例 → 本地缓存 24h → 轮换选用 →
    失败(429/JS挑战)自动冷却+换下一个。作为 bing/DDG 都不可用时的最后兜底，
    避免烧公共实例配额（单次搜索最多试 3 个实例）。
    """
    if not _SEARXNG_ENABLED:
        return []
    results: list[dict] = []
    for _ in range(_SEARXNG_TRY_PER_QUERY):
        inst = _searx_pick_instance()
        if not inst:
            break
        try:
            # 优先尝试 JSON 格式（干净、易解析）
            # 出站经 ssrf_safe_get：先校验实例 URL 公网可达并钉死 IP，杜绝 DNS 重绑定
            resp = ssrf_safe_get(
                inst + "/search",
                params={"q": query, "format": "json"},
                headers=_HEADERS, timeout=timeout, allow_redirects=True,
            )
        except Exception as e:
            logger.debug("searx 实例 %s 请求异常: %s", inst, e)
            _searx_mark_bad(inst)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            _searx_mark_bad(inst)
            continue
        text = resp.text
        if _searx_is_challenge(text):
            _searx_mark_bad(inst)
            continue
        # JSON 成功？
        if "json" in resp.headers.get("Content-Type", "") or text.strip().startswith("{"):
            results = _searx_parse_json(text)
        else:
            # 返回的是首页/非结果页 → 实例对匿名请求不可用，冷却换下一个
            if _searx_is_index_page(text):
                logger.debug("searx 实例 %s 返回首页而非结果，冷却", inst)
                _searx_mark_bad(inst)
                continue
            results = _searx_parse_html(text)
        if results:
            logger.info("SearXNG 实例 %s 命中 '%s': %d 条", inst, query, len(results))
            return results[:num_results]
        # 无结果也尝试下一个实例（可能该实例索引不全）
    return results[:num_results] if results else []

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _is_blocked_host(url: str) -> bool:
    """拒绝访问保留/内网/回环地址，缓解 SSRF（如搜索结果 301 跳转到内网）。

    返回 True 表示应被拦截。DNS 解析失败时保守地判定为拦截。
    """
    from urllib.parse import urlparse

    import ipaddress
    import socket

    try:
        host = urlparse(url).hostname
        if not host:
            return True
        # 先按字面 IP 判断（如 http://169.254.169.254/）
        try:
            ip = ipaddress.ip_address(host)
            return _ip_is_blocked(ip)
        except ValueError:
            pass  # host 非字面 IP（域名），继续走域名解析分支
        # 域名：解析所有 A/AAAA 记录，任一落入保留段即拦截
        for resolved in socket.getaddrinfo(host, None):
            addr = resolved[4][0]
            try:
                if _ip_is_blocked(ipaddress.ip_address(str(addr).split("%")[0])):
                    return True
            except ValueError as _exc:
                logger.warning("_is_blocked_host: 解析保留段判断失败，保守拦截: %s", _exc)
                return True
        return False
    except Exception:
        # 解析异常（含 IDN/编码/超时）保守拦截，避免把不可信主机当公网放行
        return True


def _ip_is_blocked(ip) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _http_get(url: str, timeout: int, retries: int = 3, allow_redirects: bool = True):
    """带重试的 GET，缓解沙箱/代理偶发的 TLS 断连（SSL EOF）。

    出站统一经 ssrf_safe_get（src.utils.net）：先校验 URL 公网可达并钉死 IP，
    杜绝 SSRF DNS 重绑定；allow_redirects=False 用于抓取外部结果页，避免 301/302 跳内网。
    """
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return ssrf_safe_get(
                url, headers=_HEADERS, timeout=timeout, allow_redirects=allow_redirects
            )
        except ValueError:
            # SSRF 拦截/DNS 失败不应重试
            raise
        except Exception as e:  # 网络/TLS 瞬时错误，重试
            last_err = e
            if attempt < retries - 1:
                time.sleep(0.5 + attempt * 0.5)
    assert last_err is not None
    raise last_err


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#\d+;", "", text)
    return text.strip()


def _decode_ddg_url(href: str) -> str:
    """DuckDuckGo 结果链接是 //duckduckgo.com/l/?uddg=<encoded> 中转地址，
    解出真实 URL，避免把中转链接透传给 LLM。"""
    if not href:
        return href
    m = re.search(r"uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


def bing_search(query: str, num_results: int = 5, timeout: int = 10) -> list[dict]:
    """使用必应搜索，返回标题、链接、摘要。失败或解析为空时返回 []。"""
    url = f"https://cn.bing.com/search?q={urllib.parse.quote(query)}&mkt=zh-CN&setlang=zh-CN"
    try:
        resp = _http_get(url, timeout, retries=2)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("必应搜索请求失败（query=%s）: %s", query, e)
        return []

    html = resp.text
    raw_blocks = re.split(r'<li class="b_algo"[^>]*>', html)[1:]
    results: list[dict] = []
    seen_links = set()

    for block in raw_blocks:
        end_markers = [
            block.find('<li class='),
            block.find('</ol>'),
            block.find('<div id="b_content"'),
        ]
        end_idx = min((i for i in end_markers if i > 0), default=len(block))
        content = block[:end_idx]

        title_m = re.search(r"<h2[^>]*>(.*?)</h2>", content, re.DOTALL)
        if not title_m:
            continue
        title = _strip_html(title_m.group(1))

        link_m = re.search(
            r"<h2[^>]*>.*?<a[^>]*href=\"(https?://[^\"]+)\"",
            content, re.DOTALL,
        )
        link = link_m.group(1) if link_m else ""
        if not link or link in seen_links:
            continue

        snippet = ""
        cap_m = re.search(r'class="b_caption"[^>]*>(.*?)</div>', content, re.DOTALL)
        if cap_m:
            p_m = re.search(r"<p[^>]*>(.*?)</p>", cap_m.group(1), re.DOTALL)
            if p_m:
                snippet = _strip_html(p_m.group(1))

        if title or snippet:
            seen_links.add(link)
            results.append({"title": title, "url": link, "snippet": snippet})
            if len(results) >= num_results:
                break

    logger.info("必应搜索 '%s': %d 条结果", query, len(results))
    return results


def duckduckgo_search(query: str, num_results: int = 5, timeout: int = 10) -> list[dict]:
    """使用 DuckDuckGo HTML 接口作为备用搜索源，返回标题、链接、摘要。"""
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}&kl=cn-zh"
    try:
        resp = _http_get(url, timeout, retries=2)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("DuckDuckGo 搜索请求失败（query=%s）: %s", query, e)
        return []

    html = resp.text
    blocks = re.split(r'<div class="result[ "]', html)
    results: list[dict] = []
    seen_links = set()

    for b in blocks[1:]:
        am = re.search(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.DOTALL
        )
        if not am:
            continue
        link = _decode_ddg_url(am.group(1))
        title = _strip_html(am.group(2))
        if not link or link in seen_links:
            continue

        snippet = ""
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</(div|a)>', b, re.DOTALL)
        if sm:
            snippet = _strip_html(sm.group(1))

        seen_links.add(link)
        results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= num_results:
            break

    logger.info("DuckDuckGo 搜索 '%s': %d 条结果", query, len(results))
    return results


# 搜索后端顺序：必应为主，DuckDuckGo 备用，SearXNG（searx.space 动态发现）为兜底。
# SearXNG 放最后：公网实例限流(429)/JS 挑战严重，仅当前两者都失败时才触发，
# 避免烧公共实例配额（单次搜索最多试 3 个实例，见 searxng_search）。
_SEARCH_BACKEND_NAMES = [
    ("bing", "bing_search"),
    ("duckduckgo", "duckduckgo_search"),
    ("searxng", "searxng_search"),
]

# 回传给 LLM 的干净结果上限：原始可请求更多，但去噪后只保留最相关的前 N 条，
# 避免把 10 条噪音（FIFA/Instagram/俄语扩展等）一起灌进上下文淹没真实答案。
_MAX_CLEAN_RESULTS = 6

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 标题/摘要里允许出现的“正常”字符（中英文、数字、常见中英文标点）；
# 其余（如印地语、西里尔字母、二进制乱码的控制符）视为噪音信号。
_NORMAL_CHARS = set(
    "，。、：；！？“”‘’（）《》【】.,:;!?()[]{}<>/-—_+=*&%@#\\|~^ $"
    "0123456789"
)


def _is_garbled(text: str) -> bool:
    """判断 snippet 是否含乱码（控制字符/大量非中英文的奇异字符）。"""
    if not text:
        return False
    if _CONTROL_CHARS.search(text):
        return True
    weird = 0
    total = 0
    for ch in text:
        total += 1
        if ch.isspace() or ch.isalnum() or ch in _NORMAL_CHARS:
            continue
        # 中日韩统一表意文字（含中文）算正常
        if "\u4e00" <= ch <= "\u9fff":
            continue
        weird += 1
    if total == 0:
        return False
    return weird / total > 0.3


_DICT_NOISE_PATTERNS = [
    # 单字字典页特征
    "汉语文字", "怎么读", "的意思", "的解释", "汉典", "康熙字典",
    "部首", "笔顺", "汉字", "_百度百科", "汉语字典", "汉语国学",
]
_DICT_SNIPPET_NOISE = [
    "康熙字典", "集韵", "笔画", "部首", "笔顺", "韻", "切",
    "唐韻", "廣韻", "玉篇", "说文", "韻會", "正韻",
]


def _is_dictionary_noise(item: dict) -> bool:
    """检测单字字典/百科噪音条目（如"珞（汉语文字）_百度百科"）。

    这类条目与用户搜索的公司/事件等实体无关，Bing 对罕见中文字会前置返回。
    命中后应在去噪阶段剔除，避免淹没真正的搜索结果。
    """
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    # 标题命中字典特征词
    if any(kw in title for kw in _DICT_NOISE_PATTERNS):
        return True
    # snippet 大量字典术语 → 强信号
    dict_signal_count = sum(1 for kw in _DICT_SNIPPET_NOISE if kw in snippet)
    if dict_signal_count >= 3:
        return True
    # 标题是单字+百科（如"珞_百度百科"、单字后面紧跟括号解释）
    if re.match(r"^[^\w\s]{1,2}[_(（].*百科", title):
        return True
    return False


def _tokenize(query: str) -> list[str]:
    """英文/数字按词、中文按 2-gram 切分，用于相关度打分。"""
    toks: list[str] = []
    for m in re.findall(r"[a-zA-Z0-9]+", query):
        toks.append(m.lower())
    cn = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    if len(cn) == 1:
        toks.append(cn)
    else:
        for i in range(len(cn) - 1):
            toks.append(cn[i:i + 2])
    return [t for t in toks if t]


def _score_result(item: dict, tokens: list[str]) -> int:
    text = (item.get("title", "") + " " + (item.get("snippet", "") or "")).lower()
    return sum(1 for t in tokens if t and t in text)


def _clean_and_rank(results: list[dict], queries, top_n: int = _MAX_CLEAN_RESULTS) -> list[dict]:
    """去噪 + 按多 query 最高相关度重排，返回最相关的前 top_n 条。

    queries 接受单个查询字符串或查询列表（list[str]）。多 query 场景下，每条结果
    取与【任一 query】的最高分词命中分作综合分排序——早期版本此处仅接单 query 打分，
    多 query 在 execute 中内联重实现导致两份逻辑漂移；现统一到此单一实现，execute 直接调用。

    去噪：剔除乱码 / 字典噪音 / 标题摘要皆空的结果；去噪后为空则退回原始结果。
    """
    if isinstance(queries, str):
        queries = [queries]
    query_tokens_list = [_tokenize(q) for q in queries]

    clean: list[dict] = []
    for r in results:
        snip = r.get("snippet") or ""
        if _is_garbled(snip):
            continue
        if not (r.get("title") or snip):
            continue
        if _is_dictionary_noise(r):
            continue
        clean.append(r)
    # 去噪后为空（极端情况）→ 退回原始结果，保证不至于完全无料
    if not clean:
        clean = results

    def _score(r: dict) -> int:
        return max((_score_result(r, t) for t in query_tokens_list), default=0)

    clean.sort(key=_score, reverse=True)
    # 只要有命中 query 的结果，就只保留命中的（剔除 0 分噪音）；
    # 若全部 0 分（极端），保留全部以免完全无料。
    hit = [r for r in clean if _score(r) > 0]
    if hit:
        clean = hit
    return clean[:top_n]


_FETCH_PAGE_MAX_CHARS = 2000  # 单页抓取的最大字符数
_CONTENT_TAGS_RE = re.compile(
    r"<(article|main|div class=\"?content|div class=\"?article|div class=\"?post|div class=\"?body).*?>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)


def _fetch_page(url: str, timeout: int = 8) -> Optional[str]:
    """抓取指定网页的正文文本（短内容），用于富化搜索结果。

    先尝试从 <article>/<main> 等语义标签提取正文，
    若未命中则回退到 <body> 全量后截断。
    返回 None 表示抓取失败（网络错误/超时/非 HTML）。
    """
    try:
        # 外部搜索结果 URL 可能被投毒/跳转，先拦截私网/保留地址，且不跟随重定向
        if _is_blocked_host(url):
            logger.debug("_fetch_page 拒绝访问保留/内网地址: %s", url)
            return None
        resp = _http_get(url, timeout=timeout, retries=1, allow_redirects=False)
    except Exception as e:
        logger.debug("_fetch_page 网络错误 %s: %s", url, str(e)[:60])
        return None

    # 不跟随重定向：避免 3xx 跳转到内网地址造成 SSRF（外部结果页几乎都是 200）
    if resp.status_code >= 300:
        return None

    ct = resp.headers.get("Content-Type", "")
    if "text/html" not in ct:
        return None

    html = resp.text
    # 优先提取语义内容块
    for m in _CONTENT_TAGS_RE.finditer(html):
        body = _strip_html(m.group(2))
        body = re.sub(r"\n{3,}", "\n\n", body)
        body = body.strip()
        if len(body) > 120:
            return body[:_FETCH_PAGE_MAX_CHARS]

    # 回退：提取 <body> 全文后截断
    body_m = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL)
    if not body_m:
        return None
    text = _strip_html(body_m.group(1))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text[:_FETCH_PAGE_MAX_CHARS] if len(text) > 40 else None



class WebSearchTool(BaseTool):
    name = "web_search"
    display_name = "联网搜索"
    short_description = "通过搜索引擎查询实时互联网信息（必应为主、DuckDuckGo 备用、SearXNG 兜底），适用于新闻、行情、科普等场景"
    description = (
        "通过搜索引擎查询互联网上的实时信息，"
        "适用于天气、新闻、行情、公司动态、知识科普等需要最新信息的场景。"
        "当问题涉及实时数据、最新事件或不确定的信息时使用。"
        "主搜索源为必应，失败时回退到 DuckDuckGo，再失败回退到 SearXNG（从 searx.space "
        "动态发现的公共实例，自动轮换+冷却，作为最后兜底），提升可用性。"
    )
    # 场景关键词统一维护在 IntentRegistry 的 domain.web_search（单一真源），
    # 此处仅声明服务类别，路由时自动解析。
    intent_categories = ["domain.web_search"]
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "单个搜索关键词(与 queries 二选一)。例：'今天北京天气'、'Rokae上市情况'",
            },
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "【推荐】多角度查询列表(推荐 2-3 条),例如 ['周星驰 最新电影', 'Stephen Chow 2025 film', '功夫女足 定档']。多个 query 会跨后端跑,合并去重+重排一次性返回。涉及中文娱乐/时效/人物/公司动态类查询时强烈推荐。",
            },
            "num_results": {
                "type": "integer",
                "description": "每条 query 的返回结果数量,默认 5 条",
                "default": 5,
            },
        },
        "required": [],
    }

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def execute(self, args: dict) -> str | dict:
        # 解析查询列表：优先用 queries 数组；fallback 到单 query 字符串（向后兼容）
        raw_queries = args.get("queries")
        if isinstance(raw_queries, list) and raw_queries:
            queries = [str(q).strip() for q in raw_queries if str(q).strip()]
        else:
            queries = []
        single_q = str(args.get("query", "")).strip()
        if single_q:
            queries.append(single_q)
        # 去重保序
        seen_q: set[str] = set()
        dedup_queries: list[str] = []
        for q in queries:
            if q and q not in seen_q:
                seen_q.add(q)
                dedup_queries.append(q)
        queries = dedup_queries
        if not queries:
            return {"error": "query or queries is required"}

        # 限上限（防止 LLM 滥用发 10 个 query 击穿服务）
        if len(queries) > 4:
            queries = queries[:4]
        primary_query = queries[0]

        num = _safe_int(args.get("num_results", 5), 5)
        num = max(1, min(num, 10))

        # 多 query 逐个跨后端跑，合并去重 + 重排
        # 为什么：必应对中文娱乐/时效/人物类查询质量飘忽（"周星驰 最新上映"会返回"周朝字源"）
        # 同一主题多角度拼（中文+英文+不同关键词）可同时覆盖必应与 DDG 的优势区间。
        all_results: list[dict] = []
        per_source_count: dict[str, int] = {}
        last_err = None
        tried: list[str] = []
        target = num * len(queries)
        for q in queries:
            for name, fn_name in _SEARCH_BACKEND_NAMES:
                fn = globals().get(fn_name)
                if fn is None:
                    continue
                try:
                    results = fn(q, num_results=num, timeout=self.timeout)
                except Exception as e:
                    last_err = str(e)
                    logger.warning("搜索后端 %s 执行异常: %s", name, e)
                    results = []
                tried.append(name)
                per_source_count[name] = per_source_count.get(name, 0) + len(results)
                for r in results:
                    r["_source"] = name
                    r["_from_query"] = q
                    all_results.append(r)
                # 早退：已收集到足够结果即停止，避免无谓烧 SearXNG 公共实例配额
                # （设计上 SearXNG 仅当前述后端都失败时兜底，见上方注释）
                if len(all_results) >= target:
                    break
            if len(all_results) >= target:
                break

        if not all_results:
            return {
                "queries": queries,
                "query": primary_query,
                "results": [],
                "error": "所有搜索源均不可用" + (f"（{last_err}）" if last_err else ""),
                "note": "联网搜索暂时不可用，请稍后重试或换种问法",
                "tried": tried,
            }

        # 合并去重（按 url 主机名+路径首段，忽略末尾查询参数）
        dedup: list[dict] = []
        seen_keys: set[str] = set()
        for r in all_results:
            url = (r.get("url") or "").lower()
            try:
                # 主机名+path，去掉 query 和 trailing slash
                from urllib.parse import urlparse
                p = urlparse(url if "://" in url else f"http://{url}")
                key = f"{p.netloc}{p.path}".rstrip("/")
            except Exception as _exc:
                logger.debug("execute: url 去重键解析失败: %s", _exc)
                key = url
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            dedup.append(r)

        # 去噪+重排（统一到 _clean_and_rank：多 query 取每条结果与任一 query 的最高分）
        ranked = _clean_and_rank(dedup, queries)

        # 剥离内部调试字段（_source/_from_query），它们对 LLM 无决策价值且浪费 token
        for r in ranked:
            r.pop("_source", None)
            r.pop("_from_query", None)

        # 对 top 2 条结果抓取正文富化（网络出错不阻塞返回）
        for i in range(min(2, len(ranked))):
            content = _fetch_page(ranked[i]["url"], timeout=6)
            if content:
                ranked[i]["content"] = content

        # 计算来源说明：哪个后端提供了结果
        sources_with_hits = [n for n, c in per_source_count.items() if c > 0]
        primary = sources_with_hits[0] if sources_with_hits else "none"

        logger.info(
            "web_search 统计: raw=%d dedup=%d clean=%d source=%s",
            len(all_results), len(dedup), len(ranked), primary,
        )
        return {
            "queries": queries,
            "query": primary_query,
            "results": ranked,
            "source": primary,
            "merged_sources": sources_with_hits,
        }
