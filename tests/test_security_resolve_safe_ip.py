"""resolve_safe_ip 回归测试：解析到公网 IP，拒绝私网/保留/不可解析。"""
import socket
from unittest.mock import patch

from web.security import resolve_safe_ip


def test_resolve_safe_ip_public():
    with patch("socket.getaddrinfo",
               return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]):
        assert resolve_safe_ip("example.com") == "93.184.216.34"


def test_resolve_safe_ip_loopback_rejected():
    with patch("socket.getaddrinfo",
               return_value=[(socket.AF_INET, 0, 0, "", ("127.0.0.1", 0))]):
        assert resolve_safe_ip("localhost") is None


def test_resolve_safe_ip_private_rejected():
    with patch("socket.getaddrinfo",
               return_value=[(socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))]):
        assert resolve_safe_ip("internal.corp") is None


def test_resolve_safe_ip_link_local_rejected():
    with patch("socket.getaddrinfo",
               return_value=[(socket.AF_INET, 0, 0, "", ("169.254.169.254", 0))]):
        assert resolve_safe_ip("metadata") is None


def test_resolve_safe_ip_unresolvable():
    with patch("socket.getaddrinfo", side_effect=socket.gaierror):
        assert resolve_safe_ip("nope.invalid") is None


def test_resolve_safe_ip_mixed_picks_public():
    # 首个为私网，第二个为公网 → 应返回公网
    with patch("socket.getaddrinfo", return_value=[
        (socket.AF_INET, 0, 0, "", ("192.168.1.1", 0)),
        (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
    ]):
        assert resolve_safe_ip("multi.example.com") == "93.184.216.34"
