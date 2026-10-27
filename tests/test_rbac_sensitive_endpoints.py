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


def test_every_get_endpoint_is_classified():
    """每个 GET 端点都必须**明确表态**：敏感（admin-only）或公开（任意已认证角色）。

    这是 P1-4 的核心闭环：两个集合之外的端点 = 未表态 → CI 直接红。
    目的：把「新增只读端点时记得往敏感清单里加一行」这种易漏的隐式约定，
    改成必须表态——漏表态在 CI 暴露，而不是上线后才被漏审。

    端点全集用 **公开 API** ``app.openapi()`` 取（含 router prefix 拼接，
    且不依赖新版 FastAPI 的私有 ``_IncludedRouter`` 包装）。
    """
    from web.api import app

    gets = {p for p, ops in app.openapi().get("paths", {}).items() if "get" in ops}
    unclassified = sorted(
        p for p in gets
        if not web_api._is_sensitive_get_path(p) and p not in web_api._PUBLIC_GET_PATHS
    )
    assert unclassified == [], (
        "未表态的 GET 端点（请归入 _SENSITIVE_GET_PATHS 或 _PUBLIC_GET_PATHS）："
        f"{unclassified}"
    )


def test_public_allowlist_entries_all_exist():
    """公开白名单里的路径必须真实存在——防拼错造成假覆盖，也防端点删除后清单腐化。"""
    from web.api import app

    paths = set(app.openapi().get("paths", {}).keys())
    missing = sorted(p for p in web_api._PUBLIC_GET_PATHS if p not in paths)
    assert missing == [], f"公开白名单中的路径已不存在（请清理）：{missing}"


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
