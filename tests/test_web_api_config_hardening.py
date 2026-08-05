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
         patch("web.api._write_config"), \
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
         patch("web.api._write_config"), \
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
    from web.routers.config import update_config
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


def test_all_router_annotations_are_runtime_resolvable():
    """所有路由层函数的注解必须能在运行时求值（FastAPI 请求体推导前置条件）。

    背景（真实缺陷回归）：`web/routers/config.py` 曾以「`from __future__ import
    annotations` 已让注解懒求值，无需运行时导入」为由，不导入 ConfigUpdate /
    SystemPromptUpdate。但 FastAPI 在构建 dependant 时会 `get_type_hints()` 对
    签名求值，模块 namespace 里没有这两个名字即 NameError，请求体模型推导不出来。

    本文件其余测试直接把 update_config 当普通协程调用（自己构造 payload），
    完全绕开了 FastAPI 的注解求值路径，因此掩盖了该缺陷——故此处补一条覆盖
    全部 router 的通用护栏，未来任何 router 漏导入类型都会在这里立刻暴露。
    """
    import inspect
    import sys
    import typing

    import web.api  # noqa: F401  先完整加载入口，子 router 随之就绪（规避循环导入）

    bad = []
    checked = 0
    for modname, mod in list(sys.modules.items()):
        if not modname.startswith("web.routers.") or mod is None:
            continue
        for name, fn in vars(mod).items():
            if not (inspect.isfunction(fn) and getattr(fn, "__module__", "") == modname):
                continue
            if not fn.__annotations__:
                continue
            checked += 1
            try:
                typing.get_type_hints(fn)
            except Exception as e:  # noqa: BLE001 — 收集全部失败点便于一次修完
                bad.append(f"{modname}.{name} -> {type(e).__name__}: {e}")

    assert checked > 100, f"扫描到的路由函数过少（{checked}），护栏可能已失效"
    assert not bad, "以下路由函数注解无法在运行时求值（FastAPI 会推导不出请求体）：\n  " + "\n  ".join(bad)
