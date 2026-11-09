"""出站回复密钥脱敏：确定性凭据形态替换，正常文本零改写。

样例密钥全部运行时拼接，避免源码里出现可被密钥扫描器命中的静态串。
"""
from __future__ import annotations

import pytest

from src.guard.output import redact_secrets


def _fake_key() -> str:
    return "sk-" + "A" * 24


def _fake_jwt() -> str:
    return "eyJ" + "A" * 10 + "." + "B" * 10 + "." + "C" * 10


def _fake_private_key() -> str:
    begin = "-----BEGIN " + "PRIVATE KEY-----"
    end = "-----END " + "PRIVATE KEY-----"
    return begin + "\nMIIEvQIBADANBgkqhkiG9w0BAQEF\n" + end


class TestRedact:
    @pytest.mark.parametrize(
        "build",
        [
            _fake_key,
            _fake_jwt,
            _fake_private_key,
            lambda: "Bearer " + "B" * 30,
            lambda: "AKID" + "C" * 20,
            lambda: "api_key=" + "D" * 24,
        ],
    )
    def test_secret_is_redacted(self, build) -> None:
        secret = build()
        out, count = redact_secrets(f"这是工具返回的原文：{secret} 结束")
        assert count >= 1
        assert secret not in out
        assert "这是工具返回的原文" in out
        assert "结束" in out

    @pytest.mark.parametrize(
        "text",
        [
            "打印机在3楼，走 VPN 才能连",
            "API 文档见 https://example.com/docs",
            "账号 admin，密码请找 IT 同事重置",
            "本月的 token 用量统计已出，详见报表",
            "Bearer 令牌的长度配置在 llm 段",
            "普通短参数 key=abc123 不需要脱敏",
        ],
    )
    def test_normal_text_unchanged(self, text: str) -> None:
        out, count = redact_secrets(text)
        assert count == 0
        assert out == text

    def test_empty(self) -> None:
        assert redact_secrets("") == ("", 0)

    def test_multiple_secrets_all_redacted(self) -> None:
        text = f"{_fake_key()} 和 {_fake_jwt()}"
        out, count = redact_secrets(text)
        assert count == 2
        assert _fake_key() not in out
        assert _fake_jwt() not in out
