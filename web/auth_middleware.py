"""Web API 认证中间件。

提供 JWT 令牌认证和基于角色的访问控制 (RBAC)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from typing import Any

import jwt as _pyjwt
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# JWT 配置（基于 pyjwt 的 HS256 标准实现）
# 安全：源码中不存在任何硬编码密钥。TokenManager 默认用哨兵标记 __NOT_SET__，
# 运行时由 _resolve_jwt_secret() 优先读取 config.web.jwt_secret，否则生成本进程唯一的
# 随机密钥（重启失效），从而让任何用旧占位值伪造的令牌在校验时失效。
_DEFAULT_SECRET_SENTINEL = object()  # 仅用于区分「未显式设置密钥」与「显式密钥」
_TOKEN_EXPIRE_SECONDS = 3600 * 24  # 24 小时

# 运行期解析出的 JWT 签名密钥（进程内缓存，保证签发与校验使用同一密钥）。
_runtime_jwt_secret: str | None = None

# 登出令牌黑名单（内存集合，存令牌的 SHA-256）。verify_token 校验时拒绝命中者。
# 说明：单进程内有效；多 worker 部署时各 worker 独立（登出仅本 worker 生效），
# 对本地管理工具可接受（令牌本身 24h 过期，重启进程也会清空）。如需跨 worker 一致，
# 需外接共享存储（如 Redis），当前不引入以控制复杂度。
_revoked_token_hashes: set[str] = set()
_revoked_lock = threading.Lock()


def _hash_token(token: str) -> str:
    """令牌不可逆哈希，用于黑名单存储（不落明文令牌）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _resolve_jwt_secret() -> str:
    """解析 JWT 签名密钥。

    - 若 config.web.jwt_secret 已设置，使用它（推荐，跨重启稳定）；
    - 否则生成本进程唯一的随机密钥并告警（重启后旧令牌失效，但不再是公开硬编码值）。
    """
    global _runtime_jwt_secret
    if _runtime_jwt_secret is not None:
        return _runtime_jwt_secret
    try:
        from src.shared_state import get_config

        cfg = get_config()
        if cfg is not None:
            secret = (getattr(cfg.web, "jwt_secret", "") or "").strip()
            if secret:
                _runtime_jwt_secret = secret
                return secret
    except Exception as _e:
        logger.debug("解析 JWT 签名密钥失败，回退到临时随机密钥: %s", _e)
    _runtime_jwt_secret = secrets.token_urlsafe(32)
    logger.warning(
        "JWT 签名密钥未配置（web.jwt_secret 为空），已生成本进程临时随机密钥，"
        "重启后已签发令牌将失效；生产环境请在 config.yaml 的 web.jwt_secret 设置固定高熵密钥"
    )
    return _runtime_jwt_secret

# 角色定义
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

VALID_ROLES = {ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER}


class TokenManager:
    """简单的令牌管理器。"""

    def __init__(self, secret_key: str = _DEFAULT_SECRET_SENTINEL):  # type: ignore[assignment]
        self.secret_key = secret_key

    def _secret(self) -> str:
        """取得实际签名密钥：显式设置过则用显式值，否则走运行期解析。"""
        if self.secret_key is not _DEFAULT_SECRET_SENTINEL:
            return self.secret_key
        return _resolve_jwt_secret()

    def generate_token(self, username: str, role: str = ROLE_VIEWER) -> str:
        """生成 JWT 令牌（HS256，与 pyjwt 标准格式兼容）。

        等价于 ``jwt.encode``：header 固定 ``{"alg":"HS256","typ":"JWT"}``，
        payload 含 sub/role/iat/exp。保留手工签发语义以便旧令牌可被标准库继续校验。
        """
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role}")

        now = int(time.time())
        payload = {
            "sub": username,
            "role": role,
            "iat": now,
            "exp": now + _TOKEN_EXPIRE_SECONDS,
        }
        # pyjwt>=2 返回 str；headers 显式锁定，确保与历史令牌 header 字节一致
        return _pyjwt.encode(
            payload, self._secret(), algorithm="HS256",
            headers={"alg": "HS256", "typ": "JWT"},
        )

    def verify_token(self, token: str) -> dict[str, Any]:
        """验证令牌并返回 payload；非法/过期/已吊销统一抛 401。"""
        try:
            payload = _pyjwt.decode(token, self._secret(), algorithms=["HS256"])
        except _pyjwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except _pyjwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail=f"Token verification failed: {exc}") from exc
        if _hash_token(token) in _revoked_token_hashes:
            raise HTTPException(status_code=401, detail="Token revoked")
        return payload

    def _sign(self, data: str) -> str:
        """HMAC-SHA256 签名（保留给历史单测手工构造 token 用）。

        与 ``generate_token`` 走同一密钥，故产出的签名可被 ``verify_token`` 正常验过，
        测例据此验证「过期 / 篡改」场景。
        """
        return base64.urlsafe_b64encode(
            hmac.new(self._secret().encode(), data.encode(), hashlib.sha256).digest()
        ).decode()


# 全局令牌管理器实例
_token_manager = TokenManager()


# ── 密码哈希（PBKDF2-HMAC-SHA256，stdlib，无外部依赖）──────────────────────
# 存储格式支持两种：
#   - 哈希 `pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>`（推荐，生产应使用）
#   - 明文（legacy 兼容，启动时告警，建议改为哈希）
# 生成哈希：python -c "from web.auth_middleware import hash_password; print(hash_password('你的密码'))"
_PBKDF2_ITER = 200_000


def hash_password(password: str) -> str:
    """生成 PBKDF2-HMAC-SHA256 密码哈希，供写入 config.yaml 的 web.auth_password。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITER,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码：哈希格式走 PBKDF2，明文格式向后兼容（恒定时间比较）。"""
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
        except ValueError:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(dk, expected)
    return hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))


# ── 登录失败限流（进程内，按用户名锁定，防暴力破解）────────────────────────
_login_fail_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}
_MAX_LOGIN_FAILS = 5
_LOGIN_LOCKOUT_SECS = 300


def _login_rate_allowed(username: str) -> bool:
    now = time.time()
    with _login_fail_lock:
        ts = _login_failures.get(username, [])
        ts = [t for t in ts if now - t < _LOGIN_LOCKOUT_SECS]
        _login_failures[username] = ts
        return len(ts) < _MAX_LOGIN_FAILS


def _register_login_failure(username: str) -> None:
    with _login_fail_lock:
        _login_failures.setdefault(username, []).append(time.time())


def _clear_login_failures(username: str) -> None:
    with _login_fail_lock:
        _login_failures.pop(username, None)


def login(username: str, password: str) -> dict[str, Any]:
    """用户登录，返回令牌。"""
    # Import config using shared_state
    from src.shared_state import get_config
    cfg = get_config()
    if cfg is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    if not cfg.web.auth_enabled:
        raise HTTPException(status_code=403, detail="Authentication is disabled")

    # 登录失败限流：同一用户名连续失败超阈值后临时锁定，防暴力破解
    if not _login_rate_allowed(username):
        logger.warning("[认证] 登录限流：用户 %s 短时失败过多，已临时锁定", username)
        raise HTTPException(status_code=429, detail="Too many failed attempts, try later")

    if username != cfg.web.auth_username:
        _register_login_failure(username)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    stored_password = cfg.web.auth_password
    # 明文弱口令兜底告警（生产应改为 PBKDF2 哈希）
    if stored_password and not stored_password.startswith("pbkdf2_sha256$") \
            and stored_password in ("please-change-me",):
        logger.error(
            "[认证] 检测到默认/弱口令，存在暴力破解风险，"
            "请改用 hash_password() 生成的 PBKDF2 哈希写入 config.yaml 的 web.auth_password",
        )

    if not verify_password(password, stored_password):
        _register_login_failure(username)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    _clear_login_failures(username)

    # 根据用户名分配角色（简化逻辑）
    role = ROLE_ADMIN if username == cfg.web.auth_username else ROLE_OPERATOR

    token = _token_manager.generate_token(username, role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": role,
        "expires_in": _TOKEN_EXPIRE_SECONDS,
    }


def logout(token: str) -> bool:
    """用户登出：将当前令牌加入黑名单，使其立即失效（verify_token 后续拒绝）。

    仅对合法（可解析）的令牌生效；非法/空令牌返回 False（无可吊销对象）。
    黑名单为进程内内存集合，多 worker 部署下仅本 worker 生效。
    """
    if not token:
        return False
    try:
        _token_manager.verify_token(token)
    except HTTPException:
        return False
    with _revoked_lock:
        _revoked_token_hashes.add(_hash_token(token))
    return True


def get_current_user(request: Request) -> dict[str, Any]:
    """获取当前用户信息。"""
    return {
        "username": getattr(request.state, "username", "anonymous"),
        "role": getattr(request.state, "role", ROLE_VIEWER),
    }
