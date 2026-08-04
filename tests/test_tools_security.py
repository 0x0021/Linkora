"""工具安全加固回归测试（F1 SSRF / F3 任意文件上传）。

F1: web_search._fetch_page 抓取外部搜索结果 URL 时，需拦截私网/保留/回环地址
    且不跟随重定向，避免 SSRF（恶意结果页 301 跳转到内网/云元数据）。
F3: upload_image 仅允许项目 data/ 与 /tmp 路径，拒绝越界/符号链接逃逸，
    防止提示注入诱导上传 /etc/passwd、SSH 私钥等敏感文件。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.tools.media import ImageUploadTool, _ALLOWED_ROOTS
from src.tools.web_search import _fetch_page, _ip_is_blocked, _is_blocked_host

# 数据目录根取自工具真实的白名单（macOS /tmp 是符号链接，realpath 后需一致）
_DATA_ROOT = next((r for r in _ALLOWED_ROOTS if r.endswith("/data") or r.endswith("\\data")), None)


# ============================================================================
# F1 — SSRF 防护
# ============================================================================
class TestSSRFGuard:
    def test_ip_is_blocked_private_ranges(self):
        import ipaddress

        blocked = [
            "127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1",
            "169.254.169.254", "0.0.0.0", "::1", "fc00::1",
        ]
        for s in blocked:
            assert _ip_is_blocked(ipaddress.ip_address(s)) is True, s

    def test_ip_is_blocked_public(self):
        import ipaddress

        public = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"]
        for s in public:
            assert _ip_is_blocked(ipaddress.ip_address(s)) is False, s

    def test_blocked_host_literal_private(self):
        # 字面私网 IP 无需 DNS 即可判定
        assert _is_blocked_host("http://127.0.0.1/secret") is True
        assert _is_blocked_host("http://169.254.169.254/latest/meta-data/") is True
        assert _is_blocked_host("http://10.0.0.5/admin") is True
        assert _is_blocked_host("http://192.168.1.1/") is True

    def test_blocked_host_loopback_name(self):
        # localhost 解析到 127.0.0.1（在测试里固定解析结果，避免真实 DNS 抖动）
        with patch(
            "socket.getaddrinfo",
            return_value=[(None, None, None, None, ("127.0.0.1", 0))],
        ):
            assert _is_blocked_host("http://localhost/") is True

    def test_blocked_host_public_allowed(self):
        # 公网域名（固定解析为公有 IP）应放行
        with patch(
            "socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        ):
            assert _is_blocked_host("http://example.com/page") is False

    def test_fetch_page_redirect_to_internal_blocked(self):
        """_fetch_page 不跟随重定向：外部结果页返回 302 时直接返回 None（防 SSRF 跳转）。"""
        fake = MagicMock()
        fake.status_code = 302
        fake.headers = {"Content-Type": "text/html"}
        fake.text = ""
        with patch("src.tools.web_search._http_get", return_value=fake), \
             patch("src.tools.web_search._is_blocked_host", return_value=False):
            assert _fetch_page("http://public-result.example/page") is None

    def test_fetch_page_blocked_host_returns_none(self):
        """目标为保留/内网地址时，_fetch_page 直接返回 None，不发起请求。"""
        with patch("src.tools.web_search._http_get") as mock_get:
            assert _fetch_page("http://169.254.169.254/latest/meta-data/") is None
            mock_get.assert_not_called()


# ============================================================================
# F3 — upload_image 路径白名单
# ============================================================================
class TestImageUploadPathGuard:
    def _tool(self):
        return ImageUploadTool(dws=MagicMock(), config=None)

    def test_allowed_tmp(self):
        assert self._tool()._is_allowed_path("/tmp/chart.png") is True

    def test_allowed_data_dir(self):
        assert _DATA_ROOT is not None, "未能从 _ALLOWED_ROOTS 解析出 data 根"
        p = str(Path(_DATA_ROOT) / "tmp_images" / "x" / "ocr.png")
        assert self._tool()._is_allowed_path(p) is True

    def test_reject_etc_passwd(self):
        assert self._tool()._is_allowed_path("/etc/passwd") is False

    def test_reject_ssh_key(self):
        assert self._tool()._is_allowed_path("/Users/user/.ssh/id_rsa") is False

    def test_reject_traversal(self):
        # ../ 越界在 realpath 后被解析到 /etc，应拒绝
        assert self._tool()._is_allowed_path("/tmp/../etc/passwd") is False

    def test_execute_rejects_sensitive_path(self):
        tool = self._tool()
        r = tool.execute({"file_path": "/etc/passwd"})
        assert r.get("error"), r
        tool.dws.media_upload.assert_not_called()

    def test_execute_uploads_allowed_tmp_file(self):
        import os
        import tempfile

        # macOS 上 pytest tmp_path 落在 /private/var/folders/...，不属于 /tmp 白名单；
        # 显式写到 realpath(/tmp) 下以保证命中允许根。
        real_tmp = os.path.realpath("/tmp")
        fd, p = tempfile.mkstemp(dir=real_tmp, suffix=".png")
        os.write(fd, b"fake-png-bytes")
        os.close(fd)
        try:
            tool = self._tool()
            tool.dws.media_upload.return_value = "MEDIA_123"
            r = tool.execute({"file_path": p})
            assert r.get("success") is True
            assert r["media_id"] == "MEDIA_123"
            tool.dws.media_upload.assert_called_once()
        finally:
            os.unlink(p)
