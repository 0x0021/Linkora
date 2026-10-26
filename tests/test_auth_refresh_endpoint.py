"""POST /api/auth/refresh 滚动续期端点回归。

背景（真实故障 2026-10-26）：JWT 硬过期（24h）后，管理台若不刷新页面就毫无
感知，直到用户点某个写操作（死信队列的「丢弃」）才突然 401，前端只能弹出
英文 ``unauthorized`` —— 用户看到的就是「点删除报错」。

同批修复引入了 ``/api/auth/refresh``：持有效 Bearer 即可换发新令牌（同 sub/role），
前端在剩余有效期不足 30 分钟时静默续期，并在撞 401 时续期后重放一次请求。

本文件锁定三条契约：
1. 没有 Bearer → 401（不能变成免认证的令牌批发口）；
2. 非法令牌 → 401；
3. 有效令牌 → 200 且新令牌保留原身份与角色（RBAC 不被续期放大或降级）。
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from web.auth_middleware import TokenManager

_SECRET = "refresh-endpoint-test-secret-0123456789abcdef"  # ≥32 字节，避免 pyjwt 短密钥告警


def _client() -> TestClient:
    from web.api import app

    return TestClient(app)


def _with_secret():
    """固定 JWT 签名密钥，避免测试受本机 config.web.jwt_secret 影响。"""
    return patch("web.auth_middleware._runtime_jwt_secret", _SECRET)


class TestRefreshEndpoint:
    def test_requires_bearer_token(self):
        resp = _client().post("/api/auth/refresh")
        assert resp.status_code == 401
        assert "Bearer" in resp.json()["detail"]

    def test_rejects_invalid_token(self):
        with _with_secret():
            resp = _client().post(
                "/api/auth/refresh",
                headers={"Authorization": "Bearer not-a-real-jwt"},
            )
        assert resp.status_code == 401

    def test_issues_new_token_keeping_identity_and_role(self):
        tm = TokenManager(_SECRET)
        old_token = tm.generate_token("alice", "operator")

        with _with_secret():
            resp = _client().post(
                "/api/auth/refresh",
                headers={"Authorization": f"Bearer {old_token}"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["username"] == "alice"
        assert data["role"] == "operator"
        assert data["access_token"]

        payload = tm.verify_token(data["access_token"])
        assert payload["sub"] == "alice"
        assert payload["role"] == "operator"

    def test_rejects_expired_token(self):
        """过期的令牌不能用来续期——否则等于无限期会话。"""
        import time

        import jwt as _pyjwt

        now = int(time.time())
        expired = _pyjwt.encode(
            {"sub": "alice", "role": "admin", "iat": now - 7200, "exp": now - 3600},
            _SECRET,
            algorithm="HS256",
            headers={"alg": "HS256", "typ": "JWT"},
        )
        with _with_secret():
            resp = _client().post(
                "/api/auth/refresh",
                headers={"Authorization": f"Bearer {expired}"},
            )
        assert resp.status_code == 401
