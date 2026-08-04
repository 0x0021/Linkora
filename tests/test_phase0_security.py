"""Phase 0 安全加固（T1-T5）回归测试。

覆盖：
① T1 空密码 + auth_enabled=True 时 WebConfig 构造抛 ValueError（fail-closed）；
   空密码 + auth_enabled=False 不抛。
② T2 is_ssrf_safe 拒 unspecified/reserved/multicast（含 0.0.0.0），公网仍 True。
③ T3 限流账号维度（IP|username）独立计数，且激活 _AUTH_BLOCK_SECONDS
   （窗口刚过但 block_until 未过期仍封锁，避免立即再爆破）。
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.config import AppConfig, WebConfig
from web import api as web_api
from web.security import is_ssrf_safe


# ---------------------------------------------------------------------------
# T1 — 空密码启动强校验（fail-closed）
# ---------------------------------------------------------------------------
def test_webconfig_empty_password_with_auth_enabled_raises():
    # 空字符串与纯空白均视为空密码，应拒绝
    with pytest.raises(ValueError):
        WebConfig(auth_enabled=True, auth_password="")
    with pytest.raises(ValueError):
        WebConfig(auth_enabled=True, auth_password="   \t\n")


def test_webconfig_empty_password_with_auth_disabled_ok():
    # 关闭全局鉴权时空密码允许（信任内网/反代场景）
    cfg = WebConfig(auth_enabled=False, auth_password="")
    assert cfg.auth_enabled is False
    # AppConfig 整体构造也不应受影响
    app = AppConfig(web={"auth_enabled": False, "auth_password": ""})
    assert app.web.auth_enabled is False


# ---------------------------------------------------------------------------
# T2 — is_ssrf_safe 扩大拒绝范围
# ---------------------------------------------------------------------------
def _mock_resolve(ip: str):
    # socket.getaddrinfo 返回 [(family, type, proto, canonname, sockaddr), ...]
    return [(None, None, None, None, (ip, 0))]


def test_is_ssrf_safe_rejects_unspecified_zero():
    # 0.0.0.0 / :: 未指定地址（原放行漏洞）
    with patch("src.utils.net.socket.getaddrinfo", return_value=_mock_resolve("0.0.0.0")):
        assert is_ssrf_safe("http://0.0.0.0") is False
    with patch("src.utils.net.socket.getaddrinfo", return_value=_mock_resolve("0.0.0.0")):
        assert is_ssrf_safe("http://0.0.0.0:8080") is False
    with patch("src.utils.net.socket.getaddrinfo", return_value=_mock_resolve("::")):
        assert is_ssrf_safe("http://[::]") is False


def test_is_ssrf_safe_rejects_private_linklocal_reserved_multicast():
    bad = {
        "192.168.1.1": "私网",
        "10.0.0.1": "私网 A",
        "127.0.0.1": "回环",
        "169.254.0.1": "链路本地",
        "240.0.0.1": "保留段",
        "224.0.0.1": "组播",
    }
    for ip, _desc in bad.items():
        with patch("src.utils.net.socket.getaddrinfo", return_value=_mock_resolve(ip)):
            assert is_ssrf_safe(f"http://{ip}") is False


def test_is_ssrf_safe_allows_public():
    # 正常公网域名（解析为公网 IP）仍通过
    with patch("src.utils.net.socket.getaddrinfo", return_value=_mock_resolve("93.184.216.34")):
        assert is_ssrf_safe("http://example.com") is True
    # 非 http(s) 协议仍拒绝
    with patch("src.utils.net.socket.getaddrinfo", return_value=_mock_resolve("93.184.216.34")):
        assert is_ssrf_safe("ftp://example.com") is False


# ---------------------------------------------------------------------------
# T3 — 限流账号维度 + 激活 _AUTH_BLOCK_SECONDS
# ---------------------------------------------------------------------------
def test_rate_limit_account_dimension_independent():
    web_api._AUTH_FAILS.clear()
    ip = "203.0.113.9"
    account_key = f"{ip}|admin"
    try:
        # IP 维度超阈值被封
        for _ in range(web_api._AUTH_MAX_FAILS):
            web_api._auth_record_fail(ip)
        assert web_api._auth_rate_allowed(ip) is False

        # 账号维度独立计数：另一用户名达到阈值也被封
        web_api._AUTH_FAILS.clear()
        for _ in range(web_api._AUTH_MAX_FAILS):
            web_api._auth_record_fail(account_key)
        assert web_api._auth_rate_allowed(account_key) is False
        # IP 维度未受账号维度拖累（独立）
        assert web_api._auth_rate_allowed(ip) is True
    finally:
        web_api._AUTH_FAILS.clear()


def test_block_until_persists_beyond_window():
    """激活 _AUTH_BLOCK_SECONDS：窗口刚过但 block_until 未过期仍封锁。"""
    web_api._AUTH_FAILS.clear()
    ip = "198.51.100.7"
    try:
        for _ in range(web_api._AUTH_MAX_FAILS):
            web_api._auth_record_fail(ip)
        entry = web_api._AUTH_FAILS[ip]
        assert entry["block_until"] > time.time()
        # 立刻再查仍封锁（block_until 生效）
        assert web_api._auth_rate_allowed(ip) is False

        # 模拟「窗口刚过」：把失败时间戳前移到窗口之外，
        # 但 block_until 尚未到期 —— 此时仍应封锁（防立即再爆破）
        entry["fails"] = [time.time() - (web_api._AUTH_FAIL_WINDOW + 10)] * web_api._AUTH_MAX_FAILS
        assert entry["block_until"] > time.time()
        assert web_api._auth_rate_allowed(ip) is False
    finally:
        web_api._AUTH_FAILS.clear()
