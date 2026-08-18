"""Web API 认证中间件。

提供 JWT 令牌认证和基于角色的访问控制 (RBAC)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from functools import wraps
from typing import Any, Callable

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
    except Exception:
        pass
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
        """验证令牌并返回 payload；非法/过期统一抛 401。"""
        try:
            return _pyjwt.decode(token, self._secret(), algorithms=["HS256"])
        except _pyjwt.ExpiredSignatureError as exc:
            raise HTTPException(status_code=401, detail="Token expired") from exc
        except _pyjwt.InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail=f"Token verification failed: {exc}") from exc

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


def require_role(*roles: str) -> Callable:
    """角色检查装饰器。"""
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            role = getattr(request.state, "role", None)
            if role not in roles:
                raise HTTPException(
                    status_code=403,
                    detail=f"Requires role: {', '.join(roles)}"
                )
            return await f(request, *args, **kwargs)
        return wrapper
    return decorator


def login(username: str, password: str) -> dict[str, Any]:
    """用户登录，返回令牌。"""
    # Import config using shared_state
    from src.shared_state import get_config
    cfg = get_config()
    if cfg is None:
        raise HTTPException(status_code=500, detail="Configuration not loaded")

    if not cfg.web.auth_enabled:
        raise HTTPException(status_code=403, detail="Authentication is disabled")

    if username != cfg.web.auth_username:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    expected_password = cfg.web.auth_password.encode("utf-8")
    provided_password = password.encode("utf-8")

    if not hmac.compare_digest(provided_password, expected_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

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
    """用户登出（本地标记黑名单）。"""
    # TODO: 实现令牌黑名单机制
    return True


def get_current_user(request: Request) -> dict[str, Any]:
    """获取当前用户信息。"""
    return {
        "username": getattr(request.state, "username", "anonymous"),
        "role": getattr(request.state, "role", ROLE_VIEWER),
    }
