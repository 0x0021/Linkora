"""SSRF 出站守卫门禁 + 引擎 host 白名单的回归测试（P1-1）。

覆盖两件事：
1. ``scripts/check_ssrf_guard.py`` 的判定逻辑（裸出站识别 / 多行注释块豁免）。
2. ``web-composite-search`` 的引擎 host 白名单（fail-closed）：内置引擎 URL 放行，
   内网 / 云元数据 / 仿冒 host 一律拒绝。

两个被测模块都不在常规 import 路径上（``scripts/`` 非包；``src/skills/web-composite-search/``
目录名含连字符），故按文件路径用 importlib 加载，避免依赖 pytest 的 sys.path 行为。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ssrf_guard = _load(ROOT / "scripts" / "check_ssrf_guard.py", "check_ssrf_guard_under_test")
_search = _load(
    ROOT / "src" / "skills" / "web-composite-search" / "scripts" / "search.py",
    "composite_search_under_test",
)


# ── 门禁判定逻辑 ────────────────────────────────────────────────────────────

def test_detects_bare_outbound_calls():
    tree = ast.parse(
        "import requests\n"
        "import httpx\n"
        "requests.get(u)\n"
        "httpx.post(u)\n"
        "httpx.Client(trust_env=False)\n"
        "urllib.request.urlopen(u)\n"
    )
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert all(_ssrf_guard._is_bare_outbound(c) for c in calls), "裸出站应全部被识别"


def test_guarded_calls_are_not_flagged():
    tree = ast.parse("ssrf_safe_get(u)\nsafe_http_request('GET', u)\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not any(_ssrf_guard._is_bare_outbound(c) for c in calls), "受 guard 保护的调用不应命中"


def test_exemption_recognised_in_multiline_comment_block():
    lines = [
        "# 一些前置说明",
        "# ssrf-ok: 可信端点",
        "# 第二行豁免说明",
        "response = requests.get(u)",
    ]
    assert _ssrf_guard._has_exemption(lines, 4) is True
    # 标记不在连续注释块内 → 不算豁免
    assert _ssrf_guard._has_exemption(["x = 1", "requests.get(u)"], 2) is False


def test_repo_has_no_unexempted_outbound():
    """仓库当前所有裸出站点都必须带显式豁免（新增裸出站会直接让本用例失败）。"""
    violations = [h for h in _ssrf_guard.scan(_ssrf_guard._iter_py_files()) if not h["exempt"]]
    assert violations == [], f"存在未豁免的裸出站: {violations}"


# ── 引擎 host 白名单（fail-closed） ─────────────────────────────────────────

def test_builtin_engine_urls_are_allowed():
    for engine_id, engine in _search.CompositeSearch.ENGINES.items():
        tpl = engine["search_url"]
        # 去掉模板参数，只保留 host 形态
        url = tpl.split("{")[0]
        assert _search.CompositeSearch._engine_host_allowed(url) is True, engine_id


def test_private_and_metadata_hosts_are_blocked():
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/admin",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
    ):
        assert _search.CompositeSearch._engine_host_allowed(url) is False, url


def test_lookalike_host_is_blocked():
    """仿冒 host（如 www.baidu.com.evil.com）不得因后缀相似而放行。"""
    assert _search.CompositeSearch._engine_host_allowed(
        "https://www.baidu.com.evil.com/s?wd=x"
    ) is False


def test_empty_and_malformed_urls_are_blocked():
    for url in ("", "not-a-url", "https://"):
        assert _search.CompositeSearch._engine_host_allowed(url) is False, url
