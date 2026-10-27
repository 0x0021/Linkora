#!/usr/bin/env python3
"""SSRF 出站守卫门禁：禁止裸 requests/httpx 出站，或要求显式豁免（P1-1）。

背景
----
项目已有行业级 SSRF 防护 ``src/utils/net.py``（``ssrf_safe_get`` / ``safe_http_request``）：
解析一次并固定 IP，阻断 DNS 重绑定 TOCTOU。问题不在能力，而在**采用率**——裸
``requests.get`` / ``httpx.Client`` 散落在技能脚本里，防护形同可选，新代码会继续绕过。

本门禁把「能力」变成「约束」
--------------------------------
用 AST 扫描 src/ + web/ 的**真实出站调用点**（AST 天然排除 docstring / 注释里的示例），
三种判定：

1. **受保护**：走 ``ssrf_safe_get`` / ``safe_http_request``（本身不在命中范围）。
2. **显式豁免**：命中行或其上一行带 ``# ssrf-ok`` 注释并写明理由。
   豁免是**显式且可审计**的——不再是「没人记得这里有洞」。
3. **违规**：裸出站且无豁免 → 门禁失败（exit 1）。

豁免的正确用法
--------------
* 端点由**部署方配置**决定且部署方可信任（如 LLM provider base_url）→ 可豁免；
* 端点指向**内网可信服务**（如企业内网合同审查 / 钉盘 API）→ 可豁免。
  ⚠️ 必须先确认该 URL **不是**终端用户可影响的输入；若是，必须改走 guard。
* 用户可控 URL → **不可豁免**，必须走 ``ssrf_safe_get`` / ``safe_http_request``。

用法
----
    python scripts/check_ssrf_guard.py            # 闸门（存在未豁免裸出站则失败）
    python scripts/check_ssrf_guard.py report     # 打印全部命中点及其判定
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 扫描范围（生产代码；技能脚本在 src/skills/ 下，随 src 一并扫描）
SCAN_DIRS = ("src", "web")

# 守卫实现所在文件：其内部对 requests/httpx 的引用是「防护本体」，不算违规出站
SKIP_FILES = {"src/utils/net.py"}

# 豁免标记：命中行或其上一行出现该标记即视为显式豁免
EXEMPT_MARKER = "ssrf-ok"

# 受保护的出站封装（调用它们即视为已受 SSRF 约束）
GUARDED_CALLS = {"ssrf_safe_get", "safe_http_request"}

# 裸出站的方法名
_HTTP_METHODS = {
    "get", "post", "put", "patch", "delete", "head", "options", "request",
}


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            if any(part in ("__pycache__", ".venv", "node_modules") for part in p.parts):
                continue
            files.append(p)
    return files


def _is_bare_outbound(node: ast.Call) -> bool:
    """判定一个 Call 是否为「裸出站」（未经 SSRF guard 封装的 requests/httpx 调用）。"""
    func = node.func
    # urlopen(...)
    if isinstance(func, ast.Name) and func.id == "urlopen":
        return True
    if not isinstance(func, ast.Attribute):
        return False
    # requests.get / httpx.post / requests.request ...
    if isinstance(func.value, ast.Name) and func.value.id in ("requests", "httpx"):
        if func.attr in _HTTP_METHODS:
            return True
        # httpx.Client(...) / httpx.AsyncClient(...)
        if func.attr in ("Client", "AsyncClient"):
            return True
    # urllib.request.urlopen(...)
    if (
        func.attr == "urlopen"
        and isinstance(func.value, ast.Attribute)
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "urllib"
        and func.value.attr == "request"
    ):
        return True
    return False


def _has_exemption(lines: list[str], lineno: int) -> bool:
    """命中行本身或其**上方连续注释块**内是否带 ``# ssrf-ok`` 豁免标记。

    向上扫描连续以 ``#`` 开头的注释行——豁免说明常需两行（如 image.py 那种
    「已做 A + 已做 B + 等价于 guard」），只看紧邻上一行会漏判。遇到非注释行即停。
    """
    idx = lineno - 1
    if 0 <= idx < len(lines) and EXEMPT_MARKER in lines[idx]:
        return True
    idx -= 1
    while 0 <= idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped.startswith("#"):
            break
        if EXEMPT_MARKER in stripped:
            return True
        idx -= 1
    return False


def scan(files: list[Path]) -> list[dict]:
    """返回全部裸出站命中点及其判定。"""
    hits: list[dict] = []
    for p in files:
        rel = str(p.relative_to(ROOT))
        if rel in SKIP_FILES:
            continue
        try:
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_bare_outbound(node):
                continue
            exempt = _has_exemption(lines, node.lineno)
            hits.append(
                {
                    "file": rel,
                    "line": node.lineno,
                    "exempt": exempt,
                    "snippet": lines[node.lineno - 1].strip()[:100] if lines else "",
                }
            )
    return hits


def _cmd_report() -> int:
    hits = scan(_iter_py_files())
    violations = [h for h in hits if not h["exempt"]]
    exempted = [h for h in hits if h["exempt"]]
    print(f"裸出站命中点：{len(hits)}（豁免 {len(exempted)} / 违规 {len(violations)}）")
    print("\n### 已显式豁免（需定期复核是否仍可信）")
    for h in exempted:
        print(f"  {h['file']}:{h['line']}  {h['snippet']}")
    print("\n### 违规（裸出站且无 # ssrf-ok 豁免）")
    for h in violations:
        print(f"  {h['file']}:{h['line']}  {h['snippet']}")
    return 0


def _cmd_gate() -> int:
    hits = scan(_iter_py_files())
    violations = [h for h in hits if not h["exempt"]]
    print(f"bare outbound calls (requests/httpx/urlopen) : {len(hits)}")
    print(f"exempted via '# {EXEMPT_MARKER}'              : {len(hits) - len(violations)}")
    print(f"VIOLATIONS (no exemption)                   : {len(violations)}")
    if violations:
        print(
            f"FAIL: 存在 {len(violations)} 处未走 SSRF guard 的裸出站。"
            f"用户可控 URL 必须改走 ssrf_safe_get / safe_http_request；"
            f"确属可信端点请加 `# {EXEMPT_MARKER}: <理由>` 行内豁免注释。"
        )
        for h in violations:
            print(f"  {h['file']}:{h['line']}  {h['snippet']}")
        return 1
    print("PASS: 所有出站调用均受 SSRF guard 保护或已显式豁免。")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "gate"
    if cmd == "report":
        return _cmd_report()
    return _cmd_gate()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
