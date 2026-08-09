"""Web API 认证中间件。

提供 JWT 令牌认证和基于角色的访问控制 (RBAC)。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from functools import wraps
from typing import Any, Callable

from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# JWT 配置（简单实现，生产环境建议使用 pyjwt）
_TOKEN_SECRET_KEY = "linkora-dev-secret-change-in-production"
_TOKEN_EXPIRE_SECONDS = 3600 * 24  # 24 小时

# 角色定义
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

VALID_ROLES = {ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER}


class TokenManager:
    """简单的令牌管理器。"""

    def __init__(self, secret_key: str = _TOKEN_SECRET_KEY):
        self.secret_key = secret_key

    def generate_token(self, username: str, role: str = ROLE_VIEWER) -> str:
        """生成 JWT 风格的令牌。"""
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role: {role}")

        header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode()
        payload = base64.urlsafe_b64encode(
            f'{{"sub":"{username}","role":"{role}","iat":{int(time.time())},"exp":{int(time.time()) + _TOKEN_EXPIRE_SECONDS}}}'.encode()
        ).decode()

        signature = self._sign(f"{header}.{payload}")
        return f"{header}.{payload}.{signature}"

    def verify_token(self, token: str) -> dict[str, Any]:
        """验证令牌并返回 payload。"""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError("Invalid token format")

            header, payload, signature = parts

            # 验证签名
            expected_signature = self._sign(f"{header}.{payload}")
            if not hmac.compare_digest(signature, expected_signature):
                raise HTTPException(status_code=401, detail="Invalid signature")

            # 解码 payload
            decoded_payload = base64.urlsafe_b64decode(payload).decode()
            data = eval(decoded_payload)  # 简化实现，生产环境用 json.loads

            # 检查过期
            if data.get("exp", 0) < time.time():
                raise HTTPException(status_code=401, detail="Token expired")

            return data
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=401, detail=f"Token verification failed: {e}") from e

    def _sign(self, data: str) -> str:
        """计算 HMAC-SHA256 签名。"""
        return base64.urlsafe_b64encode(
            hmac.new(self.secret_key.encode(), data.encode(), hashlib.sha256).digest()
        ).decode()


# 全局令牌管理器实例
_token_manager = TokenManager()


def require_auth(f: Callable) -> Callable:
    """认证装饰器。"""
    @wraps(f)
    async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
        auth_header = request.headers.get("Authorization", "")

        if not auth_header:
            raise HTTPException(status_code=401, detail="Authentication required")

        if auth_header.startswith("Basic "):
            # Basic Auth（向后兼容）
            try:
                creds = base64.b64decode(auth_header[6:]).decode("utf-8")
                username, password = creds.split(":", 1)
                # TODO: 验证用户名密码
                request.state.username = username
                request.state.role = ROLE_ADMIN
            except Exception as e:
                raise HTTPException(status_code=401, detail=f"Invalid credentials: {e}") from e
        elif auth_header.startswith("Bearer "):
            # Bearer Token (JWT)
            token = auth_header[7:]
            try:
                payload = _token_manager.verify_token(token)
                request.state.username = payload.get("sub", "unknown")
                request.state.role = payload.get("role", ROLE_VIEWER)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=401, detail=f"Token error: {e}") from e
        else:
            raise HTTPException(status_code=401, detail="Unsupported auth type")

        return await f(request, *args, **kwargs)

    return wrapper


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
