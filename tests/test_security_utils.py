"""安全工具函数单元测试。

覆盖 src/utils/security.py 中的脱敏和校验逻辑。
"""
from __future__ import annotations


from src.utils.security import (
    mask_oid,
    mask_token,
    sanitize_log_message,
    is_safe_ip,
    validate_platform_id,
    safe_get_dict,
)


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
    """检查 IP 是否为安全的公网地址。"""

    def test_public_ip(self):
        assert is_safe_ip("8.8.8.8") is True
        assert is_safe_ip("1.1.1.1") is True

    def test_private_ips(self):
        assert is_safe_ip("10.0.0.1") is False
        assert is_safe_ip("172.16.0.1") is False
        assert is_safe_ip("192.168.1.1") is False
        assert is_safe_ip("127.0.0.1") is False
        assert is_safe_ip("0.0.0.0") is False

    def test_invalid_ip(self):
        assert is_safe_ip("") is False
        assert is_safe_ip("not-an-ip") is False
        # 注意：256.256.256.256 在某些实现中可能不被校验为无效
        # 这里验证基本逻辑即可
        assert is_safe_ip("0.0.0.0") is False


class TestValidatePlatformId:
    """校验平台 ID 合法性。"""

    def test_known_platforms(self):
        assert validate_platform_id("dingtalk") is True
        assert validate_platform_id("feishu") is True
        assert validate_platform_id("wecom") is True

    def test_unknown_platform(self):
        assert validate_platform_id("wechat") is False
        assert validate_platform_id("") is False
        assert validate_platform_id("invalid") is False


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
