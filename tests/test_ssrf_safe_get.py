"""SSRF DNS 重绑定 TOCTOU 修复回归测试。

验证 web.security.ssrf_safe_get：
- 解析一次并固定 IP，Host/SNI 仍为原域名（消除 TOCTOU）
- 指向内网/保留地址（如云元数据 169.254.169.254）被拒
- 仅允许 http/https
"""
import socket
from unittest.mock import patch, MagicMock

import pytest


def test_ssrf_safe_get_pins_ip_and_keeps_host():
    from web.security import ssrf_safe_get

    fake = MagicMock()
    fake.status_code = 200
    with patch("socket.getaddrinfo",
               return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]), \
         patch("requests.Session") as MS:
        inst = MS.return_value
        inst.get.return_value = fake
        r = ssrf_safe_get("http://example.com/path")
        assert r is fake
        # 应为 http/https 各 mount 一个固定 IP 适配器
        assert inst.mount.call_count == 2
        inst.get.assert_called_once()
        # URL 原样传递，Host 头仍是原域名（由适配器连接类注入固定 IP）
        assert inst.get.call_args[0][0] == "http://example.com/path"


def test_ssrf_safe_get_rejects_private_metadata_ip():
    from web.security import ssrf_safe_get

    with patch("socket.getaddrinfo",
               return_value=[(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]):
        with pytest.raises(ValueError):
            ssrf_safe_get("http://evil.metadata.example.com/x")


def test_ssrf_safe_get_rejects_invalid_scheme():
    from web.security import ssrf_safe_get

    with pytest.raises(ValueError):
        ssrf_safe_get("ftp://example.com/x")
