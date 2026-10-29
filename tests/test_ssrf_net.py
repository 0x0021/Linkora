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


class TestPinningSupportGuard:
    """SSRF「IP 钉死」失效时必须 fail-closed，不得静默降级。

    IP 钉死是 SSRF 防护的最后一环（消除 DNS 重绑定 TOCTOU）。若 urllib3 改了
    ``PoolManager.pool_classes_by_scheme`` 的存在性/类型，我们改不动 → 连接会走
    普通 DNS 解析 → TOCTOU 窗口重新打开，而外部看不出任何异常。
    故守卫必须抛可读错误，并给出明确的修复版本。
    """

    def test_clean_env_passes(self):
        # 当前环境（urllib3 2.8.0）支持替换池类 → 不应抛异常
        from urllib3.poolmanager import PoolManager

        net._ensure_pinning_supported(PoolManager(num_pools=1))

    def test_unsupported_poolmanager_raises_readable_error(self):
        import pytest

        class _FakeManager:
            """模拟 urllib3 改版后不再暴露 pool_classes_by_scheme。"""

            pool_classes_by_scheme = None

        with pytest.raises(RuntimeError) as exc:
            net._ensure_pinning_supported(_FakeManager())
        msg = str(exc.value)
        assert "urllib3" in msg
        assert "urllib3==2.8.0" in msg  # 修复命令必须给出明确版本
        assert "SSRF" in msg  # 必须说明这是 SSRF 防护失效


class TestIssue46Urllib3Compat:
    """issue #46 回归：出站建连不得因 urllib3 内部签名差异而崩溃。

    根因（2026-10-29）：``_PinnedAdapter.init_poolmanager`` 往
    ``PoolManager(**kwargs)`` 注入了 urllib3 不认识的 ``pool_classes_by_scheme``，
    该参数残留在 ``connection_pool_kw`` → 被拷进 ``request_context`` →
    ``_default_key_normalizer`` 给每个 key 加 ``key_`` 前缀 →
    ``PoolKey.__new__() got an unexpected keyword argument 'key_pool_classes_by_scheme'``
    → 所有走 SSRF 钉 IP 的工具（天气等）整批崩溃，用户侧表现为「什么工具都没法用」。
    """

    def test_pin_session_pool_kwargs_are_clean(self):
        """建连用的 connection_pool_kw 不得含 urllib3 不认识的参数。"""
        from unittest.mock import patch

        with patch("src.utils.net.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = _gai("1.1.1.1")
            session, _ = net._pin_session("https://example.com/x")

        manager = session.get_adapter("https://example.com").poolmanager
        assert "pool_classes_by_scheme" not in manager.connection_pool_kw

    def test_pin_session_builds_pool_without_typeerror(self):
        """完整走一遍 urllib3 的建池路径，不得抛 TypeError。"""
        from unittest.mock import patch

        with patch("src.utils.net.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = _gai("1.1.1.1")
            session, _ = net._pin_session("https://example.com/x")

        manager = session.get_adapter("https://example.com").poolmanager
        # 旧实现在此抛 TypeError: PoolKey.__new__() got an unexpected keyword
        # argument 'key_pool_classes_by_scheme'
        pool = manager.connection_from_host("example.com", 443, "https")
        assert pool is not None

    def test_pinned_pool_class_is_installed(self):
        """SSRF 的「IP 钉死」必须真正生效：连接池的 ConnectionCls 应被换成钉死版。

        旧实现覆盖的是 ``HTTPAdapter._new_pool``，而 requests ≥2.34 已删除该方法
        → 覆盖是死代码 → 钉死从未生效（DNS 重绑定 TOCTOU 窗口实际存在）。
        """
        from unittest.mock import patch

        with patch("src.utils.net.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = _gai("1.1.1.1")
            session, _ = net._pin_session("https://example.com/x")

        manager = session.get_adapter("https://example.com").poolmanager
        for scheme in ("http", "https"):
            pool_cls = manager.pool_classes_by_scheme[scheme]
            assert pool_cls.__name__.startswith("_Pinned"), (
                f"{scheme} 的连接池类未被替换，IP 钉死未生效"
            )
            assert pool_cls.ConnectionCls.__name__ == "_PinnedConnection"
