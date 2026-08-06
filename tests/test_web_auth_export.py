"""Web 鉴权硬化回归测试。

覆盖：
1) /api/config/export 必须脱敏密钥字段（不裸漏明文）
2) /api/config/import 遇脱敏哨兵值须从现有配置还原真实密钥（round-trip 安全）
3) web_auth_middleware：auth_enabled=False 时写入/导出类敏感端点仍强制凭据（已配凭据），
   非敏感只读与「无凭据的信任边界」维持放行；auth_enabled=True 仍为全局强制。
"""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import patch

import yaml


def _run(coro):
    return asyncio.run(coro)


# ---------- 导出脱敏 / 导入还原（直接测递归助手） ----------

def test_redact_secrets_masks_nested_and_lists():
    from web.routers.config import _redact_secrets, REDACTED_SENTINEL

    obj = {
        "llm": {"api_key": "sk-abc", "model": "gpt-4o", "fallback_api_key": "sk-def"},
        "embedding": {"api_key": "", "hf_token": "hf-xyz"},  # 空值不脱敏
        "platforms": [
            {"id": "feishu", "adapter": {"app_secret": "fs-secret"}},
            {"id": "wecom", "adapter": {"corp_secret": "wc", "token": "tk", "encoding_aes_key": "key"}},
        ],
        "web": {"auth_password": "pw", "auth_enabled": True},
    }
    out = _redact_secrets(obj)
    assert out["llm"]["api_key"] == REDACTED_SENTINEL
    assert out["llm"]["model"] == "gpt-4o"  # 非密钥保留
    assert out["llm"]["fallback_api_key"] == REDACTED_SENTINEL
    assert out["embedding"]["api_key"] == ""  # 空值不脱敏
    assert out["embedding"]["hf_token"] == REDACTED_SENTINEL
    assert out["platforms"][0]["adapter"]["app_secret"] == REDACTED_SENTINEL
    assert out["platforms"][1]["adapter"]["corp_secret"] == REDACTED_SENTINEL
    assert out["platforms"][1]["adapter"]["token"] == REDACTED_SENTINEL
    assert out["platforms"][1]["adapter"]["encoding_aes_key"] == REDACTED_SENTINEL
    assert out["web"]["auth_password"] == REDACTED_SENTINEL
    assert out["web"]["auth_enabled"] is True  # 非密钥保留


def test_restore_secrets_keeps_real_and_new_values():
    from web.routers.config import _restore_secrets, REDACTED_SENTINEL

    current = {
        "llm": {"api_key": "sk-real", "model": "gpt-4o"},
        "embedding": {"api_key": "ek-real"},
        "web": {"auth_password": "real_pw"},
    }
    imported = {
        "llm": {"api_key": REDACTED_SENTINEL, "model": "gpt-4o-mini"},  # 哨兵->还原；新值保留
        "embedding": {"api_key": "ek-brand-new"},                       # 新真实值保留
        "web": {"auth_password": REDACTED_SENTINEL},                    # 哨兵->还原
    }
    _restore_secrets(imported, current)
    assert imported["llm"]["api_key"] == "sk-real"
    assert imported["llm"]["model"] == "gpt-4o-mini"
    assert imported["embedding"]["api_key"] == "ek-brand-new"
    assert imported["web"]["auth_password"] == "real_pw"


def test_export_config_endpoint_masks_secrets():
    from web.routers.config import export_config
    from src.config import AppConfig

    cfg = AppConfig(web={"auth_password": "admin_pw"})
    cfg.llm.api_key = "sk-realsecret123"
    cfg.llm.fallback_api_key = "sk-fallback999"
    cfg.embedding.api_key = "ek-abc"
    cfg.embedding.hf_token = "hf-xyz"
    cfg.web.auth_password = "admin_pw"

    with patch("web.api._get_cfg", return_value=cfg):
        resp = _run(export_config())

    body = json.loads(resp.body.decode("utf-8"))
    exported = yaml.safe_load(body["config"])
    assert exported["llm"]["api_key"] == "***REDACTED***"
    assert exported["llm"]["fallback_api_key"] == "***REDACTED***"
    assert exported["embedding"]["api_key"] == "***REDACTED***"
    assert exported["embedding"]["hf_token"] == "***REDACTED***"
    assert exported["web"]["auth_password"] == "***REDACTED***"
    # 非密钥字段原样保留
    assert exported["llm"]["model"] == cfg.llm.model


def test_import_config_restores_redacted_secrets(monkeypatch):
    """import_config 在写盘前应从现有配置还原哨兵密钥，避免覆盖真值。"""
    import os
    import tempfile
    from web.routers import config as config_router

    def _model_dump():
        return {
            "web": {"auth_enabled": True, "auth_username": "admin", "auth_password": "real_pw"},
            "llm": {"api_key": "sk-real-current"},
        }

    real_cfg = SimpleNamespace(
        web=SimpleNamespace(auth_enabled=True, auth_username="admin", auth_password="real_pw"),
        llm=SimpleNamespace(api_key="sk-real-current"),
    )
    real_cfg.model_dump = _model_dump

    # 构造一个含哨兵值的导入文件（模拟从脱敏导出回灌）
    payload = {
        "dws": {"cli_path": "dws", "profile": "default", "dry_run": False, "retries": 3, "timeout": 30},
        "llm": {"api_key": "***REDACTED***", "provider": "openai", "model": "gpt-4o"},
        "poller": {"interval_seconds": 7},
        "storage": {"path": "data/store.db"},
        "web": {"auth_enabled": True, "auth_username": "admin", "auth_password": "***REDACTED***"},
    }
    yaml_text = yaml.safe_dump(payload, allow_unicode=True)

    class _FakeFile:
        async def read(self):
            return yaml_text.encode("utf-8")

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp_path = tmp.name
    tmp.close()

    # 当前磁盘配置（含真实 api_key）：先写入 CONFIG_PATH，作为导入还原的 current 源
    # （新逻辑仅从磁盘文件还原，不取环境变量注入的真实密钥，避免 env 密钥落盘）
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(_model_dump(), f, allow_unicode=True)

    with patch("web.api.CONFIG_PATH", tmp_path), \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        _run(config_router.import_config(_FakeFile()))

    written = yaml.safe_load(open(tmp_path, encoding="utf-8"))
    os.unlink(tmp_path)
    assert written["llm"]["api_key"] == "sk-real-current"   # 从现有配置还原
    assert written["llm"]["model"] == "gpt-4o"              # 导入中的新值保留
    assert written["web"]["auth_password"] == "real_pw"     # 还原
    assert written["dws"]["cli_path"] == "dws"


def test_import_config_preserves_unmentioned_sections(monkeypatch):
    """导入仅含部分段的配置时，未提及的段/参数须完整保留（防静默丢参数）。"""
    import os
    import tempfile
    from web.routers import config as config_router

    # 当前磁盘配置：含 rules / tools / platforms / web 等完整定制
    current = {
        "web": {"auth_enabled": True, "auth_username": "admin",
                "auth_password": "real_pw", "host": "127.0.0.1", "port": 8080},
        "rules": {"blacklist": {"users": ["alice", "bob"]}},
        "tools": {"available": ["search", "remind", "weather"]},
        "platforms": [{"id": "dingtalk", "adapter": {"cli_path": "/usr/bin/dws"}}],
        "llm": {"api_key": "sk-real-current", "model": "gpt-4o"},
        "dws": {"cli_path": "dws", "profile": "default"},
        "storage": {"path": "data/store.db"},
    }
    # 导入文件：仅改 llm.model + dws.profile，且故意不含 rules/tools/platforms/web
    payload = {
        "dws": {"cli_path": "dws", "profile": "prod", "dry_run": False, "retries": 3, "timeout": 30},
        "llm": {"api_key": "sk-real-current", "provider": "openai", "model": "gpt-4o-mini"},
        "storage": {"path": "data/store.db"},
    }
    yaml_text = yaml.safe_dump(payload, allow_unicode=True)

    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp_path = tmp.name
    tmp.close()
    with open(tmp_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(current, f, allow_unicode=True)

    class _FakeFile:
        async def read(self):
            return yaml_text.encode("utf-8")

    with patch("web.api.CONFIG_PATH", tmp_path), \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        _run(config_router.import_config(_FakeFile()))

    written = yaml.safe_load(open(tmp_path, encoding="utf-8"))
    os.unlink(tmp_path)
    # 导入中出现的 key 已覆盖
    assert written["llm"]["model"] == "gpt-4o-mini"
    assert written["dws"]["profile"] == "prod"
    # 未提及的段完整保留（关键：不得静默丢弃）
    assert written["rules"]["blacklist"]["users"] == ["alice", "bob"]
    assert written["tools"]["available"] == ["search", "remind", "weather"]
    assert written["web"]["host"] == "127.0.0.1"
    assert written["web"]["port"] == 8080
    assert written["platforms"][0]["id"] == "dingtalk"


# ---------- 中间件鉴权策略 ----------

def _fake_config(auth_enabled, username="", password=""):
    web = SimpleNamespace(auth_enabled=auth_enabled, auth_username=username, auth_password=password)
    return SimpleNamespace(web=web)


def _fake_request(path, method="GET", headers=None, host="10.0.0.5"):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        method=method,
        headers=headers or {},
        client=SimpleNamespace(host=host),
    )


async def _ok_call_next(request):
    return "OK"


def _auth_header(user, pwd):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


def test_middleware_whitelist_passthrough():
    from web.api import web_auth_middleware

    for path in ("/", "/health", "/api/platforms", "/static/app.js", "/api/image/abc"):
        with patch("web.api._get_cfg", return_value=_fake_config(auth_enabled=False)):
            resp = _run(web_auth_middleware(_fake_request(path), _ok_call_next))
        assert resp == "OK", f"白名单 {path} 应放行"


def test_middleware_auth_enabled_requires_auth():
    from web.api import web_auth_middleware

    cfg = _fake_config(auth_enabled=True, username="admin", password="pw")
    with patch("web.api._get_cfg", return_value=cfg), patch("web.api._AUTH_FAILS", {}):
        # 无头 -> 401
        resp = _run(web_auth_middleware(_fake_request("/api/config", "POST"), _ok_call_next))
        assert resp.status_code == 401
        # 错误凭据 -> 401
        resp2 = _run(web_auth_middleware(
            _fake_request("/api/config", "POST", headers=_auth_header("admin", "wrong")), _ok_call_next))
        assert resp2.status_code == 401
        # 正确凭据 -> 放行
        resp3 = _run(web_auth_middleware(
            _fake_request("/api/config", "POST", headers=_auth_header("admin", "pw")), _ok_call_next))
        assert resp3 == "OK"


def test_middleware_auth_disabled_sensitive_requires_creds():
    from web.api import web_auth_middleware

    cfg = _fake_config(auth_enabled=False, username="admin", password="pw")
    with patch("web.api._get_cfg", return_value=cfg), patch("web.api._AUTH_FAILS", {}):
        # 敏感端点（导出 GET）无头 -> 401
        resp = _run(web_auth_middleware(_fake_request("/api/config/export", "GET"), _ok_call_next))
        assert resp.status_code == 401
        # 敏感端点（写 POST）无头 -> 401
        resp2 = _run(web_auth_middleware(_fake_request("/api/config", "POST"), _ok_call_next))
        assert resp2.status_code == 401
        # 正确凭据 -> 放行
        resp3 = _run(web_auth_middleware(
            _fake_request("/api/config/export", "GET", headers=_auth_header("admin", "pw")), _ok_call_next))
        assert resp3 == "OK"


def test_middleware_auth_disabled_sensitive_no_creds_passthrough():
    """auth_enabled=False 且无凭据（信任边界 LAN/反代）：敏感端点维持放行，
    但导出已脱敏，密钥不会裸漏。"""
    from web.api import web_auth_middleware

    cfg = _fake_config(auth_enabled=False, username="", password="")
    with patch("web.api._get_cfg", return_value=cfg), patch("web.api._AUTH_FAILS", {}):
        resp = _run(web_auth_middleware(_fake_request("/api/config/export", "GET"), _ok_call_next))
        assert resp == "OK"
        resp2 = _run(web_auth_middleware(_fake_request("/api/config", "POST"), _ok_call_next))
        assert resp2 == "OK"


def test_middleware_auth_disabled_nonsensitive_read_passthrough():
    from web.api import web_auth_middleware

    cfg = _fake_config(auth_enabled=False, username="", password="")
    with patch("web.api._get_cfg", return_value=cfg), patch("web.api._AUTH_FAILS", {}):
        resp = _run(web_auth_middleware(_fake_request("/api/some/read", "GET"), _ok_call_next))
        assert resp == "OK"
