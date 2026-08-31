"""图片访问令牌 Cookie 的 Secure 标记回归测试（2026-08-31 P3 安全增强）。

`issue_image_token` 此前缺 `secure` 标记；2026-08-31 改为按请求 scheme 推断：
仅 HTTPS 才置 Secure，避免本地 HTTP（localhost 开发）下图片 Cookie 被浏览器
丢弃导致整屏图片加载失败。
"""
from __future__ import annotations

import asyncio

from fastapi import Response


def _call_issue(scheme: str) -> str:
    from web.routers import image as img

    class _Req:
        url = type("U", (), {"scheme": scheme})()

    resp = Response()
    asyncio.run(img.issue_image_token(resp, _Req()))
    return resp.headers.get("set-cookie", "")


def test_image_token_secure_over_https():
    sc = _call_issue("https")
    assert "img_token" in sc
    assert "Secure" in sc


def test_image_token_not_secure_over_http():
    """本地 HTTP（localhost 开发）不加 Secure，否则图片 Cookie 被丢弃致图片失效。"""
    sc = _call_issue("http")
    assert "img_token" in sc
    assert "Secure" not in sc
