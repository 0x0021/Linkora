from __future__ import annotations

import base64
import hmac
import ipaddress
import logging
import os
import re
import threading
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

# 抑制 jieba 内部 pkg_resources 弃用警告（在 import jieba 之前设置）
warnings.filterwarnings("ignore", category=DeprecationWarning, module="pkg_resources")
warnings.filterwarnings("ignore", message=".*pkg_resources.*")


from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from web.errors import SAFE_INTERNAL_ERROR

from src.config import load_config
from src.dws_adapter import DwsAdapter
from src.shared_state import get_app_instance, get_config as _get_shared_config
from src.utils.request_id import request_id_scope
from src.utils.security import mask_oid, sanitize_log_message
from src.paths import (
    get_config_path, get_data_dir, get_log_dir, get_static_dir,
    get_templates_dir, get_user_data_dir, is_frozen, data_path,
)
# 共享资源访问器（get_store 等）下沉到 web.dependencies，避免与各子路由循环导入。
from web.dependencies import (
    get_store,  # noqa: F401  # 通过 web.api 命名空间 re-export，供 routers 以 _api.get_store 访问（兼容 monkeypatch）
    get_platforms,  # noqa: F401
    get_rag_config,  # noqa: F401
    set_platform_context,
    _platform_ctx,
)

if TYPE_CHECKING:
    # 仅供类型标注：EmbeddingClient 运行时按需惰性 import（内部会拉起
    # sentence-transformers/torch，顶层导入会拖慢 Web 启动数十秒）。
    from src.memory.embedding import EmbeddingClient
    from src.config_models import AppConfig

logger = logging.getLogger(__name__)

# 模块级配置缓存（仅作回退）：未启动主进程（单元测试 / 独立运行 Web）时按
# “路径 + mtime”失效读取磁盘；生产环境由主进程发布的配置单例接管（见 _get_cfg）。
_cfg_cache = None
_cfg_cache_path = None
_cfg_cache_mtime = -1


def _load_cfg_from_disk():
    """按 mtime 缓存读取磁盘配置（测试 monkeypatch CONFIG_PATH 时仍生效）。"""
    global _cfg_cache, _cfg_cache_path, _cfg_cache_mtime
    try:
        p = CONFIG_PATH
        mtime = os.path.getmtime(p) if os.path.exists(p) else -1
    except OSError as e:
        logger.debug("获取配置文件 mtime 失败: %s", e)
        mtime = -1
    if _cfg_cache is not None and _cfg_cache_path == p and _cfg_cache_mtime == mtime:
        return _cfg_cache if _cfg_cache is not False else None
    try:
        cfg = load_config(p)
        _cfg_cache, _cfg_cache_path, _cfg_cache_mtime = cfg, p, mtime
        return cfg
    except Exception as e:
        logger.warning("加载配置文件失败: %s", e)
        _cfg_cache, _cfg_cache_path, _cfg_cache_mtime = False, p, mtime
        return None


def _get_cfg():
    """获取全局配置（失败则返回 None，不抛异常）。

    优先级：
    1. 主进程发布的配置单例（shared_state.get_config）——生产环境唯一真源，
       热重载与 Web 改配置后 Web 端立即读到同一对象，消除“磁盘/main/api”三处
       真源不一致及每请求重读磁盘的开销。
    2. 回退到磁盘（按 mtime 缓存）：测试 monkeypatch CONFIG_PATH 或独立运行
       Web（未启动主进程、单例为 None）时使用，保证行为与旧逻辑一致。
    """
    shared = _get_shared_config()
    if shared is not None:
        return shared
    return _load_cfg_from_disk()


def _require_cfg() -> "AppConfig":
    """获取全局配置，缺失时抛 503 而非让调用方裸解引用 None。

    _get_cfg() 在「配置文件缺失/解析失败且主进程未发布单例」时返回 None。此前各
    路由普遍写 `config = _api._get_cfg()` 后直接 `config.rules.xxx`，None 时抛
    AttributeError('NoneType' object has no attribute 'rules')，被路由的
    `except Exception` 兜成 HTTP 500 + 一句看不懂的报错，运维无从判断是配置没加载。
    本函数把它收敛成语义明确的 503（服务尚未就绪），同时让类型检查器把返回值
    收窄为非 Optional，消除下游一整片 reportOptionalMemberAccess。
    """
    config = _get_cfg()
    if config is None:
        raise HTTPException(status_code=503, detail="配置尚未就绪（配置文件缺失或解析失败）")
    return config

# 提高 uvicorn.access 日志级别（避免 API 请求刷屏）
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)


app = FastAPI(title="灵桥 (Linkora) 管理后台", version="2.0")

# 全链路 gzip 压缩：HTML / JSON API / 静态 bundle 一并压缩（首屏文本体积约降 75–86%）。
# minimum_size=1024 跳过极小响应（如 304/空体），避免无谓的压缩开销。
app.add_middleware(GZipMiddleware, minimum_size=1024)


# F29：每个 Web 请求分配独立 request_id 并贯穿日志（Web→Runtime 链路可见）；
# 同时回写 X-Request-Id 响应头，便于前端/网关侧关联。
@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    with request_id_scope(prefix="web") as rid:
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
    return response


# ============ Global Exception Handler ============
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局兜底：未捕获的异常返回 500 并完整记录 traceback，避免静默丢弃。"""
    logger.error(
        "[API 500] Unhandled exception on %s %s: %s",
        request.method, request.url.path, exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """统一收敛异常信息泄露（CodeQL py/stack-trace-exposure）。

    - 4xx（客户端输入 / 鉴权问题）：保留原 detail，便于前端 / 用户定位，
      这类文案不含内部结构，可安全回传。
    - 5xx（服务器内部错误）：无论路由里写的是 ``detail=str(e)`` 还是别的，
      一律替换为安全常量文案；真实异常仅记入服务端日志（含 traceback），
      从响应体切断「异常文本 → 客户端」的链路。
    """
    if exc.status_code >= 500:
        logger.error(
            "[API %d] Internal error on %s %s: %s",
            exc.status_code, request.method, request.url.path, exc.detail,
            exc_info=True,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": SAFE_INTERNAL_ERROR},
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


_BASE_DIR = get_static_dir().parent  # = web/ 目录（开发态仓库根 / 冻结态 _MEIPASS/web）

# 自定义 StaticFiles：版本感知缓存
# index.html 通过 ?v=<文件mtime> 引用所有 CSS/JS，故带 ?v= 的资源可长缓存(immutable)，
# 靠 mtime 版本号自动失效。生产态走 dist/ 内容哈希单 bundle（哈希在文件名、URL 无 ?v=），
# 同样按文件名判 immutable。其余未版本化资源（vendor / fontawesome 等）保留 ETag/Last-Modified
# 走 no-cache，允许 304 协商，避免重复完整下载。
# ⚠️ 历史坑：早期实现对所有「不带 ?v=」资源删除 ETag/Last-Modified，导致连 304 都走不了，
# 生产态反而比开发态更差。修正后未版本化资源仅 no-cache（保留验证器）。
_HASH_BUNDLE_RE = re.compile(r"bundle\.[0-9a-f]{8,}\.(?:js|css)$")


class VersionedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if not hasattr(response, "headers"):
            return response
        qs = scope.get("query_string", b"").decode("latin-1")
        is_immutable = (
            "v=" in qs
            or "/dist/" in path
            or _HASH_BUNDLE_RE.search(path) is not None
        )
        if is_immutable:
            # 版本化 / 内容哈希资源：长缓存，浏览器不再重复下载（除 hash/?v 变化外）
            response.headers["Cache-Control"] = "public, max-age=86400, immutable"
        else:
            # 未版本化：每次校验，但保留 ETag/Last-Modified 允许 304 协商，
            # 避免重复完整下载（原先误删验证器导致连 304 都走不了）。
            response.headers["Cache-Control"] = "no-cache"
        # 清理原 NoCache 遗留的禁用缓存头（MutableHeaders 大小写不敏感）
        for _h in ("pragma", "expires"):
            if _h in response.headers:
                del response.headers[_h]
        return response

CONFIG_PATH = str(get_config_path())

# 配置写前备份目录。默认 data/config-backups/；测试可 monkeypatch 此变量，
# 把备份重定向到临时目录，避免测试写配置污染真实 data/ 目录（见 tests/conftest.py）。
CONFIG_BACKUP_ROOT = data_path("config-backups")

app.mount("/static", VersionedStaticFiles(directory=str(get_static_dir())), name="static")

# ============ Web Auth Middleware ============
# /api/platforms 仅暴露 id/display_name/enabled/adapter_type（无密钥），供前端切换器
# 在登录前即可渲染，故加入白名单免认证。
_AUTH_WHITELIST = {"/", "/health", "/api/platforms"}
# /api/image/ 和 /api/skill-icons/ 保留在白名单（免 Basic Auth），
# 因为前端用 <img src> 直接渲染，浏览器不会自动带 Basic Auth 头。
# /api/image/ 内部已用签名 token 做二次校验。
_AUTH_WHITELIST_PREFIXES = ("/static/", "/api/image/", "/api/skill-icons/")

# —— 登录失败限流（防爆破）——
# key（IP 或 "IP|username"） -> {"fails": 失败时间戳列表, "block_until": 封锁截止时间戳}
# 窗口内失败超阈值则临时封锁；触发后额外延长封锁至 now + _AUTH_BLOCK_SECONDS，
# 避免「窗口刚过即可立即再爆破」（原 _AUTH_BLOCK_SECONDS 为死代码，现激活）。
_AUTH_FAILS: dict[str, dict] = {}
_AUTH_FAIL_LOCK = threading.Lock()
_AUTH_MAX_FAILS = 5          # 窗口内最大失败次数
_AUTH_FAIL_WINDOW = 300.0    # 失败计数窗口（秒）
_AUTH_BLOCK_SECONDS = 300.0  # 触发后封锁时长（秒）


def _is_local_ip(ip_str: str) -> bool:
    """判断是否为回环/私网地址（通常表示请求经过反向代理到达本服务）。"""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_loopback or ip.is_private
    except (ValueError, TypeError):
        return False


def _client_ip(request: Request) -> str:
    """获取客户端 IP（用于登录限流）。

    安全策略：优先使用 TCP 连接 IP（request.client.host）。
    仅当直连 IP 为回环/私网地址（即请求经过反向代理）时才信任 X-Forwarded-For
    的最右端值（由可信边缘代理追加的真实连接 IP）。
    防止服务直接暴露时攻击者伪造 XFF 绕过限流。
    """
    direct = request.client.host if request.client else "unknown"
    # 仅当直连来自回环/私网（说明在反代后面）时才信任 XFF
    if _is_local_ip(direct):
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            # XFF 链为 client, proxy1, proxy2…；最右端是受信任边缘代理追加的真实连接 IP，
            # 客户端只能伪造左侧值。取最右而非最左，防止伪造 IP 绕过登录限流（F18）。
            return fwd.split(",")[-1].strip()
    return direct


def _auth_rate_allowed(key: str) -> bool:
    """返回 True 表示允许尝试（未被封锁）。

    key 为 IP（主维度）或 "IP|username"（账号维度，防固定 IP 多账号轮询）。
    任一被封锁则拒绝。
    """
    now = time.time()
    with _AUTH_FAIL_LOCK:
        # 顺便清理过期条目，防止 _AUTH_FAILS 字典无界增长
        if len(_AUTH_FAILS) > 1000:
            stale = [
                k for k, v in _AUTH_FAILS.items()
                if (not v["fails"] or all(now - t >= _AUTH_FAIL_WINDOW for t in v["fails"]))
                and v["block_until"] <= now
            ]
            for k in stale:
                _AUTH_FAILS.pop(k, None)
        entry = _AUTH_FAILS.get(key)
        if not entry:
            return True
        # 仍处于独立封锁期内（block_until），直接拒绝
        if entry["block_until"] > now:
            return False
        # 清理过期失败记录
        fails = [t for t in entry["fails"] if now - t < _AUTH_FAIL_WINDOW]
        entry["fails"] = fails
        if len(fails) >= _AUTH_MAX_FAILS:
            # 窗口内失败超阈值：延长封锁至 now + _AUTH_BLOCK_SECONDS
            entry["block_until"] = now + _AUTH_BLOCK_SECONDS
            return False
        return True


def _auth_record_fail(key: str) -> None:
    now = time.time()
    with _AUTH_FAIL_LOCK:
        entry = _AUTH_FAILS.setdefault(key, {"fails": [], "block_until": 0.0})
        entry["fails"].append(now)
        # 达到阈值立即进入封锁期，避免本次之后的爆破在窗口陈旧间隙得逞
        if len(entry["fails"]) >= _AUTH_MAX_FAILS:
            entry["block_until"] = now + _AUTH_BLOCK_SECONDS


def _auth_check(username: str, password: str, cfg) -> bool:
    """恒定时间比对，避免时序侧信道。"""
    expected_u = (cfg.web.auth_username or "").encode("utf-8")
    expected_p = (cfg.web.auth_password or "").encode("utf-8")
    return hmac.compare_digest(username.encode("utf-8"), expected_u) and \
        hmac.compare_digest(password.encode("utf-8"), expected_p)


# 敏感端点：即便全局 auth_enabled=False，也强制要求凭据（纵深防御）。
# 覆盖：所有非 GET 的写操作 + 显式列出的敏感只读（配置导出）。
_SENSITIVE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SENSITIVE_GET_PATHS = {"/api/config/export", "/api/logs"}


def _is_sensitive_request(request: Request) -> bool:
    if request.method in _SENSITIVE_METHODS:
        return True
    return request.url.path in _SENSITIVE_GET_PATHS


def _require_basic_auth(request: Request) -> JSONResponse | None:
    """校验 Basic Auth；通过返回 None，失败返回错误响应（已含限流）。

    限流维度：IP 为主、账号（"IP|username"）为辅，任一被封锁则拒绝，
    防固定 IP 多账号轮询爆破。未带/无法解析用户名时仅按 IP 维度计。
    """
    ip = _client_ip(request)
    if not _auth_rate_allowed(ip):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed login attempts. Try again later."},
        )
    auth_header = request.headers.get("Authorization", "")
    
    # 支持 Basic Auth 和 Bearer Token (JWT)
    try:
        if auth_header.startswith("Basic "):
            # Basic Auth 模式
            creds = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, password = creds.split(":", 1)
            logger.debug("Basic auth 尝试: user=%s", mask_oid(username[:3] if len(username) > 3 else username))
        elif auth_header.startswith("Bearer "):
            # JWT Token 模式
            from web.auth_middleware import _token_manager
            payload = _token_manager.verify_token(auth_header[7:])
            request.state.jwt_payload = payload
            request.state.username = payload.get("sub", "unknown")
            request.state.role = payload.get("role", "viewer")
            return None  # JWT 认证成功，直接放行
        else:
            return JSONResponse(
                status_code=401,
                content={"detail": "Unsupported auth type. Use 'Basic' or 'Bearer'"},
            )
    except HTTPException as e:
        logger.warning("auth 失败: %s", sanitize_log_message(str(e)))
        _auth_record_fail(ip)
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    except Exception as e:
        logger.warning("basic auth 凭据解码失败: %s", sanitize_log_message(str(e)))
        _auth_record_fail(ip)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials format"},
        )
    # 账号维度限流（IP 已通过；此处独立计数，防固定 IP 多账号轮询）
    account_key = f"{ip}|{username}"
    if not _auth_rate_allowed(account_key):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed login attempts. Try again later."},
        )
    config = _get_cfg()
    if config is None or not _auth_check(username, password, config):
        _auth_record_fail(ip)
        _auth_record_fail(account_key)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid username or password"},
        )
    return None


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """为所有 HTTP 响应注入安全头（本地内网管理工具，非对公网服务）。

    防点击劫持 / MIME 嗅探 / XSS 反射，对内部工具的安全水位提升。
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: 本地管理工具，放宽以适配 Bootstrap/FontAwesome 等内联样式/CDN
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'"
    )
    return response


@app.middleware("http")
async def web_auth_middleware(request: Request, call_next):
    """HTTP Basic Auth 中间件。未认证时返回 401，前端据此弹出登录框。

    鉴权策略：
    - 白名单路径（/、/health、/api/platforms、/static、/api/image）始终放行；
    - 全局 auth_enabled=True：所有非白名单端点强制 Basic Auth；
    - 全局 auth_enabled=False（常用于信任的 LAN / 反代场景）：
      非敏感只读端点放行；写入 / 导出类敏感端点若已配置凭据，仍强制 Basic Auth
      作纵深防御；未配置凭据时维持原放行（由网络边界负责隔离），但导出已脱敏，
      密钥不会经此接口裸漏。
    """
    path = request.url.path

    # 白名单直接放行
    if path in _AUTH_WHITELIST or any(path.startswith(p) for p in _AUTH_WHITELIST_PREFIXES):
        return await call_next(request)

    config = _get_cfg()
    if config is None:
        # 配置无法加载时，出于安全默认“要求认证”而非放行
        return JSONResponse(status_code=401, content={"detail": "Config unavailable"})

    if not config.web.auth_enabled:
        # 关掉全局鉴权：敏感端点若已配置凭据则仍强制 Basic Auth（纵深防御）
        if _is_sensitive_request(request):
            has_creds = bool(config.web.auth_username) and bool(config.web.auth_password)
            if has_creds:
                auth_err = _require_basic_auth(request)
                if auth_err is not None:
                    return auth_err
        return await call_next(request)

    # 全局鉴权开启：所有非白名单端点强制 Basic Auth
    auth_err = _require_basic_auth(request)
    if auth_err is not None:
        return auth_err
    return await call_next(request)


# ============ Platform Context Middleware ============
@app.middleware("http")
async def platform_context_middleware(request: Request, call_next):
    """将 `?platform=` 查询参数写入请求级上下文，使所有 `get_store()` 调用自动落到
    对应平台库（多平台隔离）。

    - 合法且已配置的平台 id（取自幼 get_platforms()）→ 原样透传；
    - 缺失 / 非法 / 未配置 → 回退 "dingtalk"（不报错，旧链接与手写 URL 兼容）。
    该中间件只设置/复位 ContextVar，不拦截响应；须位于请求处理链内（auth 之后、
    endpoint 之前），故定义在 web_auth_middleware 之后。
    """
    raw = request.query_params.get("platform", "") or ""
    valid_ids = {p["id"] for p in get_platforms()}
    platform = raw if raw in valid_ids else "dingtalk"
    token = set_platform_context(platform)
    try:
        return await call_next(request)
    finally:
        _platform_ctx.reset(token)


# ============ Platform Discovery ============
@app.get("/api/platforms")
async def list_platforms():
    """返回所有已配置平台（供前端平台切换器 + 校验 `?platform=` 合法性）。

    字段：id / display_name / enabled / adapter_type。结果不含任何密钥。
    """
    return {"platforms": get_platforms()}


@app.get("/api/system/paths")
async def system_paths():
    """暴露运行时路径解析结果（供前端展示配置文件位置、用户数据目录等）。

    设计目的：冻结部署下配置文件落到 `~/Library/Application Support/linkora/...`，
    用户找不到时前端能直接显示具体路径 + 一键打开按钮（前端可后续接 `file://` 跳转）。
    """
    cfg = str(get_config_path())
    return {
        "is_frozen": is_frozen(),
        "user_data_dir": str(get_user_data_dir()),
        "data_dir": str(get_data_dir()),
        "config_path": cfg,
        "config_exists": os.path.exists(cfg),
        "log_dir": str(get_log_dir()),
        "skills_root": str(data_path("skills")),
        "pid_file": str(data_path("linkora.pid")),
    }


@app.get("/api/platforms/health")
async def platform_health():
    """返回所有平台的健康状态报告。

    对每个已启用的平台探测：适配器可用性、数据库连通性、CLI 就绪状态。
    结果含 per-platform 的 health / error 字段，供前端状态监控面板展示。
    """

    result = {"platforms": [], "overall": "healthy"}
    platforms = get_platforms()
    inst = get_app_instance()

    for p in platforms:
        status = {
            "id": p["id"],
            "display_name": p.get("display_name", p["id"]),
            "enabled": p.get("enabled", False),
            "health": "unknown",
            "errors": [],
        }
        if not p.get("enabled"):
            status["health"] = "disabled"
            result["platforms"].append(status)
            continue

        # 适配器健康：检查 platform context 是否已创建
        ctx = inst.platforms.get(p["id"]) if inst else None
        if ctx is None:
            status["health"] = "unhealthy"
            status["errors"].append("适配器未初始化")
        else:
            adapter = ctx.dws
            # CLI 就绪探测
            try:
                auth = adapter.auth_status() if hasattr(adapter, "auth_status") else {}
                status["adapter_auth"] = auth.get("authenticated", False)
            except Exception as e:
                logger.warning("CLI 探测失败: %s", e)
                status["errors"].append("CLI 探测失败")

            # 数据库连通性（通过 store.conn 触发懒连接即可验证）
            try:
                store = ctx.store
                _ = store.conn  # 触发连接创建 + schema 初始化
                status["db_connected"] = True
            except Exception as e:
                logger.warning("DB 连接失败: %s", e)
                status["db_connected"] = False
                status["errors"].append("DB 连接失败")

        # 汇总 health 结果
        if status["errors"]:
            status["health"] = "unhealthy"
            if result["overall"] == "healthy":
                result["overall"] = "degraded"
        else:
            status["health"] = "healthy"

        result["platforms"].append(status)

    return result


_CONFIG_COMMENT_HEADER = """# ============================================================
#  灵桥 (Linkora) — 企业 AI 智能连接平台 · 核心配置文件
#  修改后保存即可生效（Web 管理面板同步修改此文件）
# ============================================================
"""

# 每个配置段的行内注释（key -> comment）
_SECTION_COMMENTS: dict[str, str] = {
    "dws": "钉钉 DWS 适配器",
    "embedding": "Embedding 向量模型",
    "llm": "LLM 大语言模型",
    "logging": "日志",
    "memory": "记忆管理",
    "poller": "消息轮询器",
    "rag": "RAG 知识库分块",
    "rules": "规则引擎",
    "safety": "安全",
    "storage": "存储",
    "tools": "工具",
}


def _backup_config_before_write() -> None:
    """写入 config.yaml 前自动备份旧文件到 data/config-backups/。

    保留最近 30 份备份，超出时清理最旧的。备份含明文密钥，禁止 git 入库
    （.gitignore 已配置 data/config-backups/）。
    """
    src = Path(CONFIG_PATH)
    if not src.exists():
        return
    backup_dir = CONFIG_BACKUP_ROOT
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f"config_{ts}.yaml"
    try:
        import shutil
        shutil.copy2(str(src), str(dst))
    except OSError as e:
        logger.warning("配置备份失败: %s", e)
        return

    # 保留最近 30 份，删除更旧的
    try:
        all_backups = sorted(backup_dir.glob("config_*.yaml"))
        while len(all_backups) > 30:
            oldest = all_backups.pop(0)
            oldest.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("清理旧备份失败: %s", e)


def _write_config(config_dict: dict, changed_keys: set[str] | None = None) -> dict:
    """将配置写入 config.yaml，并附加注释头和各段注释。

    使用原子写入策略：先写入临时文件，再通过 os.replace() 原子替换目标文件，
    确保写入过程中断（断电/磁盘满）不会损坏原配置文件。

    返回 dict，包含 needs_restart 标识（当 changed_keys 含 model/chunk_size 等项时为 True）。
    """
    _backup_config_before_write()
    import yaml
    # 多平台隔离：root 级 poller 已迁移到各平台块，写回时剔除 root 键，
    # 避免重复/复活 root poller（平台块内的 poller 才是真源）。
    config_dict.pop("poller", None)
    raw = yaml.dump(config_dict, default_flow_style=False, allow_unicode=True)
    # 在每个顶级配置段前插入注释
    lines = raw.split("\n")
    out: list[str] = [_CONFIG_COMMENT_HEADER]
    for line in lines:
        # 检测顶级 key（非空、非缩进、以字母开头、以 : 结尾）
        if line and not line[0].isspace() and line.endswith(":"):
            key = line.rstrip(":")
            if key in _SECTION_COMMENTS:
                out.append(f"# ---------- {_SECTION_COMMENTS[key]} ----------")
        out.append(line)
    content = "\n".join(out)
    # 原子写入：先写临时文件，再替换目标文件
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CONFIG_PATH)

    # 审计：高危写操作留痕（best-effort，失败不拖垮主流程）
    try:
        from src.audit import audit
        changed = sorted(changed_keys) if changed_keys else []
        audit("config_write", "update_config", "success",
              actor="web", target=str(CONFIG_PATH),
              detail=f"changed_keys={changed}")
    except Exception as e:  # noqa: BLE001
        logger.warning("[audit] 配置写回审计失败(best-effort): %s", e)

    result: dict = {}
    # 需重启的配置项：model、chunk_size、embedding 模型等
    _RESTART_KEYS = {"llm", "embedding", "rag", "poller"}
    if changed_keys and any(k in _RESTART_KEYS for k in changed_keys):
        result["needs_restart"] = True
    return result


# ============ Request Models ============
# Pydantic 模型已提取到 web/schemas.py；以下仅为向后兼容的 re-export。
from web.schemas import (  # noqa: F401  # 供测试/子路由通过 web.api.ConfigUpdate 等方式访问
    AutoSyncUpdate,
    ConfigUpdate,
    DingTalkDocImportKb,
    DingTalkDocSync,
    KbDocumentCreate,
    KeywordBatchOp,
    KeywordMatchTest,
    KeywordUpdate,
    RagChatQuery,
    RagQuery,
    RuleKeyword,
    SystemPromptUpdate,
)


# ============ Utility ============


# EmbeddingClient 单例缓存：避免每个 KB 请求都重新加载 1.2GB 向量模型
# （旧实现每请求 new EmbeddingClient → 同步 SentenceTransformer 加载，数十秒级阻塞）。
# 按 (model, provider, offline) 维度缓存复用。
_embedding_clients: dict[tuple, "EmbeddingClient"] = {}
_embedding_clients_lock = threading.Lock()


def _get_embedding_client(embedding_config) -> "EmbeddingClient":
    """返回（或惰性创建并缓存）EmbeddingClient 单例。

    返回类型此前写作 `object`，导致所有调用点的 `.embed_with_retry` /
    `.embed_batch` 等成员访问全部退化为 unknown，类型检查形同虚设。
    """
    from src.memory.embedding import EmbeddingClient
    key = (
        embedding_config.model,
        getattr(embedding_config, "provider", "local"),
        getattr(embedding_config, "offline", False),
    )
    with _embedding_clients_lock:
        client = _embedding_clients.get(key)
        if client is None:
            client = EmbeddingClient(embedding_config)
            _embedding_clients[key] = client
    return client


# /api/stats/messages 计算结果缓存：
# 该端点做全表 GROUP BY + jieba 词频（较重），而仪表盘每 30s 拉一次，
# 故对按 days 维度的结果做 5 分钟 TTL 缓存，显著降低 DB/CPU 压力。
# 多平台隔离：缓存键必须含 platform，否则 A 平台的聚合结果会被 B 平台命中（数据串味）。
_stats_messages_cache: dict[tuple[int, str], tuple[float, dict]] = {}
_stats_messages_lock = threading.Lock()
_STATS_MESSAGES_TTL = 300.0  # 秒


def _get_cached_stats(days: int, platform: str | None = None) -> dict | None:
    from web.dependencies import get_current_platform
    key = (days, platform or get_current_platform())
    with _stats_messages_lock:
        cached = _stats_messages_cache.get(key)
        if cached and (time.time() - cached[0]) < _STATS_MESSAGES_TTL:
            return cached[1]
    return None


def _put_cached_stats(days: int, result: dict, platform: str | None = None) -> None:
    from web.dependencies import get_current_platform
    key = (days, platform or get_current_platform())
    with _stats_messages_lock:
        _stats_messages_cache[key] = (time.time(), result)


def get_dws() -> DwsAdapter:
    config = _require_cfg()
    return DwsAdapter(
        cli_path=config.dws.cli_path,
        dry_run=config.dws.dry_run,
        profile=config.dws.profile,
    )


# ============ Pages ============

def _auto_page_versions(v_func) -> dict[str, str]:
    """自动扫描 static 下多个前端目录，为每个文件生成版本变量：

    - js/pages/foo_bar.js        → foo_bar_js_v
    - js/components/foo_bar.js   → foo_bar_js_v   （共享组件层）
    - css/pages/foo_bar.css      → foo_bar_css_v
    - css/components/foo_bar.css → foo_bar_css_v  （共享组件样式）

    省去「每加一个文件都手动改 api.py」的维护负担。
    若目录不存在或读取失败，安全返回空 dict（模板使用默认值 1）。
    """
    result: dict[str, str] = {}
    scan_specs = [
        ("js/pages", ".js"),
        ("js/components", ".js"),
        ("js/services", ".js"),
        ("css/pages", ".css"),
        ("css/components", ".css"),
    ]
    for rel, ext in scan_specs:
        d = get_static_dir() / rel
        try:
            if d.is_dir():
                for f in sorted(os.listdir(str(d))):
                    if f.endswith(ext):
                        var_name = f[: -len(ext)].replace("-", "_") + ("_js_v" if ext == ".js" else "_css_v")
                        result[var_name] = v_func(rel + "/" + f)
        except OSError as e:
            logger.debug("扫描目录失败 %s: %s", rel, e)
    return result


def _read_bundle_manifest() -> dict:
    """读取前端构建产物 manifest（esbuild 合并后的单 bundle 文件名 + 内容哈希）。

    存在时返回 {bundle_css_v, bundle_js_v}（哈希文件名），模板据此加载单 bundle；
    缺失时返回空串，模板自动回退到逐文件加载（兼容未执行 build:frontend 的开发态）。
    """
    import json

    manifest = get_static_dir() / "dist" / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        return {
            "bundle_css_v": data.get("css", "") or "",
            "bundle_js_v": data.get("js", "") or "",
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bundle_css_v": "", "bundle_js_v": ""}


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = get_templates_dir() / "index.html"
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(autoescape=select_autoescape(["html"]))
        env.loader = FileSystemLoader(str(html_path.parent))
        tpl = env.get_template(html_path.name)
        def v(name: str) -> str:
            p = get_static_dir() / name
            try:
                return str(int(p.stat().st_mtime))
            except FileNotFoundError:
                return "1"
        return tpl.render(
            style_v=v("css/style.css"),
            **_read_bundle_manifest(),
            theme_v=v("css/theme.css"),
            motion_v=v("css/motion.css"),
            override_v=v("css/bootstrap-override.css"),
            icons_css_v=v("css/icons.css"),
            icons_svg_v=v("icons/icons.svg"),
            icons_js_v=v("js/icons.js"),
            app_js_v=v("js/app.js"),
            # 每个 core 脚本使用自身 mtime 版本号（不能共用 app_js_v，否则改 core/app.js 后浏览器仍用旧缓存）
            core_api_js_v=v("js/core/api.js"),
            core_store_js_v=v("js/core/store.js"),
            core_util_js_v=v("js/core/util.js"),
            core_app_js_v=v("js/core/app.js"),
            core_onboarding_js_v=v("js/core/onboarding.js"),
            theme_js_v=v("js/theme.js"),
            # 每个 CSS 文件使用自身 mtime 作为版本号，避免共用 theme_v 导致某文件修改后缓存不破
            variables_v=v("css/base/variables.css"),
            reset_v=v("css/base/reset.css"),
            utilities_v=v("css/base/utilities.css"),
            app_shell_v=v("css/layout/app-shell.css"),
            dashboard_v=v("css/layout/dashboard.css"),
            panel_v=v("css/components/panel.css"),
            table_v=v("css/components/table.css"),
            toast_v=v("css/components/toast.css"),
            button_v=v("css/components/button.css"),
            form_v=v("css/components/form.css"),
            dashboard_page_v=v("css/pages/dashboard.css"),
            messages_page_v=v("css/pages/messages.css"),
            rag_page_v=v("css/pages/rag.css"),
            rules_page_v=v("css/pages/rules.css"),
            settings_page_v=v("css/pages/settings.css"),
            keywords_page_v=v("css/pages/keywords.css"),
            # 自动扫描 static/js/pages/ 与 static/css/pages/ 目录生成版本号变量，无需手动注册
            #   - js/pages/foo_bar.js     → {{ foo_bar_js_v }}
            #   - css/pages/persona.css  → {{ persona_css_v }}
            # 文件名 foo_bar.js → {{ foo_bar_js_v }}
            **_auto_page_versions(v),
        )
    except ImportError as e:
        logger.debug("Jinja2 模板渲染失败，使用原始 HTML: %s", e)
        return html_path.read_text(encoding="utf-8")


# ============ Status & Stats ============


# ---- 外部好友映射 API（非组织内成员） ----
# ExternalFriendCreate 已提取到 web/schemas.py；路由内定义保留在 web/routers/external_friends.py。


# ============ Runner ============

# ============ Runner ============

def run_web(port: int = 8000, host: str | None = None):
    import uvicorn
    import logging

    # 启动钩子：配置文件每日滚动备份（当天已备份 / 无变化则跳过，详见 src/config_backup.py）。
    # 与 lifecycle.main 共用同一逻辑；去重保证每天至多一份，备份失败绝不中断启动。
    try:
        from src.config_backup import maybe_backup

        maybe_backup()
    except Exception as _e:  # noqa: BLE001
        logger.warning("[config-backup] 启动备份失败（已忽略）：%s", _e)

    # 安全默认：仅监听本机回环。若需从其他设备访问，应经反代并在其上加认证，
    # 或显式传 host="0.0.0.0"（不推荐公网直曝）。优先级：显式参数 > 环境变量 > config.yaml。
    if host is None:
        host = os.environ.get("WEB_HOST")
    if host is None:
        try:
            from src.shared_state import get_config
            host = get_config().web.host
        except Exception:
            host = "127.0.0.1"

    # 安全告警：绑定 0.0.0.0 且未开启认证时，管理后台可能公网裸奔（不阻断启动）。
    if host == "0.0.0.0":
        _auth_on = True
        try:
            from src.shared_state import get_config
            _auth_on = get_config().web.auth_enabled
        except Exception:
            _auth_on = True
        if not _auth_on:
            logger.warning(
                "Web 绑定 0.0.0.0 且未开启认证，管理后台可能公网裸奔，请经反向代理并开启 auth"
            )

    # 将静态资源请求的日志级别降低为 DEBUG，减少控制台噪音
    class AccessLogFilter(logging.Filter):
        """过滤掉静态资源的 INFO 级别访问日志，只保留非静态资源的请求。"""
        def filter(self, record):
            msg = record.getMessage()
            # 如果是 GET 请求且路径包含 /static/，则降级为 DEBUG
            if 'GET' in msg and '/static/' in msg:
                record.levelno = logging.DEBUG
                record.levelname = 'DEBUG'
            return True

    # 获取 uvicorn.access logger 并添加过滤器
    access_logger = logging.getLogger('uvicorn.access')
    access_logger.addFilter(AccessLogFilter())

    # 设置 uvicorn 日志级别为 WARNING，只显示警告和错误
    # 这样静态资源的 DEBUG 日志和非 API 请求的 INFO 日志都不会显示
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_web()


# ============ 子路由挂载（P2-11 模块拆分）============
# 必须在模块末尾挂载：生产由 main.py 以模块方式整文件导入 web.api，
# 此处所有定义已就绪；各子路由通过 `import web.api as _api`（惰性代理）复用共享符号
# （get_store/get_dws/get_app_instance/_get_cfg 等）与 pydantic 模型。
# 注意：子路由不要在本模块「被先导入」时顶层 import web.api——那样会与本段的
# `from web.routers.X import router` 形成循环导入。config 路由已改为惰性代理规避；
# 其余路由若也需直接 import web.api，请同样改为惰性导入。
# 直接 `python web/api.py` 运行因 __main__ 守卫较早（非生产入口），不影响 main.py 加载。
from web.routers.dead_letters import router as _dead_letters_router
from web.routers.drafts import router as _drafts_router
from web.routers.departments import router as _departments_router
from web.routers.external_friends import router as _external_friends_router
from web.routers.health import router as _health_router
from web.routers.orgs import router as _orgs_router
from web.routers.skills_marketplace import router as _skills_marketplace_router
from web.routers.memories import router as _memories_router
from web.routers.rules import router as _rules_router
from web.routers.intents import router as _intents_router
from web.routers.decisions import router as _decisions_router
from web.routers.routing_quality import router as _routing_quality_router
from web.routers.metrics import router as _metrics_router
from web.routers.logs import router as _logs_router
from web.routers.sync import router as _sync_router
from web.routers.config import router as _config_router
from web.routers.stats import router as _stats_router
from web.routers.conversations import router as _conversations_router
from web.routers.keywords import router as _keywords_router
from web.routers.dingtalk_docs import router as _dingtalk_docs_router
from web.routers.kb import router as _kb_router
from web.routers.skills import router as _skills_router
from web.routers.status import router as _status_router
from web.routers.image import router as _image_router
from web.routers.tools import router as _tools_router
from web.routers.feedback import router as _feedback_router
from web.routers.persona import router as _persona_router
from web.routers.simulate import router as _simulate_router
from web.routers.cost_quality import router as _cost_quality_router
from web.routers.dashboard_live import router as _dashboard_live_router

app.include_router(_dead_letters_router)
app.include_router(_drafts_router)
app.include_router(_departments_router)
app.include_router(_external_friends_router)
app.include_router(_health_router)
app.include_router(_orgs_router)
app.include_router(_skills_marketplace_router)
app.include_router(_memories_router)
app.include_router(_rules_router)
app.include_router(_intents_router)
app.include_router(_decisions_router)
app.include_router(_routing_quality_router)
app.include_router(_metrics_router)
app.include_router(_logs_router)
app.include_router(_sync_router)
app.include_router(_config_router)
app.include_router(_stats_router)
app.include_router(_conversations_router)
app.include_router(_keywords_router)
app.include_router(_dingtalk_docs_router)
app.include_router(_kb_router)
app.include_router(_skills_router)
app.include_router(_status_router)
app.include_router(_image_router)
app.include_router(_tools_router)
app.include_router(_feedback_router)
app.include_router(_persona_router)
app.include_router(_simulate_router)
app.include_router(_cost_quality_router)
app.include_router(_dashboard_live_router)

