"""web/api.py CSP 安全头回归测试。

背景（2026-09 前端安全整改 E/F 项）：
- 全站内联事件（onclick/onchange/oninput/onkeydown/ondrag*）已迁移为
  data-action / data-keydown / data-drag* 事件委托（web/static/js/core/app.js）；
- 主页 "/" 的 CSP 收紧为 script-src 'self' 'nonce-XXX'（去掉 'unsafe-inline'），
  仅放行带一次性 nonce 的主题预取内联脚本（防 FOUC，见 index.html 头部）；
- 其余路径（含 FastAPI /docs 的 Swagger UI 内联脚本）维持放宽 CSP。

锁住的行为：
1. "/" 严格档：script-src 无 unsafe-inline、含一次性 nonce，且 nonce 与
   HTML 内联脚本的 nonce 属性一致、每次请求轮换；
2. 其他路径维持放宽档（避免破坏 /docs Swagger UI）；
3. 模板与前端 JS 不再回潮内联事件属性（防回归扫描）。
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from web.api import app

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "web"


def _script_src(csp: str) -> str:
    """从 CSP 头取 script-src 指令片段。"""
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith("script-src"):
            return part
    return ""


class TestStrictCspOnIndex:
    def test_index_csp_uses_nonce_without_unsafe_inline(self):
        """/ 应返回严格 CSP：script-src 有 nonce、无 unsafe-inline。"""
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        script_src = _script_src(csp)
        assert "'nonce-" in script_src, f"主页 CSP 缺少 nonce: {csp}"
        assert "'unsafe-inline'" not in script_src, (
            f"主页 script-src 仍含 unsafe-inline: {csp}"
        )

    def test_index_inline_theme_script_carries_matching_nonce(self):
        """HTML 里的内联主题预取脚本必须带与响应头一致的 nonce。"""
        client = TestClient(app)
        resp = client.get("/")
        csp = resp.headers["Content-Security-Policy"]
        m = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        assert m, f"CSP 头无 nonce: {csp}"
        nonce = m.group(1)
        # 模板渲染后内联脚本应携带同一 nonce
        assert f'<script nonce="{nonce}"' in resp.text, (
            "内联主题脚本未携带与 CSP 头一致的 nonce，严格档下会被浏览器拦截（白屏）"
        )

    def test_index_nonce_rotates_per_request(self):
        """nonce 每请求轮换（主页 no-cache，不存在缓存钉死旧 nonce 的问题）。"""
        client = TestClient(app)
        n1 = re.search(
            r"'nonce-([A-Za-z0-9_-]+)'", client.get("/").headers["Content-Security-Policy"]
        ).group(1)
        n2 = re.search(
            r"'nonce-([A-Za-z0-9_-]+)'", client.get("/").headers["Content-Security-Policy"]
        ).group(1)
        assert n1 != n2, "两次请求的 nonce 相同，未轮换"


class TestLaxCspElsewhere:
    def test_non_index_paths_keep_unsafe_inline(self):
        """非主页路径（如 /health）维持放宽 CSP，保护 /docs Swagger UI 内联脚本。"""
        client = TestClient(app)
        resp = client.get("/health")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "'unsafe-inline'" in csp, f"非主页 CSP 未保持放宽档: {csp}"


class TestNoInlineEventHandlersRegression:
    """防回归：模板与前端 JS 不得再出现内联事件属性。

    任何新内联 on*=" 都会让 CSP 严格档（主页）静默拦截对应交互，
    因此在服务端测试层锁死。
    """

    # 覆盖常见事件；[\s"'] 前置避免误配 data-page-node-id / condition= 之类标识符
    _PATTERN = re.compile(
        r"""[\s"']on(?:click|change|input|error|keydown|keyup|keypress|submit|focus|blur|load|unload|dblclick|dragover|dragleave|drop|dragstart|dragend|mouseover|mouseout|mouseenter|mouseleave|wheel|scroll|resize|contextmenu)\s*="""
    )

    def _iter_files(self):
        templates = (_FRONTEND_DIR / "templates").glob("*.html")
        js_files = [
            p for p in (_FRONTEND_DIR / "static" / "js").rglob("*.js")
            if "vendor" not in p.parts
        ]
        return list(templates) + js_files

    def test_no_inline_event_handlers(self):
        offenders: list[str] = []
        for path in self._iter_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), start=1):
                # 跳过纯注释行（app.js 的说明注释里引用了旧写法作为文档）
                stripped = line.strip()
                if stripped.startswith(("//", "*", "/*", "{#", "#}")):
                    continue
                if self._PATTERN.search(line):
                    offenders.append(f"{path.relative_to(_FRONTEND_DIR.parent)}:{i}: {stripped[:120]}")
        assert not offenders, (
            "发现回潮的内联事件属性（会破坏主页严格 CSP），应改用 "
            "data-action / data-keydown / data-drag* 事件委托：\n" + "\n".join(offenders)
        )
