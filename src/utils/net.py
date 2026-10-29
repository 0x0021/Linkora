"""统一出站网络请求的 SSRF 防护（src 层单一真源）。

原实现位于 ``web/security.py``，现下沉到 ``src`` 层，使非 Web 路径（tools 等）也能
复用同一套 SSRF 校验逻辑，避免「防护只在 Web 侧落地、工具侧裸奔」的不对称。

Web 层 ``web/security.py`` 从此处再导出，保持 ``from web.security import ...`` 兼容。

约定（出站 SSRF 强制闸门）：
- 任何「用户可控 URL」出站（导入/抓取/Webhook）必须走 ``ssrf_safe_get`` 或
  ``safe_http_request``，禁止裸 ``httpx.Client`` / ``requests.get``，确保 DNS
  重绑定防护（IP 钉死）生效。
- 「配置端点」（部署方可信域名，如 embedding/KB 地址）可裸调，但调用处须注释
  标明「配置端点，可信；一旦改为用户可控必须改走 guard」。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 本地透明代理（Clash / ClashX / Surge 等 fake-IP 模式）拦截 DNS，把公网域名解析成
# 198.18.0.0/15（TEST-NET-2，RFC 2544 保留段，仅作代理转发占位，绝非真实内网）。
# 该段无法寻址任何真实内网 / 云元数据（169.254.169.254 等），连接会被代理转回真实
# 公网目的，故在此放行——否则所有经代理的出站请求都会被误判为「内网/保留地址」整批失败。
# 真实内网（10/8、172.16/12、192.168/16、127/8、169.254/16 等）仍照常拦截。
_PROXY_FAKEIP_NETS = [ipaddress.ip_network("198.18.0.0/15")]


def _ip_is_public(ip_str: str) -> bool:
    """单 IP 是否允许出站：仅「全球单播」可放行，其余一律拒绝（egress 默认拒绝）。

    默认拒绝一切非全球可路由地址——私网/回环/链路本地/未指定/保留/组播/CGNAT
    (100.64.0.0/10)/文档段(TEST-NET) 等均拒绝；代理 fake-IP 段（198.18.0.0/15）
    作为显式白名单放行。这比「黑名单枚举」更稳：新增保留段不会漏放。

    供 ``is_ssrf_safe`` 与 ``ssrf_safe_get`` 复用，保证「校验」与「钉死 IP」使用
    同一份解析结果，杜绝 DNS 重绑定 TOCTOU 窗口。
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if any(ip in net for net in _PROXY_FAKEIP_NETS):
        return True
    # 仅「全球单播」可放行；多播(is_global 为 True 但绝不能出站)显式拒绝，
    # 其余非全球可路由地址（私网/回环/链路本地/未指定/保留/CGNAT/文档段）由
    # is_global 统一兜底拒绝。比「黑名单枚举」更稳：新增保留段不会漏放。
    return ip.is_global and not ip.is_multicast


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


def _ensure_pinning_supported(pool_manager: Any) -> None:
    """守卫：确认 urllib3 允许替换「建池用的连接池类」，否则 IP 钉死会**静默失效**。

    IP 钉死是 SSRF 防护的最后一环（消除 DNS 重绑定 TOCTOU）。若上游 urllib3 改了
    ``PoolManager.pool_classes_by_scheme`` 的存在性或类型，我们改不动 → 连接会走
    普通 DNS 解析 → TOCTOU 窗口重新打开，而外部**看不出任何异常**。
    故此处 fail-closed：抛可读错误，绝不静默降级。
    """
    classes = getattr(pool_manager, "pool_classes_by_scheme", None)
    if not isinstance(classes, dict):
        raise RuntimeError(
            "当前 urllib3 的 PoolManager 不支持替换 pool_classes_by_scheme，"
            "SSRF 的「IP 钉死」无法生效（会出现 DNS 重绑定 TOCTOU 窗口），已拒绝发起请求。\n"
            "请检查 urllib3 版本：requirements.txt 已钉 urllib3==2.8.0，"
            "建议执行 `pip install -U 'urllib3==2.8.0'` 后重试。"
        )


def _pin_session(url: str, **kwargs):
    """解析+校验+钉死 IP，返回 ``(Session, url)``；任一 IP 私网/保留即抛 ``ValueError``。

    供 ``ssrf_safe_get`` / ``safe_http_request`` 复用，确保「校验」与「建连」使用同一地址，
    消除 DNS 重绑定 TOCTOU 窗口。

    实现要点（均为真实踩过的坑，勿回退）：

    1. **禁止**往 ``PoolManager(**kwargs)`` 塞 urllib3 不认识的参数（如
       ``pool_classes_by_scheme``）。urllib3 会把未知参数留在 ``connection_pool_kw`` 里，
       随后被拷进 ``request_context``，``_default_key_normalizer`` 再给每个 key 加
       ``key_`` 前缀 → ``PoolKey`` 收到未知字段 →
       ``TypeError: PoolKey.__new__() got an unexpected keyword argument
       'key_pool_classes_by_scheme'`` → **全站出站工具瘫痪**（见 issue #46）。
    2. ``HTTPAdapter._new_pool`` 在 requests ≥ 2.34 已被删除，覆盖它是**死代码**——
       真正的建池入口是 urllib3 ``PoolManager._new_pool``，池用什么连接类由
       ``PoolManager.pool_classes_by_scheme``（实例属性）决定。故改池类，而非覆盖
       adapter 方法（旧实现等于「SSRF 钉死从未真正生效」）。
    3. 钉 IP 靠替换 ``HTTPConnectionPool.ConnectionCls``：在 ``_new_conn``（真正建 socket）
       期间把 ``self._dns_host`` 换成钉死 IP（urllib3 1.26 / 2.x 建连都读 ``_dns_host``），
       建完立即还原 → Host 头与 TLS SNI 仍是原域名，证书校验不受影响。
    4. **代理场景不钉 IP**：走代理时由代理解析域名，socket 应连代理而非目标 IP。
       故只替换直连 ``poolmanager`` 的池类，代理 manager 保持默认（与旧行为一致）。
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

    def _make_pinned_connection_cls(base_cls: Any) -> Any:
        """生成「建连时把 socket 目标换成钉死 IP」的连接类。

        只在 ``_new_conn``（真正建 socket）期间替换 ``_dns_host``，建完立即还原：
        这样 Host 头与 TLS SNI 仍是原始域名（证书校验、SNI 均不受影响），
        而 TCP 连接的目标是已校验过的公网 IP。
        """

        class _PinnedConnection(base_cls):  # type: ignore[misc, valid-type]
            def _new_conn(self) -> Any:
                saved = self._dns_host
                self._dns_host = dest_ip
                try:
                    return super()._new_conn()
                finally:
                    self._dns_host = saved  # 还原，Host 头 / SNI 仍用原域名

        return _PinnedConnection

    class _PinnedHTTPConnectionPool(HTTPConnectionPool):  # type: ignore[misc, valid-type]
        ConnectionCls = _make_pinned_connection_cls(HTTPConnection)

    class _PinnedHTTPSConnectionPool(HTTPSConnectionPool):  # type: ignore[misc, valid-type]
        ConnectionCls = _make_pinned_connection_cls(HTTPSConnection)

    class _PinnedAdapter(HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            # 注意：这里**不能**往 kwargs 里加 urllib3 不认识的参数（见函数 docstring 第 1 点）。
            super().init_poolmanager(*args, **kwargs)
            # 建池用的连接池类由 PoolManager 的实例属性决定，替换它即可让直连请求
            # 走「钉死 IP」的连接类（见 docstring 第 2、3 点）。
            manager = getattr(self, "poolmanager", None)
            if manager is not None:
                _ensure_pinning_supported(manager)
                manager.pool_classes_by_scheme = {
                    "http": _PinnedHTTPConnectionPool,
                    "https": _PinnedHTTPSConnectionPool,
                }

    session = Session()
    session.mount("http://", _PinnedAdapter())
    session.mount("https://", _PinnedAdapter())
    pinned_url = url
    is_ssrf_safe(pinned_url)  # 双保险：私网/保留地址直接抛 ValueError，阻断后续建连
    return session, pinned_url


def ssrf_safe_get(url: str, **kwargs):
    """解析一次并固定 IP 发起请求，消除 SSRF DNS 重绑定 TOCTOU。

    先解析 + 校验（任一 IP 私网/保留即拒），再用固定 IP 建连，
    Host/SNI 仍为原域名，证书校验不关闭。供 Web/工具层 URL 导入与出站抓取统一使用。
    """
    kwargs.setdefault("allow_redirects", False)
    kwargs.setdefault("verify", True)
    session, pinned_url = _pin_session(url, **kwargs)
    try:
        resp = session.get(pinned_url, **kwargs)
        _ = resp.content  # 预读 body，释放连接回池
        return resp
    finally:
        session.close()


def safe_http_request(method: str, url: str, **kwargs):
    """通用安全出站：支持任意 HTTP method，强制 SSRF 钉死 IP。

    用户可控 URL 一律经此（而非裸 ``httpx.Client`` / ``requests``），确保 DNS 重绑定
    防护。配置端点（可信部署方域名）可裸调，但调用处须注释标明「配置端点，可信；一旦
    改为用户可控必须改走 guard」。
    """
    kwargs.setdefault("allow_redirects", False)
    kwargs.setdefault("verify", True)
    session, pinned_url = _pin_session(url, **kwargs)
    try:
        resp = session.request(method.upper(), pinned_url, **kwargs)
        _ = resp.content
        return resp
    finally:
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
        if _ip_is_public(ip_str):
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
