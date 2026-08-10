"""前端性能优化 HIGH 项回归测试（2026-08-08）：F-H1 缓存判定 + F-H4 图片 token/Cache 头。

覆盖：
- F-H1：VersionedStaticFiles 对 dist/ 内容哈希 bundle 设为 immutable；
        未版本化资源（vendor/fontawesome）改 no-cache 并保留 ETag/Last-Modified（允许 304）。
- F-H4：/api/image-token 下发 HttpOnly Cookie(img_token)；
        serve_image 优先读 Cookie、兼容 ?it= 回退；成功响应带 private 缓存头；缺 token 返回 401。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.responses import Response

import web.api as _api
from web.api import VersionedStaticFiles

# 静态目录用仓库绝对路径，避免依赖当前工作目录（全量套件中其他测试的 cwd 状态可能变化，
# 相对路径 "web/static" 会解析失败导致 404）。生产挂载同样使用 get_static_dir() 绝对路径。
_STATIC_DIR = str(Path(__file__).resolve().parent.parent / "web" / "static")


# ============================================================
# F-H1 · 静态资源缓存判定
# ============================================================
def _call_get_response(vs: VersionedStaticFiles, path: str, query: bytes = b"") -> object:
    scope = {"type": "http", "method": "GET", "headers": [], "query_string": query}
    return asyncio.run(vs.get_response(path, scope))


def test_fh1_dist_hashed_bundle_is_immutable():
    vs = VersionedStaticFiles(directory=_STATIC_DIR)
    # dist bundle 哈希随内容变化，从 manifest.json 读取实际文件名（避免硬编码旧哈希 404）
    manifest = json.loads((Path(_STATIC_DIR) / "dist" / "manifest.json").read_text(encoding="utf-8"))
    css_bundle = manifest["css"]
    resp = _call_get_response(vs, f"dist/{css_bundle}")
    cc = resp.headers.get("cache-control", "")
    assert "immutable" in cc, f"dist bundle 应为 immutable，实际: {cc}"
    assert "max-age=86400" in cc


def test_fh1_vendor_unversioned_keeps_etag_no_maxage_zero():
    vs = VersionedStaticFiles(directory=_STATIC_DIR)
    resp = _call_get_response(vs, "vendor/chart.umd.min.js")
    cc = resp.headers.get("cache-control", "")
    assert cc == "no-cache", f"未版本化资源应为 no-cache，实际: {cc}"
    assert "max-age=0" not in cc, "不得再误设 max-age=0（否则 304 失效）"
    # 关键：保留 ETag/Last-Modified，允许 304 协商
    assert resp.headers.get("etag"), "未版本化资源必须保留 ETag 以支持 304"
    assert resp.headers.get("last-modified"), "未版本化资源必须保留 Last-Modified 以支持 304"


def test_fh1_query_v_still_immutable():
    vs = VersionedStaticFiles(directory=_STATIC_DIR)
    resp = _call_get_response(vs, "css/theme.css", query=b"v=12345")
    assert "immutable" in resp.headers.get("cache-control", "")


# ============================================================
# F-H4 · 图片 token 迁移到 Cookie + 缓存头
# ============================================================
def test_fh4_issue_token_sets_httponly_cookie():
    import web.routers.image as img

    resp = Response()
    result = asyncio.run(img.issue_image_token(response=resp))
    assert "token" in result and result["token"]
    sc = resp.headers.get("set-cookie", "")
    assert "img_token=" in sc, "必须下发 img_token Cookie"
    assert "HttpOnly" in sc, "Cookie 必须 HttpOnly"
    assert "SameSite=Lax" in sc or "samesite=lax" in sc.lower()


def _fake_cfg_with(tmp_img_dir: Path):
    cfg = MagicMock()
    cfg.poller.image_temp_dir = str(tmp_img_dir)
    return cfg


def test_fh4_serve_image_cookie_auth_and_cache_header(tmp_path):
    import web.routers.image as img

    img_dir = tmp_path / "tmp_images"
    img_dir.mkdir()
    (img_dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n fake-png-bytes")

    with patch.object(_api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        req = MagicMock()
        req.cookies = {"img_token": img._make_image_token()}
        resp = asyncio.run(img.serve_image("a.png", it=None, request=req))
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "private, max-age=300"


def test_fh4_serve_image_it_param_fallback(tmp_path):
    import web.routers.image as img

    img_dir = tmp_path / "tmp_images"
    img_dir.mkdir()
    (img_dir / "a.png").write_bytes(b"x")

    with patch.object(_api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        req = MagicMock()
        req.cookies = {}  # 无 Cookie
        resp = asyncio.run(img.serve_image("a.png", it=img._make_image_token(), request=req))
        assert resp.status_code == 200


def test_fh4_serve_image_missing_token_401(tmp_path):
    import web.routers.image as img
    from fastapi import HTTPException

    img_dir = tmp_path / "tmp_images"
    img_dir.mkdir()
    (img_dir / "a.png").write_bytes(b"x")

    with patch.object(_api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        req = MagicMock()
        req.cookies = {}
        with pytest.raises(HTTPException) as exc:
            asyncio.run(img.serve_image("a.png", it=None, request=req))
        assert exc.value.status_code == 401
