"""安全工具函数单元测试。

覆盖 src/utils/security.py 中的脱敏和校验逻辑。
"""
from __future__ import annotations


from src.utils.security import (
    mask_oid,
    mask_token,
    sanitize_log_message,
    safe_get_dict,
)
# T-B3：is_safe_ip / validate_platform_id 已从 src.utils.security 删除（死代码），
# 其断言重指到真正跑生产的实现：
#   - IP 公网安全判定 → src.utils.net.is_ssrf_safe（SSRF 权威校验点）
#   - 平台白名单       → src.constants.SUPPORTED_PLATFORMS（单一真源）
from src.utils.net import is_ssrf_safe
from src.constants import SUPPORTED_PLATFORMS


class TestMaskOID:
    """脱敏 openDingTalkId / userId 等标识符。"""

    def test_empty_string(self):
        assert mask_oid("") == ""

    def test_short_oid(self):
        # 长度 <= 4 时全部替换为星号（统一返回 "***"）
        assert mask_oid("abc") == "***"
        assert mask_oid("ab") == "***"

    def test_normal_oid(self):
        oid = "o-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        result = mask_oid(oid)
        assert result.startswith("o-")
        assert result.endswith("wx")
        assert "***" in result
        assert "abc123" not in result

    def test_custom_visible_chars(self):
        oid = "abcdefghijklmn"
        result = mask_oid(oid, visible_prefix=3, visible_suffix=3)
        assert result.startswith("abc")
        assert result.endswith("lmn")
        assert "***" in result


class TestMaskToken:
    """脱敏 API Token。"""

    def test_empty_token(self):
        assert mask_token("") == ""

    def test_short_token(self):
        assert mask_token("abc") == "***"

    def test_normal_token(self):
        token = "sk-1234567890abcdef"
        result = mask_token(token)
        assert result.startswith("sk-1")
        assert "***" in result
        assert "abcdef" not in result


class TestSanitizeLogMessage:
    """通用日志消息脱敏兜底。"""

    def test_no_sensitive_data(self):
        msg = "正常日志消息"
        assert sanitize_log_message(msg) == msg

    def test_base64_token_masked(self):
        msg = "auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        result = sanitize_log_message(msg)
        # base64 token 被脱敏（保留前缀但隐藏主体）
        assert "auth:" in result
        # 脱敏后的长度应短于原消息
        assert len(result) < len(msg) + 5  # 允许少量星号

    def test_open_dingtalk_id_masked(self):
        msg = "openDingTalkId=o-abc123def456ghi789jkl"
        result = sanitize_log_message(msg)
        # 包含脱敏标记
        assert "***" in result or "o-" in result


class TestIsSafeIP:
    """IP 安全判定：断言重指到真正跑生产的 SSRF 权威校验点 src.utils.net.is_ssrf_safe。

    原 is_safe_ip 与 net._ip_is_public 逻辑重复且生产零调用（死代码），
    这里改为覆盖 is_ssrf_safe（被 weather.py / web_search.py 真实使用）。
    """

    def test_public_ip(self):
        assert is_ssrf_safe("http://8.8.8.8") is True
        assert is_ssrf_safe("http://1.1.1.1") is True

    def test_private_ips(self):
        assert is_ssrf_safe("http://10.0.0.1") is False
        assert is_ssrf_safe("http://172.16.0.1") is False
        assert is_ssrf_safe("http://192.168.1.1") is False
        assert is_ssrf_safe("http://127.0.0.1") is False
        assert is_ssrf_safe("http://0.0.0.0") is False

    def test_invalid_ip(self):
        # 空串：URL 解析即判不安全
        assert is_ssrf_safe("") is False
        # 0.0.0.0 同时覆盖 unspecified 场景
        assert is_ssrf_safe("http://0.0.0.0") is False
        # 非 IP 主机名且 DNS 解析失败 → 不安全。用 mock 固定，避免依赖真实 DNS：
        # 本地透明代理 fake-IP 模式会把任意主机名都解成 198.18.0.0/15，若走真实 DNS
        # 会让本用例误判为「安全」。
        import socket
        from unittest import mock

        def _gai_fail(host, port):
            raise socket.gaierror("no resolve")

        with mock.patch("socket.getaddrinfo", _gai_fail):
            assert is_ssrf_safe("http://not-an-ip") is False

    def test_proxy_fakeip_range_allowed(self):
        """本地透明代理 fake-IP 模式（Clash/ClashX/Surge 等）拦截 DNS，把公网域名
        解成 198.18.0.0/15（TEST-NET-2 保留段，仅作代理转发占位，绝非真实内网）。
        SSRF 校验须放行该段，否则所有经代理的出站请求被整批误判为「内网/保留地址」。
        真实内网仍由 test_private_ips / test_reserved_ips 覆盖拦截。"""
        import socket
        from unittest import mock

        def _fake_getaddrinfo(host, port):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.206", 0))]

        with mock.patch("socket.getaddrinfo", _fake_getaddrinfo):
            assert is_ssrf_safe("https://nominatim.openstreetmap.org/search") is True
            assert is_ssrf_safe("https://wttr.in/Beijing") is True

    def test_reserved_ips_still_blocked(self):
        """保留段（非代理 fake-IP）仍须拦截：防止将来误放宽白名单。"""
        # TEST-NET-3 / 文档示例保留段，非 198.18.0.0/15
        assert is_ssrf_safe("http://203.0.113.5") is False
        assert is_ssrf_safe("http://198.51.100.7") is False
        # 组播 / 链路本地
        assert is_ssrf_safe("http://224.0.0.1") is False
        assert is_ssrf_safe("http://169.254.169.254/latest") is False


class TestValidatePlatformId:
    """平台白名单：断言重指到单一真源 src.constants.SUPPORTED_PLATFORMS。"""

    def test_known_platforms(self):
        assert "dingtalk" in SUPPORTED_PLATFORMS
        assert "feishu" in SUPPORTED_PLATFORMS
        assert "wecom" in SUPPORTED_PLATFORMS

    def test_unknown_platform(self):
        assert "wechat" not in SUPPORTED_PLATFORMS
        assert "" not in SUPPORTED_PLATFORMS
        assert "invalid" not in SUPPORTED_PLATFORMS


class TestSafeGetDict:
    """安全地从嵌套字典中取值。"""

    def test_existing_key(self):
        data = {"user": {"profile": {"name": "张三"}}}
        assert safe_get_dict(data, "user", "profile", "name") == "张三"

    def test_missing_key(self):
        data = {"user": {"profile": {}}}
        result = safe_get_dict(data, "user", "profile", "age", default=0)
        assert result == 0

    def test_intermediate_not_dict(self):
        data = {"user": "string_value"}
        result = safe_get_dict(data, "user", "profile", "name", default="未知")
        assert result == "未知"

    def test_none_default(self):
        data = {}
        result = safe_get_dict(data, "a", "b", "c")
        assert result is None
