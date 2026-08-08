"""图片签名 Token 与图片服务路由。

从 `web/api.py` 抽取（原 639–693 行），业务逻辑不变。
- _IMG_TOKEN_SECRET / _make_image_token / _verify_image_token 随迁（仅本路由使用）。
- _get_cfg 经 `import web.api as _api` 做属性访问。
- 设计：前端经已认证的 /api/image-token 领 token，拼到 <img src> 的 ?it= 参数；
  因浏览器不会自动带 Authorization 头，图片接口改用一次性短时效签名 token 校验，
  既堵住“免认证读 OCR 截图”的洞，又保持 <img> 可用。
"""

from __future__ import annotations

import asyncio as _asyncio
import base64
import hashlib
import hmac
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

import web.api as _api
from src.paths import data_path, get_skill_icons_dir
from web.security import resolve_safe_ip  # SSRF 防护：白名单 host 仍可能 DNS 重绑定到内网，需钉公网 IP

router = APIRouter()


# 签名密钥持久化到磁盘：避免每次服务重启都重新随机生成，
# 否则重启会让所有已签发的图片 token 立即失效（前端需刷新页面才恢复）。
# 首次启动时生成并写入，之后复用同一把密钥。
_IMG_TOKEN_SECRET_FILE = data_path(".image_token_secret")


def _load_img_token_secret() -> bytes:
    try:
        if _IMG_TOKEN_SECRET_FILE.exists():
            return _IMG_TOKEN_SECRET_FILE.read_bytes()
    except Exception:
        pass
    secret = os.urandom(32)
    try:
        _IMG_TOKEN_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _IMG_TOKEN_SECRET_FILE.write_bytes(secret)
        _IMG_TOKEN_SECRET_FILE.chmod(0o600)
    except Exception:
        pass  # 写盘失败则回退为本次内存密钥（重启会轮换，但不影响单次运行）
    return secret


_IMG_TOKEN_SECRET = _load_img_token_secret()
_IMG_TOKEN_TTL = 300  # 秒（5 分钟）


def _make_image_token() -> str:
    exp = int(time.time()) + _IMG_TOKEN_TTL
    sig = hmac.new(_IMG_TOKEN_SECRET, str(exp).encode(), hashlib.sha256).digest()
    return f"{exp}." + base64.urlsafe_b64encode(sig).decode()


def _verify_image_token(token: Optional[str]) -> bool:
    if not token or "." not in token:
        return False
    try:
        exp_s, sig_b64 = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    try:
        sig = base64.urlsafe_b64decode(sig_b64)
    except Exception:
        return False
    expected = hmac.new(_IMG_TOKEN_SECRET, str(exp).encode(), hashlib.sha256).digest()
    return hmac.compare_digest(sig, expected)


@router.get("/api/image-token")
async def issue_image_token():
    """领取图片访问 token（需 Basic Auth）。前端缓存后拼到图片 URL。"""
    return {"token": _make_image_token(), "ttl": _IMG_TOKEN_TTL}


@router.get("/api/image/{path:path}")
async def serve_image(path: str, it: Optional[str] = None):
    """提供持久化 OCR 图片（签名 token 校验，嵌套子目录如 张三/ocr_xxx.png）。"""
    if not _verify_image_token(it):
        raise HTTPException(status_code=401, detail="Invalid or expired image token")
    try:
        cfg = _api._get_cfg()
        if cfg is None:
            raise HTTPException(status_code=500, detail="Config unavailable")
        base = Path(cfg.poller.image_temp_dir).expanduser().resolve()
        full = (base / path).resolve()
        # 防止路径穿越：严格判定 full 必须位于 base 之内（base 自身或以 base/ 为前缀）
        if full != base and not str(full).startswith(str(base) + os.sep):
            raise HTTPException(status_code=403, detail="Forbidden")
        if not full.exists() or not full.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(str(full))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── 外部图片代理（解决 CSP img-src 限制）──────────────────────────────
# SkillHub 技能市场图标托管在 cloudcache.tencent-cloud.com 等外网 CDN，浏览器
# 受 CSP `img-src 'self' data: blob:` 拦截直接加载。后端经白名单代理后，浏览器
# 只同源（self）拉取，规避 CSP 并复用浏览器缓存。
# 安全：仅放行白名单 host（防 SSRF）；https only；响应 content-type 必须 image/*；
# 大小 ≤ 8MB；单请求 ≤ 15s。

_IMAGE_PROXY_ALLOWED_HOSTS: frozenset[str] = frozenset({
    "cloudcache.tencent-cloud.com",
    "tencent-cloud.com",
    "mycloud.com",
    "qcloud.com",
    "cloudcache-1258344699.cos.ap-guangzhou.myqcloud.com",
})
_IMAGE_PROXY_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
_IMAGE_PROXY_MAX_BYTES = 8 * 1024 * 1024


@router.get("/api/proxy/image")
async def proxy_image(url: str = ""):
    """代理加载白名单外部图片（CSP 友好）。"""
    if not url:
        raise HTTPException(status_code=400, detail="url 参数必填")
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="url 格式无效") from None
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="仅支持 https")
    host = (parsed.hostname or "").lower()
    # 白名单后缀匹配：支持子域名（如 xxx.tencent-cloud.com 匹配 tencent-cloud.com）
    allowed = any(host == h or host.endswith("." + h) for h in _IMAGE_PROXY_ALLOWED_HOSTS)
    if not allowed:
        raise HTTPException(status_code=403, detail=f"host 不在白名单: {host}")

    # SSRF 纵深：白名单 host 仍可能经 DNS 重绑定解析到内网 IP，故再解析并钉死
    # 公网 IP，用钉 IP 的 URL 发起请求（Host/SNI 仍为原域名，TLS 校验不降）。
    dest_ip = resolve_safe_ip(host)
    if not dest_ip:
        raise HTTPException(status_code=502, detail="上游地址解析失败或被 SSRF 防护拦截")
    _port = f":{parsed.port}" if parsed.port else ""
    pinned_url = urlunparse(parsed._replace(netloc=f"{dest_ip}{_port}"))

    try:
        # 不跟随重定向（防 SSRF 重定向到非白名单 host）；CDN 直链通常不重定向
        async with httpx.AsyncClient(timeout=_IMAGE_PROXY_TIMEOUT, follow_redirects=False) as client:
            r = await client.get(
                pinned_url,
                headers={"User-Agent": "Linkora-ImageProxy/1.0", "Host": host},
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail="上游请求失败") from e

    if r.status_code != 200 or r.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"上游返回 {r.status_code}")

    ctype = r.headers.get("content-type", "")
    if not ctype.startswith("image/"):
        raise HTTPException(status_code=502, detail=f"非图片内容: {ctype[:40]}")

    body = r.content
    if len(body) > _IMAGE_PROXY_MAX_BYTES:
        raise HTTPException(status_code=502, detail="图片过大")

    return Response(
        content=body,
        media_type=ctype.split(";")[0].strip() or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "X-Proxy-Source": host,
        },
    )


# ── SkillHub 技能图标本地缓存服务 ──────────────────────────────────────
# 技能安装/同步时将图标下载到 data/skill_icons/<slug_safe>.png 本地存储，
# 彻底摆脱外部云存储 URL 的可用性/认证/签名过期依赖。图标缺失时返回基于
# slug 首字母 + 哈希色的 SVG 兜底图标，不依赖任何外部资源。

def _slug_to_safe_name(slug: str) -> str:
    """将 skill slug 转为安全文件名：去 @、/→-、保留字母数字-_.

    末尾 ``os.path.basename`` 为路径穿越防护兜底：即便上游 slug 含路径分隔符，
    也只保留最后一段，确保拼到 ``get_skill_icons_dir()`` 后不会越界写文件。
    （CodeQL py/path-injection 的显式 sanitizer 触发点。）
    """
    s = slug.lstrip("@").replace("/", "-")
    s = "".join(c for c in s if c.isalnum() or c in "_-") or "default"
    return os.path.basename(s)

def _slug_color(slug: str) -> str:
    """基于 slug 生成稳定的 HSL 色相（同一 slug 始终同色）。"""
    import hashlib as _hl
    h = int(_hl.md5(slug.encode()).hexdigest(), 16) % 360
    return f"hsl({h}, 55%, 50%)"


# 正在下载中的 safe_name 集合，防止并发重复下载
_downloading: set[str] = set()

# 后台任务强引用池。事件循环只持有 task 的弱引用，裸 create_task 的返回值若不
# 保存，任务可能在完成前被 GC（CPython 官方文档明示）。对图标下载而言后果是
# _download_and_release 的 finally 不执行 → safe_name 永远留在 _downloading，
# 该图标此后永久卡在 SVG 兜底、不再重试。故统一经 _spawn_bg 持有引用。
_BG_TASKS: set[_asyncio.Task] = set()


def _spawn_bg(coro) -> _asyncio.Task:
    """派发后台任务并持有强引用，完成后自动移除。"""
    task = _asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


@router.get("/api/skill-icons/{slug}")
async def serve_skill_icon(slug: str):
    """提供 SkillHub 技能图标。

    优先级：本地 PNG 缓存 > 异步触发下载 + SVG 兜底。
    首次请求时若本地无缓存，会自动触发后台下载；下次请求命中 PNG。
    """
    safe = _slug_to_safe_name(slug)
    # os.path.basename 在 sink 拼接处显式净化（CodeQL py/path-injection sanitizer），
    # 与 _slug_to_safe_name 内部兜底构成双重保险，确保拼入路径的只有文件名。
    icon_path = get_skill_icons_dir() / f"{os.path.basename(safe)}.png"
    if icon_path.is_file():
        return FileResponse(
            str(icon_path),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )

    # 无本地缓存：尝试获取原始 iconUrl 并异步下载
    raw_url = _lazy_resolve_icon_url(slug, safe)
    if raw_url and safe not in _downloading:
        _downloading.add(safe)
        _spawn_bg(_download_and_release(slug, raw_url, safe))

    # 返回 SVG 兜底图标（首字母 + 哈希色）
    initial = next((c.upper() for c in slug if c.isalnum()), "S")
    color = _slug_color(slug)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"'
        f' viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="12" fill="{color}"/>'
        f'<text x="32" y="42" text-anchor="middle" font-size="28"'
        f' font-family="system-ui,sans-serif" font-weight="700"'
        f' fill="#fff">{initial}</text></svg>'
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


async def _download_and_release(slug: str, raw_url: str, safe: str) -> None:
    """下载图标并释放 _downloading 锁。"""
    try:
        await _download_skill_icon(slug, raw_url)
    finally:
        _downloading.discard(safe)


def _lazy_resolve_icon_url(slug: str, safe: str) -> str | None:
    """懒解析图标原始 URL：先查 _ICON_URL_MAP，若为空则触发排行榜刷新。"""
    from web.dependencies import _get_raw_icon_url

    # 用原始 slug 和 safe_name 分别查
    raw = _get_raw_icon_url(slug) or _get_raw_icon_url(safe)
    if raw:
        return raw

    # _ICON_URL_MAP 为空 → 排行榜数据从未加载，触发异步刷新
    # （不阻塞当前请求，后台填充后后续请求可命中）
    from web.dependencies import _ICON_URL_MAP
    if not _ICON_URL_MAP:
        _spawn_bg(_lazy_fill_icon_url_map())

    return None


async def _lazy_fill_icon_url_map() -> None:
    """后台填充 _ICON_URL_MAP（仅在首次请求且 map 为空时调用）。"""
    try:
        from web.dependencies import _fetch_market_rankings
        await _fetch_market_rankings(force=False)
    except Exception:
        pass


async def _download_skill_icon(slug: str, icon_url: str) -> bool:
    """下载技能图标到本地缓存 data/skill_icons/<safe>.png。

    仅处理 http(s) 外链；目标已存在时跳过（幂等）。返回是否成功落盘。
    """
    if not icon_url or not icon_url.startswith(("http://", "https://")):
        return False
    safe = _slug_to_safe_name(slug)
    dest = get_skill_icons_dir() / f"{os.path.basename(safe)}.png"
    if dest.is_file():
        return True  # 已有缓存，幂等跳过
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            follow_redirects=True,
        ) as client:
            r = await client.get(icon_url, headers={"User-Agent": "Linkora-SkillIcon/1.0"})
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "")
                if ctype.startswith("image/"):
                    dest.write_bytes(r.content)
                    return True
    except Exception:
        pass
    return False


def _skill_icon_local_url(slug: str) -> str | None:
    """若技能图标已本地缓存则返回 /api/skill-icons/<safe> 路径，否则 None。"""
    safe = _slug_to_safe_name(slug)
    if (get_skill_icons_dir() / f"{os.path.basename(safe)}.png").is_file():
        return f"/api/skill-icons/{safe}"
    return None


# ── 主动批量预取图标 ──────────────────────────────────────────────────
# 排行榜数据加载后立即批量下载所有技能图标到本地缓存，确保用户首次打开
# 市场页面时图标已就绪，彻底消除"首次 SVG 兜底→刷新才显示"的体验割裂。

_MAX_CONCURRENT_DOWNLOADS = 8  # 并发下载上限，避免打爆网络


async def prefetch_all_skill_icons(icon_url_map: dict[str, str]) -> None:
    """批量下载所有尚未缓存的技能图标到 data/skill_icons/。

    在后台异步执行，不影响主请求响应。跳过已存在的图标（幂等）。
    """
    to_download: list[tuple[str, str]] = []
    for slug, raw_url in icon_url_map.items():
        if not raw_url or not raw_url.startswith(("http://", "https://")):
            continue
        safe = _slug_to_safe_name(slug)
        # 跳过安全名的反向映射条目（safe→url），只处理真正的 slug
        if safe != slug and slug in icon_url_map and safe in icon_url_map:
            continue
        dest = get_skill_icons_dir() / f"{os.path.basename(safe)}.png"
        if dest.is_file():
            continue
        to_download.append((slug, raw_url))

    if not to_download:
        return

    sem = _asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

    async def _download_one(slug: str, url: str) -> None:
        async with sem:
            await _download_skill_icon(slug, url)

    tasks = [_download_one(slug, url) for slug, url in to_download]
    await _asyncio.gather(*tasks, return_exceptions=True)
