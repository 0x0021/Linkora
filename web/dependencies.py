"""Web 层共享依赖（资源访问器 / 客户端单例 / 辅助函数）。

从 `web/api.py` 抽离的“神模块”公共部分，供 `web/api.py` 与各 `web/routers/*`
子路由**共同导入**。本模块刻意不反向依赖 `web.api`，以彻底打破
`api.py → 子路由 → api.py` 的循环导入。

包含：
- DB：get_store（per-thread 缓存，P1-2）
- SkillHub 市场：_get_project_root / _ensure_skillhub_cli /
  _fetch_market_rankings / _normalize_market_skill
- 应用实例与日志：get_app_instance（再导出）、logger
"""
from __future__ import annotations

import json as _json
from urllib.parse import quote
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import asyncio
import urllib.parse
import urllib.request
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Query
from src.config import load_config, DEFAULT_STORAGE_PATH
from src.paths import data_path
from src.memory.sqlite_store import SQLiteStore
# 平台上下文唯一真源：Web 层不再自建第二套 ContextVar，直接复用 src 端主变量，
# 使「Web 请求选定的平台」与「仓储会话库路由」天然一致（详见下方 _platform_ctx 注释）。
from src.memory.platform_context import current_platform_var as _current_platform_var
from src.shared_state import get_app_instance
# 纵深 SSRF 防护：下载边界统一经 is_ssrf_safe 校验，与调用点主机白名单+SHA256
# 钉值形成双重保险。web.security 仅依赖标准库，此处导入无循环依赖风险。
from web.security import is_ssrf_safe

logger = logging.getLogger("web.api")

# ── F17：同步阻塞调用异步化 ──────────────────────────────────────────────
# 把 SQLiteStore 等同步 DB / 计算调用放进线程池，避免阻塞 asyncio 事件循环。
# 安全依据：SQLiteStore 为 per-thread 连接架构（见 get_store），store 传入 worker 线程后，
# 该线程会自建自身连接，不存在跨线程连接竞态；get_store() 内部的向量索引陈旧校验
# （count_embedded_chunks）也是同步 DB 调用，因此连同 store 一起放进线程池最稳。
async def run_sync(func, *args, **kwargs):
    """将同步阻塞函数放到线程池执行，返回其返回值（await 形式）。

    用法：``result = await run_sync(store.method, arg1, arg2)`` 或
    把整段同步逻辑包进 ``def _work(): ... return await run_sync(_work)``。
    """
    return await asyncio.to_thread(func, *args, **kwargs)

# 多平台隔离：请求级平台上下文。由 web.api 的平台中间件从 `?platform=` 查询参数写入，
# get_store() 在无显式 platform 参数时读取此上下文，从而让所有现有 `get_store()` /
# `_api.get_store()` 调用自动落到对应平台库，无需逐个改 endpoint 签名。
#
# 【上下文收敛】此处原先自建「第二套」平台 ContextVar（default "dingtalk"），与
# src.memory.platform_context.current_platform_var 各自为政：Web 请求把平台写进自己
# 那套，src 端仓储（message_repo 等）读的却是另一套，导致 `?platform=feishu` 的请求里
# 仓储仍按钉钉命名空间路由（串图）。现直接复用唯一真源变量，Web 侧只在**读取**时保留
# `or "dingtalk"` 的向后兼容回退（旧链接、非请求上下文的后台任务调用）。
_platform_ctx: ContextVar[str] = _current_platform_var


def set_platform_context(platform: str) -> Token[str]:
    """设置请求级平台上下文，返回 token 供 finally 复位。"""
    return _platform_ctx.set(platform or "dingtalk")


def get_current_platform() -> str:
    """读取当前请求的平台（无上下文时回退 dingtalk）。"""
    return _platform_ctx.get() or "dingtalk"

# 多平台隔离：每个平台一个独立 SQLite DB 文件（路径由 config.platforms[].storage.path 决定）。
DEFAULT_DB_PATH = DEFAULT_STORAGE_PATH

# 连接/实例缓存（P1-2）：SQLiteStore 默认每请求 new + init_db +（KB 搜索时）从 DB 重建
# faiss 向量索引，开销显著。改为 per-thread 缓存同一实例：
# - 每个 worker 线程只构造一次 Store、只跑一次 init_db，消除每请求重复开销；
# - 线程隔离，各线程 store._conns 只含自身连接，endpoint 末尾的 store.close() 仅关闭
#   本线程连接，不存在跨线程连接竞态（与 SQLiteStore 的 per-thread 连接架构一致）；
# - 向量索引在单线程内复用，避免每次请求重建；并用廉价 COUNT 校验 DB 变化使其失效，
#   保证 KB 搜索结果不会因缓存而陈旧。
# 多平台：同一线程内按 db_path 缓存多个 Store 实例（每平台一个）。
_store_local = threading.local()


def _resolve_platform_path(platform: str) -> str:
    """解析平台对应的数据库路径（优先运行期共享配置，支持热重载；否则回退 load_config）。

    非法/未知 platform → 回退 dingtalk 默认路径，保证旧链接与错误参数不崩。
    """
    if not platform or platform == "dingtalk":
        # 快速路径：默认平台直接回退默认路径，避免无谓配置查询
        return DEFAULT_DB_PATH
    cfg = None
    try:
        inst = get_app_instance()
        if inst is not None:
            cfg = inst.config
    except Exception:
        cfg = None
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    if cfg is not None:
        for p in cfg.platforms:
            if p.id == platform:
                return p.storage.path
    # 未知平台：按 id 派生兜底路径（仍隔离），不报错（可重定位）
    return str(data_path(f"{platform}-ai.db"))


def get_store(platform: str | None = None) -> SQLiteStore:
    if not platform:
        # 请求上下文未显式传 platform 时，读取中间件设置的平台上下文（缺省 dingtalk）
        platform = get_current_platform()
    db_path = _resolve_platform_path(platform)
    stores = getattr(_store_local, "stores", None)
    if stores is None:
        stores = {}
        _store_local.stores = stores
    # DB_PATH 在测试中被 monkeypatch 切换时必须重建，否则会复用上一测试的错误库路径
    # 导致测试串味；同时检查缓存实例是否已被 close()（endpoint 末尾会调用 close）。
    cached = stores.get(db_path)
    if cached is None or getattr(cached, "_closed", False):
        from src.memory.store_factory import get_store as _factory_get_store
        cached = _factory_get_store(db_path)
        cached.init_db()
        stores[db_path] = cached
    store = cached
    # 向量索引随 DB 变化失效：仅当已加载时才做廉价 COUNT 校验
    vi = store._vector_index
    if vi is not None:
        try:
            if store._kb_repo.count_embedded_chunks() != getattr(vi, "count", -1):
                store._vector_index = None  # 下次使用自动从 DB 重建
        except Exception as e:  # noqa: BLE001
            # 降级而非上抛：这只是「向量索引是否陈旧」的廉价校验，失败不应让整个请求
            # 500（沿用已加载的索引即可）。但必须留痕——静默 pass 会把「DB 损坏 / 表缺失
            # / 连接失效」等真实故障藏成「KB 搜索结果莫名陈旧」，线上极难排查。
            logger.warning(
                "向量索引陈旧校验失败（db=%s），沿用已加载索引（结果可能陈旧）: %s",
                db_path, e,
            )
    return store


def get_platforms() -> list[dict]:
    """返回所有配置平台（供前端切换器与 /api/platforms）。

    id / display_name / enabled / adapter_type。优先运行期共享配置（支持热重载）。
    """
    cfg = None
    try:
        inst = get_app_instance()
        if inst is not None:
            cfg = inst.config
    except Exception:
        cfg = None
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    if cfg is None:
        return []
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "enabled": p.enabled,
            "adapter_type": p.adapter_type,
        }
        for p in cfg.platforms
    ]


def get_rag_config() -> dict:
    """获取当前平台的 KB（RAG）配置，支持平台级 chunk_size/chunk_overlap 覆盖。

    如果当前平台的 PlatformConfig.rag 中设置了覆盖值则使用，否则回退到全局 rag 段。
    返回 dict 包含 chunk_size / chunk_overlap / embedding_model 字段。
    """
    platform = get_current_platform()
    cfg = None
    try:
        inst = get_app_instance()
        if inst is not None:
            cfg = inst.config
    except Exception:
        cfg = None
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:
            cfg = None
    result = {
        "chunk_size": cfg.rag.chunk_size if cfg else 800,
        "chunk_overlap": cfg.rag.chunk_overlap if cfg else 50,
        "embedding_model": cfg.embedding.model if cfg else "text-embedding-3-small",
    }
    if cfg is not None and platform:
        for p in cfg.platforms:
            if p.id == platform and p.rag is not None:
                if p.rag.chunk_size is not None:
                    result["chunk_size"] = p.rag.chunk_size
                if p.rag.chunk_overlap is not None:
                    result["chunk_overlap"] = p.rag.chunk_overlap
                if p.rag.embedding_model is not None:
                    result["embedding_model"] = p.rag.embedding_model
                break
    return result


def get_store_dep(platform: str | None = Query(default=None)):
    """FastAPI 依赖注入版本：请求结束自动关闭 store，避免异常路径泄漏。

    platform 缺省时读取请求上下文（由 web.api 平台中间件设置），保证与内联
    `get_store()` 行为一致。
    """
    store = get_store(platform)
    try:
        yield store
    finally:
        try:
            store.close()
        except Exception as e:  # noqa: BLE001
            # 不上抛：close 失败发生在响应已生成之后，此时抛异常只会把一个成功的
            # 请求变成 500，且掩盖真正的业务结果。但必须留痕——静默 pass 会让「连接
            # 泄漏 / DB 文件被删」这类会逐步拖垮进程的问题完全不可见。
            logger.warning("请求结束关闭 store 失败（连接可能泄漏）: %s", e)


def _get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent

# ── SkillHub CLI 安装源加固（F12，选项 B）──────────────────────────────
# 安装源 URL 钉死为白名单常量，不可经请求/配置改写，杜绝供应链 RCE / URL 注入。
SKILLHUB_INSTALL_URL = "https://skillhub-1388575217.cos.ap-guangzhou.myqcloud.com/install/install.sh"
# 安装脚本 SHA256 钉值：部署方必须将生产使用的 install.sh 哈希钉入此处
# （或经环境变量 SKILLHUB_INSTALL_SHA256 注入）。为空则 fail-closed 拒绝自动安装。
SKILLHUB_INSTALL_SHA256 = os.environ.get("SKILLHUB_INSTALL_SHA256", "")


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _skillhub_install_url_allowed(url: str) -> bool:
    """仅允许预置白名单主机，防止未来重构时把可变 URL 传入执行。"""
    allowed_hosts = {"skillhub-1388575217.cos.ap-guangzhou.myqcloud.com"}
    try:
        host = urllib.parse.urlparse(url).netloc
    except Exception:  # noqa: BLE001
        return False
    return host in allowed_hosts


def _download_to_file(url: str, dest: Path, timeout: float = 60.0) -> None:
    # 边界纵深校验：即便调用方已做主机白名单校验，下载边界仍统一过 is_ssrf_safe，
    # 防止未来重构把可变 URL 传入时绕过调用点网关（fail-closed：非法即拒）。
    if not is_ssrf_safe(url):
        raise ValueError(f"下载源 URL 未通过 SSRF 校验，已拒绝: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "dingtalk-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (host 在白名单内)
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)


def _ensure_skillhub_cli() -> tuple[bool, str]:
    """确保 skillhub CLI 已安装，返回 (是否可用, 错误信息)。

    安全策略（F12 加固，选项 B）：
    - 优先使用 PATH 中已安装的 skillhub；
    - 仅当配置 skills.skillhub.auto_install=true 才尝试自动安装，默认关闭——
      普通 API 请求绝不触发远端代码拉取；
    - 自动安装时先把脚本下载到临时文件，SHA256 校验通过后才以非 shell 方式执行；
    - 远程 URL 走白名单常量，不可被请求篡改；
    - 全程 fail-closed：缺少钉值或未授权则明确拒绝，绝不退化为 curl|bash。
    """
    if shutil.which("skillhub"):
        return True, ""

    # 懒加载配置单例（避免与 web.api 循环导入）
    cfg = None
    try:
        from web.api import _get_cfg
        cfg = _get_cfg()
    except Exception:  # noqa: BLE001
        cfg = None
    auto_install = bool(
        getattr(cfg, "skillhub", None) and getattr(cfg.skillhub, "auto_install", False)
    ) if cfg is not None else False

    if not auto_install:
        return False, (
            "skillhub CLI 未安装且未启用自动安装（skills.skillhub.auto_install=false）。"
            "请通过部署脚本/Dockerfile 预装 skillhub CLI 后重启服务。"
        )

    url = SKILLHUB_INSTALL_URL
    if not _skillhub_install_url_allowed(url):
        return False, f"skillhub 安装源不在白名单，已拒绝执行: {url}"

    if not SKILLHUB_INSTALL_SHA256:
        return False, (
            "未配置 skillhub 安装脚本 SHA256 钉值（SKILLHUB_INSTALL_SHA256 为空），"
            "为安全起见自动安装已拒绝，请部署方钉死哈希后再启用 auto_install。"
        )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as tf:
            tmp_path = Path(tf.name)
        _download_to_file(url, tmp_path)
        if _sha256_of_file(tmp_path) != SKILLHUB_INSTALL_SHA256:
            return False, "skillhub 安装脚本 SHA256 校验失败，已拒绝执行"
        # 非 shell 执行（去 shell=True 反模式，杜绝 shell 注入）
        subprocess.run(
            ["bash", str(tmp_path), "--cli-only"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "PATH": f"{os.environ.get('HOME', '/root')}/.local/bin:{os.environ.get('PATH', '')}"},
        )
    except subprocess.TimeoutExpired:
        return False, "skillhub CLI 安装超时，请手动执行安装命令"
    except Exception as e:  # noqa: BLE001
        return False, f"skillhub CLI 自动安装异常: {e}"
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001
                logger.debug("unlink skill script failed: %s", e)

    if shutil.which("skillhub"):
        return True, ""
    for p in (Path.home() / ".local" / "bin" / "skillhub", Path.home() / "bin" / "skillhub"):
        if p.exists():
            return True, ""
    return False, "skillhub CLI 安装后未找到可执行文件，请手动检查安装日志"


# 服务端缓存：避免每次切标签都调用 CLI（网络请求较慢）
_MARKET_RANKINGS_CACHE: dict = {"ts": 0.0, "data": None, "ttl": 180.0}
# 榜单缓存单飞锁（F13）：避免并发请求同时触发 CLI / 自动安装（抢装 ~/.local/bin），
# 并消除模块级缓存 dict 在 async 下的并发读写竞态。持锁跨越 to_thread 不会阻塞事件循环。
_MARKET_RANKINGS_LOCK = asyncio.Lock()

# slug → 原始图标 URL 映射：在 _fetch_market_rankings 时填充，供安装时下载图标用
_ICON_URL_MAP: dict[str, str] = {}

# 后台任务强引用池：事件循环只持弱引用，裸 create_task 的任务可能在完成前被 GC，
# 导致图标预取静默中断。持有引用并在完成后自动移除。
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    """派发后台任务并持有强引用，完成后自动移除。"""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


# ── 技能图标本地缓存检查 ────────────────────────────────────────────
def _slug_to_safe_name(slug: str) -> str:
    """将 skill slug 转为安全文件名：去 @、/→-、保留字母数字-_."""
    s = slug.lstrip("@").replace("/", "-")
    return "".join(c for c in s if c.isalnum() or c in "_-") or "default"


def _get_raw_icon_url(slug: str) -> str | None:
    """取 slug 对应的原始图标 URL（从排行榜缓存中查）。

    支持原始 slug 和 safe_name 两种查询方式，后者由 _fetch_market_rankings
    在填充 _ICON_URL_MAP 时同步建立反向映射。
    """
    return _ICON_URL_MAP.get(slug)


def _proxy_icon_url(raw_url: str, slug: str = "") -> str:
    """将外链图标 URL 改写为同源代理路径（优先本地 serve）。

    - slug 非空 → /api/skill-icons/<safe_slug>
      （serve_skill_icon 会处理：有本地 PNG 则返回，否则异步下载 + SVG 兜底）
    - 空值原样返回（前端走 fa-cube 兜底）；
    - 相对路径（/xxx）原样返回；
    - http(s):// 外链（无 slug）→ /api/proxy/image?url=<urlencoded>
    """
    if not raw_url:
        return ""
    if slug:
        safe = _slug_to_safe_name(slug)
        return f"/api/skill-icons/{safe}"
    if raw_url.startswith(("http://", "https://")):
        return f"/api/proxy/image?url={quote(raw_url, safe='')}"
    return raw_url


async def _trigger_icon_prefetch(icon_url_map: dict[str, str]) -> None:
    """后台触发批量图标预取（从 web.routers.image 中加载并执行）。"""
    try:
        from web.routers.image import prefetch_all_skill_icons
        await prefetch_all_skill_icons(icon_url_map)
    except Exception:
        pass


def _normalize_market_skill(item: dict) -> dict:
    """将 skillhub rankings 返回的单个 skill 归一化为前端友好的结构。"""
    labels = item.get("labels") or {}
    requires_api_key = str(labels.get("requires_api_key", "false")).lower() == "true"
    subs = item.get("subCategories") or []
    sub_names = [s.get("name") for s in subs if isinstance(s, dict) and s.get("name")]
    slug = str(item.get("slug") or item.get("name") or "")
    return {
        "slug": slug,
        "name": str(item.get("name") or item.get("slug") or ""),
        "author": str(item.get("ownerName") or item.get("source") or ""),
        "description": str(item.get("description_zh") or item.get("description") or ""),
        "description_en": str(item.get("description") or ""),
        "category": str(item.get("category") or ""),
        "subCategories": sub_names,
        "tags": item.get("tags") or [],
        "downloads": int(item.get("downloads") or 0),
        "stars": int(item.get("stars") or 0),
        "installs": int(item.get("installs") or 0),
        "score": float(item.get("score") or 0),
        "version": str(item.get("version") or ""),
        "verified": bool(item.get("verified") or False),
        # 优先本地缓存图标（彻底摆脱外部 CDN 认证/签名过期/不可达），
        # 未缓存则走 /api/proxy/image 同源代理；内部相对路径原样返回。
        "iconUrl": _proxy_icon_url(item.get("iconUrl") or "", slug=slug),
        "homepage": str(item.get("homepage") or ""),
        "source": str(item.get("source") or ""),
        "created_at": int(item.get("created_at") or 0),
        "updated_at": int(item.get("updated_at") or 0),
        "requires_api_key": requires_api_key,
    }


async def _fetch_market_rankings(force: bool = False) -> dict:
    """调用 `skillhub skill rankings --type all` 获取全量榜单，构建 6 类排序 section。

    返回结构: { ok, updated_at, total, sections:{all,featured,trending,hot,newest,stars}, stale }
    带服务端缓存（TTL），CLI 不可用时降级返回上次缓存（stale=true）。
    """
    async with _MARKET_RANKINGS_LOCK:
        now = time.time()
        cache = _MARKET_RANKINGS_CACHE
        if not force and cache["data"] is not None and (now - cache["ts"]) < cache["ttl"]:
            return {**cache["data"], "stale": False}

        # 在独立线程执行（可能触发安装子进程，最长 120s），避免阻塞 asyncio 事件循环
        ok, err = await asyncio.to_thread(_ensure_skillhub_cli)
        if not ok:
            if cache["data"] is not None:
                return {**cache["data"], "stale": True}
            raise HTTPException(status_code=500, detail=f"skillhub CLI 不可用: {err}")

        local_bin = str(Path.home() / ".local" / "bin")
        env = os.environ.copy()
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        cmd = ["skillhub", "--skip-self-upgrade", "skill", "rankings", "--type", "all", "--timeout", "30"]
        try:
            # 在独立线程执行阻塞子进程，避免阻塞 asyncio 事件循环（F14）
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=45, env=env
            )
        except subprocess.TimeoutExpired:
            if cache["data"] is not None:
                return {**cache["data"], "stale": True}
            raise HTTPException(status_code=504, detail="SkillHub 榜单获取超时") from None

        if proc.returncode != 0:
            if cache["data"] is not None:
                return {**cache["data"], "stale": True}
            raise HTTPException(status_code=500, detail=f"SkillHub 榜单获取失败: {proc.stderr.strip()[:200]}")

        stdout = proc.stdout.strip()
        if not stdout:
            if cache["data"] is not None:
                return {**cache["data"], "stale": True}
            raise HTTPException(status_code=500, detail="SkillHub 返回空数据")

        try:
            raw = _json.loads(stdout)
        except _json.JSONDecodeError:
            if cache["data"] is not None:
                return {**cache["data"], "stale": True}
            raise HTTPException(status_code=500, detail="SkillHub 返回格式异常，无法解析") from None

        rankings = raw.get("rankings", {}) if isinstance(raw, dict) else {}

        def _sec(name: str) -> list:
            v = rankings.get(name)
            return v.get("skills", []) if isinstance(v, dict) else []

        def _norm(items: list) -> list:
            return [_normalize_market_skill(it) for it in items]

        # 全量技能宇宙（去重，按 slug），用于「全部」与客户端按收藏量排序
        universe_map: dict = {}
        for name in ("featured", "trending", "hot", "newest", "recommended"):
            for it in _sec(name):
                sl = it.get("slug") or it.get("name")
                if sl and sl not in universe_map:
                    universe_map[sl] = it
        universe = list(universe_map.values())

        # 构建 slug → 原始 iconUrl 映射，供安装后下载图标到本地缓存
        global _ICON_URL_MAP
        _ICON_URL_MAP = {
            sl: it.get("iconUrl") or ""
            for sl, it in universe_map.items()
            if it.get("iconUrl")
        }
        # 同时建立 safe_name → raw_url 反向映射，使 serve_skill_icon
        # 在收到 safe_name 请求（如 /api/skill-icons/contract-review）时
        # 也能反查到原始 iconUrl 并触发懒下载
        for sl, url in list(_ICON_URL_MAP.items()):
            safe = _slug_to_safe_name(sl)
            if safe != sl and safe not in _ICON_URL_MAP:
                _ICON_URL_MAP[safe] = url

        # 主动批量预取所有图标到本地缓存：排行榜数据加载后立即在后台并发下载
        # 全部技能图标，确保用户首次打开市场页面时图标已就绪，消灭"先看 SVG
        # 兜底、刷新才出真图标"的半成品体验。
        _icon_map_snapshot = dict(_ICON_URL_MAP)
        _spawn_bg(_trigger_icon_prefetch(_icon_map_snapshot))

        all_sorted = sorted(universe, key=lambda x: float(x.get("score") or 0), reverse=True)
        stars_sorted = sorted(universe, key=lambda x: int(x.get("stars") or 0), reverse=True)

        data = {
            "ok": True,
            # datetime.utcnow() 在 3.12+ 已弃用且计划移除，改用时区感知的 UTC 取值；
            # 输出格式保持 "...Z" 不变，前端无需改动。
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "total": len(all_sorted),
            "sections": {
                "all": _norm(all_sorted),
                "featured": _norm(_sec("featured")),
                "trending": _norm(_sec("trending")),
                "hot": _norm(_sec("hot")),
                "newest": _norm(_sec("newest")),
                "stars": _norm(stars_sorted),
            },
        }
        cache["data"] = data
        cache["ts"] = time.time()
        return {**data, "stale": False}
