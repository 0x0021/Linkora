"""F-H3 图片缩略图 + WebP 测试。

覆盖：Pillow 缩略图生成（缩放/格式/不放大）、serve_image 集成（w+fmt→webp 变小、
内容协商、无 w/fmt→原图、路径穿越仍 403、缩略图落盘缓存）、purge_orphan_images
连带清理 .thumbs 变体。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from PIL import Image


def _fake_cfg_with(img_dir: Path):
    class _Poller:
        def __init__(self, d):
            self.image_temp_dir = str(d)
    class _Cfg:
        def __init__(self, d):
            self.poller = _Poller(d)
    return _Cfg(img_dir)


def _make_png(path: Path, w: int = 1000, h: int = 800, color=(200, 30, 30, 255)) -> int:
    img = Image.new("RGBA", (w, h), color)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG")
    return path.stat().st_size


def _req(accept="image/webp"):
    import web.routers.image as img
    req = MagicMock()
    req.cookies = {"img_token": img._make_image_token()}
    req.headers = {"accept": accept}
    return req


def test_make_thumb_resizes_and_webp(tmp_path):
    import web.routers.image as img
    orig = tmp_path / "big.png"
    _make_png(orig, 1000, 800)
    thumb = tmp_path / "big.png__w320.webp"
    path, media = img._make_thumb(orig, thumb, 320, "webp", True)
    assert media == "image/webp"
    assert Path(path).exists()
    with Image.open(path) as im:
        assert im.size[0] == 320
        assert im.size[1] == 256  # 1000:800 → 320:256
        assert im.format == "WEBP"
    # 缩略图应明显小于原图
    assert Path(path).stat().st_size < orig.stat().st_size


def test_make_thumb_no_upscale_returns_original(tmp_path):
    import web.routers.image as img
    orig = tmp_path / "small.png"
    _make_png(orig, 200, 160)
    thumb = tmp_path / "small.png__w320.webp"
    # 格式未变(无 webp 协商)且原图已比目标窄 → 直接服务原图，不生成缩略图
    path, media = img._make_thumb(orig, thumb, 320, None, False)
    assert path == str(orig)
    assert not thumb.exists()


def test_make_thumb_jpeg_flattens_alpha(tmp_path):
    import web.routers.image as img
    orig = tmp_path / "a.png"
    _make_png(orig, 800, 600)  # 带 alpha
    thumb = tmp_path / "a.png__w400.jpeg"
    path, media = img._make_thumb(orig, thumb, 400, "jpeg", False)
    assert media == "image/jpeg"
    with Image.open(path) as im:
        assert im.mode == "RGB"
        assert im.format == "JPEG"


def test_serve_image_thumb_webp_via_fmt(tmp_path):
    import web.routers.image as img
    img_dir = tmp_path / "tmp_images"
    orig = img_dir / "chat" / "ocr.png"
    _make_png(orig, 1200, 900)
    with patch.object(img._api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        resp = asyncio.run(img.serve_image("chat/ocr.png", w=320, fmt="webp", request=_req()))
    assert resp.status_code == 200
    assert resp.media_type == "image/webp"
    # 缩略图已落盘且明显小于原图
    thumb = img_dir / ".thumbs" / "chat" / "ocr.png__w320.webp"
    assert thumb.exists()
    assert thumb.stat().st_size < orig.stat().st_size


def test_serve_image_content_negotiation_png_when_no_webp(tmp_path):
    import web.routers.image as img
    img_dir = tmp_path / "tmp_images"
    orig = img_dir / "ocr.png"
    _make_png(orig, 1200, 900)
    with patch.object(img._api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        # Accept 不含 image/webp 且无 fmt → 仍缩略但输出原格式 png
        resp = asyncio.run(img.serve_image("ocr.png", w=320, fmt=None, request=_req(accept="image/png")))
    assert resp.status_code == 200
    assert resp.media_type == "image/png"


def test_serve_image_original_without_params(tmp_path):
    import web.routers.image as img
    img_dir = tmp_path / "tmp_images"
    orig = img_dir / "ocr.png"
    _make_png(orig, 1200, 900)
    with patch.object(img._api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        resp = asyncio.run(img.serve_image("ocr.png", w=None, fmt=None, request=_req()))
    assert resp.status_code == 200
    # 无 w/fmt → 直接原图直出，不生成缩略图
    assert not (img_dir / ".thumbs").exists()


def test_serve_image_path_traversal_blocked(tmp_path):
    import web.routers.image as img
    from fastapi import HTTPException
    img_dir = tmp_path / "tmp_images"
    img_dir.mkdir()
    with patch.object(img._api, "_get_cfg", return_value=_fake_cfg_with(img_dir)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(img.serve_image("../../etc/hosts", w=320, fmt="webp", request=_req()))
    assert exc.value.status_code == 403


def test_purge_orphan_images_clears_thumbs(tmp_path):
    from src.memory.image_cleanup import purge_orphan_images

    img_dir = tmp_path / "tmp_images"
    orig = img_dir / "chat" / "ocr.png"
    _make_png(orig, 1200, 900)
    # 制造缩略图变体
    thumbs = img_dir / ".thumbs" / "chat"
    thumbs.mkdir(parents=True)
    (thumbs / "ocr.png__w320.webp").write_bytes(b"webp-bytes")
    (thumbs / "ocr.png__w640.webp").write_bytes(b"webp-bytes2")
    db_path = str(img_dir.parent / "db.sqlite")  # base = parent/tmp_images == img_dir
    removed = purge_orphan_images(db_path, ["chat/ocr.png"])
    # 原图 + 2 个缩略图均被删
    assert not orig.exists()
    assert not (thumbs / "ocr.png__w320.webp").exists()
    assert not (thumbs / "ocr.png__w640.webp").exists()
    assert removed >= 3
