"""SSRF 防护边界回归测试（src/utils/net.py 单一真源）。

用 mock 把 ``socket.getaddrinfo`` 钉死，避免依赖真实 DNS，确定性验证：
- 私网 / 回环 / 链路本地（含云元数据 169.254.169.254）/ 保留段 一律拒绝
- 代理 fake-IP（198.18.0.0/15）放行
- 公网放行
- ssrf_safe_get 解析一次并钉死 IP，杜绝 DNS 重绑定 TOCTOU
- Playwright 启动参数对域名做 host-resolver-rules 钉死
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

from src.utils import net


def _gai(*ips: str):
    """构造 socket.getaddrinfo 的返回值（每个 IP 一个 (family, type, proto, canon, (ip, port))）。"""
    return [(None, None, None, None, (ip, 0)) for ip in ips]


class TestIpIsPublic:
    def test_private_rejected(self):
        assert not net._ip_is_public("10.0.0.1")
        assert not net._ip_is_public("192.168.1.1")
        assert not net._ip_is_public("172.16.5.5")

    def test_loopback_rejected(self):
        assert not net._ip_is_public("127.0.0.1")
        assert not net._ip_is_public("::1")

    def test_link_local_metadata_rejected(self):
        # 云元数据端点是 SSRF 最危险的目标
        assert not net._ip_is_public("169.254.169.254")

    def test_reserved_rejected(self):
        assert not net._ip_is_public("100.64.0.1")  # CGNAT (100.64.0.0/10)
        assert not net._ip_is_public("0.0.0.0")
        assert not net._ip_is_public("203.0.113.10")  # TEST-NET-3 文档段，保留不可达

    def test_public_allowed(self):
        assert net._ip_is_public("8.8.8.8")
        assert net._ip_is_public("1.1.1.1")

    def test_proxy_fakeip_allowed(self):
        # Clash/Surge 等 fake-IP 模式把公网域名解析成 198.18.0.0/15，应放行
        assert net._ip_is_public("198.18.0.1")
        assert net._ip_is_public("198.19.255.254")

    def test_invalid_ip_string_rejected(self):
        assert not net._ip_is_public("not-an-ip")


class TestIsSsrfSafe:
    @patch("src.utils.net.socket.getaddrinfo")
    def test_public_url_allowed(self, mock_gai):
        mock_gai.return_value = _gai("1.1.1.1")
        assert net.is_ssrf_safe("https://example.com/api")

    @patch("src.utils.net.socket.getaddrinfo")
    def test_internal_url_rejected(self, mock_gai):
        mock_gai.return_value = _gai("10.0.0.5")
        assert not net.is_ssrf_safe("https://intranet.corp.local/x")

    @patch("src.utils.net.socket.getaddrinfo")
    def test_metadata_url_rejected(self, mock_gai):
        mock_gai.return_value = _gai("169.254.169.254")
        assert not net.is_ssrf_safe("http://169.254.169.254/latest/meta-data/")

    @patch("src.utils.net.socket.getaddrinfo")
    def test_non_http_scheme_rejected(self, mock_gai):
        mock_gai.return_value = _gai("1.1.1.1")
        assert not net.is_ssrf_safe("ftp://example.com/file")

    def test_missing_hostname_rejected(self):
        assert not net.is_ssrf_safe("https:///no-host")

    @patch("src.utils.net.socket.getaddrinfo")
    def test_any_private_ip_in_multi_resolve_rejected(self, mock_gai):
        # 多 A 记录中只要有一个私网即整体拒绝（防部分解析绕过）
        mock_gai.return_value = _gai("1.1.1.1", "10.0.0.9")
        assert not net.is_ssrf_safe("https://multi.example.com/")


class TestResolveSafeIp:
    @patch("src.utils.net.socket.getaddrinfo")
    def test_returns_first_public(self, mock_gai):
        mock_gai.return_value = _gai("10.0.0.1", "8.8.8.8")
        assert net.resolve_safe_ip("host.example") == "8.8.8.8"

    @patch("src.utils.net.socket.getaddrinfo")
    def test_all_private_returns_none(self, mock_gai):
        mock_gai.return_value = _gai("10.0.0.1", "192.168.0.1")
        assert net.resolve_safe_ip("host.example") is None

    @patch("src.utils.net.socket.getaddrinfo")
    def test_dns_failure_returns_none(self, mock_gai):
        mock_gai.side_effect = socket.gaierror("dns fail")
        assert net.resolve_safe_ip("host.example") is None


class TestSsrfSafeGet:
    @patch("src.utils.net.socket.getaddrinfo")
    @patch("requests.Session")
    def test_pins_ip_and_forces_safe_kwargs(self, mock_session_cls, mock_gai):
        mock_gai.return_value = _gai("1.1.1.1")
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        resp = net.ssrf_safe_get("https://example.com/page")

        # 公网 IP：应成功发起请求
        assert resp is mock_resp
        # 默认强制 allow_redirects=False（防重定向跳内网）/ verify=True（TLS 不降）
        _, kwargs = mock_session.get.call_args
        assert kwargs.get("allow_redirects") is False
        assert kwargs.get("verify") is True

    @patch("src.utils.net.socket.getaddrinfo")
    @patch("requests.Session")
    def test_private_ip_rejected_before_request(self, mock_session_cls, mock_gai):
        mock_gai.return_value = _gai("10.0.0.7")
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        import pytest

        with pytest.raises(ValueError):
            net.ssrf_safe_get("https://intranet.corp/x")
        # 钉死为私网时应完全不发起请求
        mock_session.get.assert_not_called()


class TestPlaywrightLaunchArgs:
    @patch("src.utils.net.resolve_safe_ip", return_value="1.1.1.1")
    def test_domain_gets_host_resolver_rule(self, _mock):
        args = net.build_playwright_launch_args("https://example.com")
        assert any(a.startswith("--host-resolver-rules=MAP example.com 1.1.1.1") for a in args)

    @patch("src.utils.net.resolve_safe_ip", return_value=None)
    def test_unresolved_domain_no_map(self, _mock):
        args = net.build_playwright_launch_args("https://unresolved.invalid")
        assert not any(a.startswith("--host-resolver-rules") for a in args)

    def test_ip_literal_no_map(self):
        args = net.build_playwright_launch_args("https://192.0.2.5")
        assert not any(a.startswith("--host-resolver-rules") for a in args)
