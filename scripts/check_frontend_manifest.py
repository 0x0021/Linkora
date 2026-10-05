#!/usr/bin/env python3
"""前端资源清单一致性门禁（P0-9 根治）。

背景：前端有三份必须手工同步的清单——

  1. ``scripts/build_frontend.mjs`` 的 ``JS_ORDER`` / ``CSS_ORDER``（生产 bundle 顺序）
  2. ``web/templates/index.html`` 的开发态 ``<script src=...>`` 标签
  3. ``web/api.py::_auto_page_versions``（自动扫目录，无需手工维护，故不参与校验）

历史上 ``js/pages/models.js`` 只在第 1 份里，导致：生产 bundle 正常，
**开发模式**下 ``core/app.js`` 调 ``loadModelsPage()`` 抛 ReferenceError，
且该行不在 try 内 → 中断 ``switchPage`` 后续全部页面初始化 → 整页白屏。
这比「漏加清单导致 404」更隐蔽：线上无任何报错，只有开发态炸。

新增前端文件若忘记登记清单，本脚本非零退出，使问题在 CI 即暴露，
而不是等到某次开发态调试时才发现。

用法::

    python scripts/check_frontend_manifest.py [--quiet]

退出码：0 = 三方一致；1 = 存在漂移（打印差异明细）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_STATIC = REPO_ROOT / "web" / "static"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_frontend.mjs"
INDEX_HTML = REPO_ROOT / "web" / "templates" / "index.html"

# 不参与清单校验的 JS：
#   - tests/ 下的测试文件由 vitest 单独加载，不进浏览器页面
#   - drafts.js 是 type=module，刻意不参与合并（见 build_frontend.mjs 注释）
EXCLUDED_JS_NAMES = {"drafts.js"}
EXCLUDED_JS_DIR_PARTS = {"tests"}


def _parse_order_list(source: str, const_name: str) -> list[str]:
    """从 build_frontend.mjs 中解析 ``const <NAME> = [ 'a', 'b' ];``。"""
    match = re.search(
        rf"const\s+{const_name}\s*=\s*\[(.*?)\];", source, re.DOTALL
    )
    if not match:
        return []
    return re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))


def _disk_files(subdir: str, suffix: str) -> set[str]:
    """扫描 web/static/<subdir> 下的实际文件，返回相对 web/static 的路径集合。"""
    base = WEB_STATIC / subdir
    if not base.is_dir():
        return set()
    out: set[str] = set()
    for path in base.rglob(f"*{suffix}"):
        if EXCLUDED_JS_DIR_PARTS & set(path.parts):
            continue
        if suffix == ".js" and path.name in EXCLUDED_JS_NAMES:
            continue
        out.add(str(path.relative_to(WEB_STATIC)))
    return out


def _index_html_script_paths() -> set[str]:
    """解析 index.html 开发态引用的 /static/... 资源路径。

    同时匹配双引号与单引号两种写法；只取 src 的第一个引号段，
    丢弃 ``?v={{ xxx_js_v }}`` 版本查询串。
    """
    if not INDEX_HTML.is_file():
        return set()
    html = INDEX_HTML.read_text(encoding="utf-8")
    return {
        m.group(1)
        for m in re.finditer(r"""src=["']/static/([^"'?]+)""", html)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="前端资源清单一致性门禁")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args()

    if not BUILD_SCRIPT.is_file():
        print(f"[FAIL] 找不到构建脚本: {BUILD_SCRIPT}")
        return 1

    build_src = BUILD_SCRIPT.read_text(encoding="utf-8")
    js_order = set(_parse_order_list(build_src, "JS_ORDER"))
    css_order = set(_parse_order_list(build_src, "CSS_ORDER"))
    if not js_order:
        print("[FAIL] 未能从 build_frontend.mjs 解析出 JS_ORDER")
        return 1

    disk_js = _disk_files("js", ".js")
    disk_css = _disk_files("css", ".css")
    html_refs = _index_html_script_paths()

    problems: list[str] = []

    # 1) 磁盘上有、JS_ORDER 无 → 生产 bundle 缺文件（加载 404 / 静默不一致）
    for path in sorted(disk_js - js_order):
        problems.append(f"磁盘存在但未登记进 build_frontend.mjs 的 JS_ORDER: {path}")
    # 2) JS_ORDER 有、磁盘无 → 构建会因缺文件报错
    for path in sorted(js_order - disk_js):
        problems.append(f"JS_ORDER 登记了但磁盘上不存在该文件: {path}")
    # 3) CSS 同理
    for path in sorted(disk_css - css_order):
        problems.append(f"磁盘存在但未登记进 build_frontend.mjs 的 CSS_ORDER: {path}")
    for path in sorted(css_order - disk_css):
        problems.append(f"CSS_ORDER 登记了但磁盘上不存在该文件: {path}")

    # 4) 开发态 index.html 漏引用 JS（生产走 bundle 正常、开发态 ReferenceError 白屏）
    #    校验全部业务脚本目录：pages / core / components / services + 顶层
    #    theme.js / icons.js（后两者同样被 core/app.js 直接调用全局函数）。
    html_business = {p for p in html_refs if p.startswith("js/")}
    for path in sorted(disk_js - html_business):
        problems.append(
            f"index.html 开发态未引用该脚本（开发模式会 ReferenceError）: {path}"
        )

    if problems:
        print("[FAIL] 前端资源清单存在漂移：")
        for item in problems:
            print(f"  - {item}")
        print(
            "\n修复方式："
            "\n  1) 新增文件后登记进 scripts/build_frontend.mjs 的 JS_ORDER/CSS_ORDER"
            "\n  2) 同步在 web/templates/index.html 的开发态脚本区加 <script src=...>"
            "\n  3) 页面用到的全局函数，确认 core/app.js 的调用点在 try 内或已定义"
        )
        return 1

    if not args.quiet:
        print(
            f"[OK] 前端资源清单一致："
            f"JS {len(js_order)} 个 / CSS {len(css_order)} 个 / "
            f"index.html 引用 {len(html_business)} 个业务脚本"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
