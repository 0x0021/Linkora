"""RBAC 敏感只读端点回归测试（P1-4）。

两件事被固化：

1. **敏感只读端点判定**：早期实现用 ``path in _SENSITIVE_GET_PATHS`` 精确相等，
   带路径参数的端点（``/api/kb/documents/{doc_id}``）永远无法命中——登记了等于
   没登记。现为「精确 + 前缀」匹配，本测试锁住该行为与高危端点清单。
2. **``GET /api/config`` 不得明文回传 ``jwt_secret``**：它是 JWT 签名密钥，
   泄漏即可离线伪造任意角色（含 admin）的合法令牌（图片访问 token 密钥亦由它派生）。
"""
from __future__ import annotations

import asyncio

import web.api as web_api
from src.config import AppConfig
from web.routers import config as config_router


# ── 敏感只读端点判定 ────────────────────────────────────────────────────────

def test_exact_sensitive_paths_are_flagged():
    for path in (
        "/api/config/export",
        "/api/logs",
        "/api/system/paths",
        "/api/image-token",
    ):
        assert web_api._is_sensitive_get_path(path) is True, path


def test_path_parameter_endpoints_are_flagged_via_prefix():
    """带路径参数的端点必须被前缀匹配命中（精确相等时永远漏判）。"""
    for path in (
        "/api/kb/documents/42",
        "/api/dingtalk-docs/doc-1",
        "/api/departments/dept-1/members",
        "/api/skills/my-skill/config",
        "/api/messages/export",
        "/api/messages/sync-history/log",
        "/api/dead-letters/9",
    ):
        assert web_api._is_sensitive_get_path(path) is True, path


def test_public_read_paths_are_not_flagged():
    for path in (
        "/api/status",
        "/api/health",
        "/api/config-drift",
        "/api/auth/refresh",
        "/api/metrics/overview",
    ):
        assert web_api._is_sensitive_get_path(path) is False, path


def test_sensitive_path_list_covers_audited_endpoints():
    """审计确认的高危只读端点必须在集合内，防止后续被误删。"""
    required = {
        "/api/config/export",
        "/api/logs",
        "/api/system/paths",
        "/api/image-token",
        "/api/messages",
        "/api/departments",
        "/api/skills",
        "/api/dead-letters",
        "/api/decisions/export",
        "/api/cost-quality/export",
        "/api/metrics/export",
        "/api/kb/documents",
        "/api/dingtalk-docs",
        "/api/persona",
        "/api/memories",
        "/api/summaries",
    }
    missing = required - web_api._SENSITIVE_GET_PATHS
    assert missing == set(), f"敏感端点清单缺失: {sorted(missing)}"


# ── GET /api/config 不得泄漏 jwt_secret ────────────────────────────────────

def test_get_config_masks_jwt_secret(monkeypatch):
    secret = "SUPER_SECRET_JWT_KEY_ABC123"
    cfg = AppConfig(web={"auth_enabled": False, "jwt_secret": secret})
    monkeypatch.setattr(web_api, "_get_cfg", lambda: cfg)
    data = asyncio.run(config_router.get_config())
    assert secret not in str(data), "jwt_secret 不得明文回传"
    assert data["web"]["jwt_secret"].endswith("****")
    assert data["web"]["auth_password"] == ""


def test_get_config_masks_llm_api_keys(monkeypatch):
    """回归保护：llm/embedding 段的密钥脱敏不得被后续改动破坏。"""
    cfg = AppConfig(web={"auth_enabled": False})
    cfg.llm.api_key = "sk-LLM-SECRET-0001"
    cfg.embedding.api_key = "sk-EMB-SECRET-0002"
    monkeypatch.setattr(web_api, "_get_cfg", lambda: cfg)
    data = asyncio.run(config_router.get_config())
    assert "sk-LLM-SECRET-0001" not in str(data)
    assert "sk-EMB-SECRET-0002" not in str(data)
