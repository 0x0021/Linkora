"""Web 层安全工具。

SSRF 防护已下沉至 ``src.utils.net``（src 层单一真源），Web 侧统一从那里导入；
本模块仅做兼容再导出，避免破坏既有 ``from web.security import ...`` 调用。
"""

from __future__ import annotations

from src.utils.net import (
    build_playwright_launch_args,
    is_ssrf_safe,
    resolve_safe_ip,
    ssrf_safe_get,
)

__all__ = [
    "is_ssrf_safe",
    "ssrf_safe_get",
    "resolve_safe_ip",
    "build_playwright_launch_args",
]
