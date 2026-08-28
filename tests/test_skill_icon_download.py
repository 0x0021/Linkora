"""技能图标下载回归测试（2026-08-28）。

事故：`_download_skill_icon` 里 ``dest`` 是 ``os.path.realpath()`` 返回的 **str**，
却当 Path 用了 ``dest.is_file()`` / ``dest.write_bytes()``，于是每次调用都抛
``AttributeError: 'str' object has no attribute 'is_file'``——
且该行位于 try 块**之前**，无人捕获，技能图标下载功能整体失效。

（CI 只跑 `pyright src/`，web/ 下这两处类型错误从未被门禁拦下，故补测试兜底。）
"""
import asyncio

import web.api  # noqa: F401 — 必须先导入父模块，规避 image.py 的循环导入
from web.routers import image as img


class _FakeClient:
    """不下真实网络请求；get 直接抛错，用于覆盖「下载失败」分支。"""

    def __init__(self, *args, **kwargs):  # noqa: ARG002
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):  # noqa: ARG002
        raise RuntimeError("no network in test")


def _patch(tmp_path, monkeypatch):
    icons = tmp_path / "icons"
    icons.mkdir()
    monkeypatch.setattr(img, "get_skill_icons_dir", lambda: icons)
    monkeypatch.setattr(img, "is_ssrf_safe", lambda _u: True)
    monkeypatch.setattr(img.httpx, "AsyncClient", _FakeClient)
    return icons


def test_download_skill_icon_returns_true_when_cached(tmp_path, monkeypatch):
    """已缓存：命中 is_file() 直接幂等返回 True，不再崩。"""
    icons = _patch(tmp_path, monkeypatch)
    (icons / "existing.png").write_bytes(b"\x89PNG")

    ok = asyncio.run(img._download_skill_icon("existing", "https://example.com/a.png"))
    assert ok is True


def test_download_skill_icon_no_attribute_error_when_not_cached(tmp_path, monkeypatch):
    """回归核心：未缓存时不得因在 str 上调 .is_file() 抛 AttributeError。

    下载失败应被 try/except 吞掉并返回 False，而不是把异常抛给调用方。
    """
    _patch(tmp_path, monkeypatch)

    ok = asyncio.run(img._download_skill_icon("brand-new", "https://example.com/b.png"))
    assert ok is False


def test_download_skill_icon_rejects_non_http_scheme(tmp_path, monkeypatch):
    """非 http(s) 直接拒绝，不落盘也不发请求。"""
    _patch(tmp_path, monkeypatch)

    assert asyncio.run(img._download_skill_icon("x", "ftp://example.com/x.png")) is False
