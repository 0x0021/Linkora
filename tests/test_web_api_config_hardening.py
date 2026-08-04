"""web/api.py update_config 硬化测试。

回归护栏：
1) auth_password 空字符串 / 纯空格不写（防误清空鉴权）
2) auth_password 正常值正常写

update_config 是 async def，测试需 asyncio.run()。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch



def _run(coro):
    return asyncio.run(coro)


def test_auth_password_empty_string_keeps_existing(monkeypatch):
    """空字符串应被识别为「未提供」，不覆盖原密码。"""
    from web.routers.config import update_config
    from web.api import ConfigUpdate
    fake_web = type("W", (), {
        "auth_enabled": True,
        "auth_username": "admin",
        "auth_password": "原密码_保留",
    })()
    fake_config = MagicMock()
    fake_config.web = fake_web
    with patch("web.api.load_config", return_value=fake_config), \
         patch("web.api._write_config") as mock_write, \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        payload = ConfigUpdate(web_auth_password="")
        _run(update_config(payload))
    assert fake_config.web.auth_password == "原密码_保留"


def test_auth_password_whitespace_only_keeps_existing(monkeypatch):
    """纯空格也应被识别为「未提供」。"""
    from web.routers.config import update_config
    from web.api import ConfigUpdate
    fake_web = type("W", (), {
        "auth_enabled": True,
        "auth_username": "admin",
        "auth_password": "原密码_保留2",
    })()
    fake_config = MagicMock()
    fake_config.web = fake_web
    with patch("web.api.load_config", return_value=fake_config), \
         patch("web.api._write_config") as mock_write, \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        payload = ConfigUpdate(web_auth_password="   \t\n  ")
        _run(update_config(payload))
    assert fake_config.web.auth_password == "原密码_保留2"


def test_auth_password_real_value_writes(monkeypatch):
    """非空真实密码应正常写入。"""
    from web.routers.config import update_config
    from web.api import ConfigUpdate
    fake_web = type("W", (), {
        "auth_enabled": True,
        "auth_username": "admin",
        "auth_password": "old",
    })()
    fake_config = MagicMock()
    fake_config.web = fake_web
    with patch("web.api.load_config", return_value=fake_config), \
         patch("web.api._write_config") as mock_write, \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        payload = ConfigUpdate(web_auth_password="new_secure_pwd")
        _run(update_config(payload))
    assert fake_config.web.auth_password == "new_secure_pwd"
    # update_config 末尾总是调 _write_config 一次
    mock_write.assert_called_once()


def test_secret_fields_redacted_sentinel_keeps_existing(monkeypatch):
    """llm/embedding/web 密钥字段若回灌 ***REDACTED*** 哨兵，不得覆盖真值（防数据丢失）。"""
    from web.routers.config import update_config, REDACTED_SENTINEL
    from web.api import ConfigUpdate
    fake_web = type("W", (), {
        "auth_enabled": True,
        "auth_username": "admin",
        "auth_password": "pw-real",
    })()
    fake_config = MagicMock()
    fake_config.llm.api_key = "sk-real"
    fake_config.llm.fallback_api_key = "sk-fb-real"
    fake_config.embedding.api_key = "ek-real"
    fake_config.embedding.hf_token = "hf-real"
    fake_config.web = fake_web
    with patch("web.api.load_config", return_value=fake_config), \
         patch("web.api._write_config") as mock_write, \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        payload = ConfigUpdate(
            llm_api_key=REDACTED_SENTINEL,
            llm_fallback_api_key=REDACTED_SENTINEL,
            embedding_api_key=REDACTED_SENTINEL,
            embedding_hf_token=REDACTED_SENTINEL,
            web_auth_password=REDACTED_SENTINEL,
        )
        _run(update_config(payload))
    assert fake_config.llm.api_key == "sk-real"
    assert fake_config.llm.fallback_api_key == "sk-fb-real"
    assert fake_config.embedding.api_key == "ek-real"
    assert fake_config.embedding.hf_token == "hf-real"
    assert fake_config.web.auth_password == "pw-real"
    mock_write.assert_called_once()


def test_secret_fields_real_value_writes(monkeypatch):
    """非哨兵的真实密钥值应正常写入（不误杀正常更新）。"""
    from web.routers.config import update_config, REDACTED_SENTINEL
    from web.api import ConfigUpdate
    fake_web = type("W", (), {
        "auth_enabled": True,
        "auth_username": "admin",
        "auth_password": "pw-old",
    })()
    fake_config = MagicMock()
    fake_config.llm.api_key = "sk-old"
    fake_config.embedding.api_key = "ek-old"
    fake_config.web = fake_web
    with patch("web.api.load_config", return_value=fake_config), \
         patch("web.api._write_config") as mock_write, \
         patch("src.shared_state.get_config_reload_callback", return_value=None):
        payload = ConfigUpdate(
            llm_api_key="sk-new",
            embedding_api_key="ek-new",
            web_auth_password="pw-new",
        )
        _run(update_config(payload))
    assert fake_config.llm.api_key == "sk-new"
    assert fake_config.embedding.api_key == "ek-new"
    assert fake_config.web.auth_password == "pw-new"
    mock_write.assert_called_once()
