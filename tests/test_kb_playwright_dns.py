"""Playwright 分支 DNS 钉死（防重绑定 TOCTOU）回归测试。"""
from unittest.mock import patch

from src.utils.net import build_playwright_launch_args


def test_build_launch_args_pins_hostname():
    with patch("src.utils.net.resolve_safe_ip", return_value="93.184.216.34"):
        args = build_playwright_launch_args("https://example.com/page")
    assert "--no-sandbox" in args
    assert "--host-resolver-rules=MAP example.com 93.184.216.34" in args


def test_build_launch_args_skips_ip_literal():
    with patch("src.utils.net.resolve_safe_ip") as m:
        args = build_playwright_launch_args("https://93.184.216.34/page")
    joined = " ".join(args)
    assert "--host-resolver-rules" not in joined
    m.assert_not_called()  # 已是 IP 字面量，无需解析/映射


def test_build_launch_args_no_pin_when_unresolvable():
    with patch("src.utils.net.resolve_safe_ip", return_value=None):
        args = build_playwright_launch_args("https://bad.example.com/x")
    joined = " ".join(args)
    assert "--host-resolver-rules" not in joined
