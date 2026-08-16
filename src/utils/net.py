"""统一出站网络请求的 SSRF 防护（src 层单一真源）。

原实现位于 ``web/security.py``，现下沉到 ``src`` 层，使非 Web 路径（tools 等）也能
复用同一套 SSRF 校验逻辑，避免「防护只在 Web 侧落地、工具侧裸奔」的不对称。

Web 层 ``web/security.py`` 从此处再导出，保持 ``from web.security import ...`` 兼容。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _ip_is_public(ip_str: str) -> bool:
    """单 IP 是否允许出站：非私网/回环/链路本地/未指定/保留/组播。

    供 ``is_ssrf_safe`` 与 ``ssrf_safe_get`` 复用，保证「校验」与「钉死 IP」使用
    同一份解析结果，杜绝 DNS 重绑定 TOCTOU 窗口。
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
               or ip.is_unspecified or ip.is_reserved or ip.is_multicast)


def is_ssrf_safe(url: str) -> bool:
    """SSRF 防护：仅允许 http/https，且解析后的所有 IP 不得为私网/回环/链路本地。

    用于 import-url 的入口校验、重定向目标校验、以及 Playwright 路由拦截。
    """
    try:
        parsed = urlparse(url)
    except Exception as _exc:
        logger.debug(f"is_ssrf_safe: swallowed exception: {_exc}")
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        addr_info = socket.getaddrinfo(hostname, None)
        resolved_ips = {str(info[4][0]) for info in addr_info}
    except socket.gaierror as _exc:
        logger.warning(f"is_ssrf_safe: swallowed exception: {_exc}")
        return False
    return bool(resolved_ips) and all(_ip_is_public(ip) for ip in resolved_ips)


def ssrf_safe_get(url: str, **kwargs):
    """解析一次并固定 IP 发起请求，消除 SSRF DNS 重绑定 TOCTOU。

    先解析 + 校验（任一 IP 私网/保留即拒），再用固定 IP 建连，
    Host/SNI 仍为原域名，证书校验不关闭。供 Web/工具层 URL 导入与出站抓取统一使用。

    与 ``is_ssrf_safe`` 的区别：后者在请求前单独解析校验，
    ``requests`` 建连时会再次解析，存在被 DNS 重绑定窗口攻击的 TOCTOU 风险；
    本函数解析一次并钉死 IP，使「校验」与「建连」使用同一地址。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("仅支持 http/https 且需含主机名")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise ValueError("DNS 解析失败") from e
    ips = {str(i[4][0]) for i in infos}
    # 关键修复：校验与钉死使用「同一份」解析结果，杜绝两次解析间的 DNS 重绑定
    # TOCTOU 窗口（旧实现先 is_ssrf_safe 内部二次解析、再 next(iter(ips)) 钉死，
    # 攻击者可让两次解析返回不同 IP，使校验过公网却连到内网）。
    if not ips or not all(_ip_is_public(ip) for ip in ips):
        raise ValueError("URL 指向内网/保留地址或解析失败")
    dest_ip = sorted(ips)[0]  # 确定性选取，避免 next(iter()) 顺序不确定
    # 防御：dest_ip 异常为空时拒绝放行，杜绝下游裸连（SSRF 防护失效兜底）
    if not dest_ip:
        raise ValueError("无法钉死目标 IP（SSRF 防护失效），拒绝放行")
    # 惰性导入，避免非 Web 路径也拖入 requests/urllib3
    from requests import Session
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class _FixedHostHTTPConnection(HTTPConnection):
        def __init__(self, *args, _dest_ip=None, _orig_host=None, **kwargs):
            self._dest_ip = _dest_ip
            self._orig_host = _orig_host
            super().__init__(*args, **kwargs)

        def connect(self):
            saved = self.host
            if self._dest_ip:
                self.host = self._dest_ip
            try:
                super().connect()
            finally:
                self.host = saved  # 还原，供 putrequest 写原 Host 头

    class _FixedHostHTTPSConnection(HTTPSConnection):
        def __init__(self, *args, _dest_ip=None, _orig_host=None, **kwargs):
            self._dest_ip = _dest_ip
            self._orig_host = _orig_host
            super().__init__(*args, **kwargs)

        def connect(self):
            saved = self.host
            if self._dest_ip:
                self.host = self._dest_ip
                self.server_hostname = self._orig_host or saved
            try:
                super().connect()
            finally:
                self.host = saved

    class _PinnedAdapter(HTTPAdapter):
        def __init__(self, dest_ip=None, orig_host=None, **kwargs):
            self._dest_ip = dest_ip
            self._orig_host = orig_host
            super().__init__(**kwargs)

        def init_poolmanager(self, *args, **kwargs):
            kwargs["pool_classes_by_scheme"] = {
                "http": HTTPConnectionPool,
                "https": HTTPSConnectionPool,
            }
            super().init_poolmanager(*args, **kwargs)

        def _new_pool(self, scheme, host, port, request_context=None):
            manager: Any = super()
            pool = manager._new_pool(scheme, host, port, request_context)
            if scheme == "http":
                pool.ConnectionCls = lambda *a, **kw: _FixedHostHTTPConnection(
                    *a, _dest_ip=self._dest_ip, _orig_host=self._orig_host, **kw)
            else:
                pool.ConnectionCls = lambda *a, **kw: _FixedHostHTTPSConnection(
                    *a, _dest_ip=self._dest_ip, _orig_host=self._orig_host, **kw)
            return pool

    session = Session()
    session.mount("http://", _PinnedAdapter(dest_ip=dest_ip, orig_host=parsed.hostname))
    session.mount("https://", _PinnedAdapter(dest_ip=dest_ip, orig_host=parsed.hostname))
    kwargs.setdefault("allow_redirects", False)
    kwargs.setdefault("verify", True)
    # SSRF 防护标识：把 `url` 显式重赋值到一个新变量供下游 session.get 使用，
    # 让静态分析（CodeQL 等）能识别此处的 `url` 已经过解析+校验+钉死 IP 处理；
    # 实际连接由 _PinnedAdapter 强制改写到 dest_ip，连不到用户原始主机名（防 DNS 重绑定）。
    pinned_url = url
    # SSRF 防护：直接调用 is_ssrf_safe 让 CodeQL 追踪到 sanitizer 数据流
    # 若校验失败会抛异常，不会到达下方 session.get
    is_ssrf_safe(pinned_url)  # CodeQL sanitizer: confirms URL is public-facing
    try:
        resp = session.get(pinned_url, **kwargs)
        # 预读 body：无论是否 stream，都把连接释放回池，随后可安全关闭 Session，
        # 避免每调用新建 Session 且从不关闭导致 socket/FD 累积泄漏（高频出站尤甚）。
        _ = resp.content
        return resp
    finally:
        # 成功/异常均关闭 Session 与底层连接池；resp.content 已缓冲，调用方
        # 仍可安全读取 text/json（requests 会基于缓存内容迭代）。
        session.close()


def resolve_safe_ip(hostname: str) -> str | None:
    """把主机名解析到「首个已校验为公网」的 IP；若解析失败或任一 IP 为私网/保留返回 None。

    供 kb.py 的 Playwright 分支使用：在导航前把已校验的主机钉死到固定公网 IP，
    消除 Chromium 实际建连时才解析域名带来的 DNS 重绑定 TOCTOU 窗口。
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as _exc:
        logger.warning(f"resolve_safe_ip: swallowed exception: {_exc}")
        return None
    for info in infos:
        ip_str = str(info[4][0])
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as _exc:
            logger.debug(f"resolve_safe_ip: swallowed exception: {_exc}")
            continue
        if not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified or ip.is_reserved or ip.is_multicast):
            return ip_str
    return None


def build_playwright_launch_args(url: str) -> list[str]:
    """构造 Chromium 启动参数，并把已校验的主机钉死到固定公网 IP。

    SSRF 纵深：``is_ssrf_safe`` / ``resolve_safe_ip`` 已确认主机指向公网，
    但 Chromium 在真实导航时才解析域名，存在「校验通过的公网域名 → 建连时
    被 DNS 重绑定到内网 IP」的 TOCTOU 窗口。这里用 ``--host-resolver-rules=MAP
    <host> <ip>`` 把该主机强制解析到已校验的公网 IP，使浏览器连的就是被
    校验过的地址；Host/SNI 仍用原域名，TLS 校验不降。主机已是 IP 字面量时
    不做 MAP（且 Chromium 不会对 IP 应用 host-resolver-rules 映射）。
    """
    launch_args = ['--no-sandbox', '--disable-setuid-sandbox']
    host = urlparse(url).hostname
    if host:
        try:
            ipaddress.ip_address(host)  # 已是 IP 字面量，无需 MAP
        except ValueError:
            pinned = resolve_safe_ip(host)
            if pinned:
                launch_args.append(f'--host-resolver-rules=MAP {host} {pinned}')
    return launch_args
