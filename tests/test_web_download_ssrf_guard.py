"""web.dependencies._download_to_file 的 SSRF 边界纵深校验回归测试。

验证：即便调用点已做主机白名单+SHA256 钉值，下载边界仍统一过 is_ssrf_safe，
非法 URL（内网/保留/非 http(s)）在真正发请求前即被 fail-closed 拒绝；
合法 URL 正常下载落盘。防未来重构把可变 URL 传入时绕过调用点网关。
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


def _make_urlopen_mock(payload: bytes = b"#!/bin/sh\necho hi\n"):
    """构造一个 urlopen 上下文管理器桩：read 先吐 payload 再吐空结束循环。"""
    resp = MagicMock()
    resp.read.side_effect = [payload, b""]
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    # 让 urlopen(...) 调用返回自身（即上下文管理器 cm），
    # 这样 `with urlopen(...) as resp` 才会走 cm.__enter__ → resp。
    cm.return_value = cm
    return cm


def test_download_rejects_unsafe_url_before_fetch():
    """is_ssrf_safe 返回 False 时必须拒绝且不发起任何网络请求。"""
    from web.dependencies import _download_to_file
    import web.dependencies as dep
    from pathlib import Path

    urlopen_cm = _make_urlopen_mock()
    with patch.object(dep, "is_ssrf_safe", return_value=False), \
         patch("urllib.request.urlopen", urlopen_cm):
        with pytest.raises(ValueError):
            _download_to_file("http://10.0.0.1/evil.sh", Path("/tmp/_dl_nonexist"))
    # 边界拒绝后绝不应发起下载请求
    urlopen_cm.__enter__.assert_not_called()


def test_download_proceeds_when_safe():
    """is_ssrf_safe 返回 True 时正常下载并落盘。"""
    from web.dependencies import _download_to_file
    import web.dependencies as dep
    from pathlib import Path
    import tempfile

    payload = b"#!/bin/sh\necho ok\n"
    urlopen_cm = _make_urlopen_mock(payload)
    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "install.sh"
        with patch.object(dep, "is_ssrf_safe", return_value=True), \
             patch("urllib.request.urlopen", urlopen_cm):
            _download_to_file("https://skillhub.example.com/install.sh", dest)
        assert dest.read_bytes() == payload
        # 确认确实发起了下载请求
        urlopen_cm.__enter__.assert_called()
