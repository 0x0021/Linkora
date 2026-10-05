"""config.yaml 写入安全回归（P0 配置红线）。

锁死历史事故「config.yaml 被整体覆盖成 example 模板，丢全部真实定制」：
断言 config_manage 的 update 动作是「加载 → 单键合入 → 原子写」，
而非「用默认/模板 dict 整体 dump 覆盖」，因此改一个键不会丢其他键（含未知 key）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import src.tools.management as mgt
from src.tools.management import ConfigManageTool

REPO_ROOT = Path(__file__).resolve().parents[1]


def _base_config() -> dict:
    # 以真实 example 模板为基准（合法），保证 load_config 校验通过
    example = yaml.safe_load(
        (REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8")
    )
    # 关闭 web 鉴权，避免触发 auth_password 必填校验（与保参测试无关）
    example.setdefault("web", {})["auth_enabled"] = False
    # 显式注入 update 所需的字段，避免 example 结构漂移导致 key 不存在
    example.setdefault("llm", {})["temperature"] = 0.5
    example.setdefault("tools", {})["enabled"] = True
    example.setdefault("tools", {})["available"] = ["a", "b"]
    example.setdefault("platforms", [{"id": "dingtalk", "poller": {"interval_seconds": 30}}])
    return example


def _write_tmp_config(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return p


def test_update_preserves_all_other_keys(monkeypatch, tmp_path):
    cfg = _base_config()
    cfg["my_unknown_flag"] = "keep-me"  # 未知 key，绝不能被整体覆盖丢弃
    p = _write_tmp_config(tmp_path, cfg)
    monkeypatch.setattr(mgt, "get_config_path", lambda: str(p))

    res = ConfigManageTool().execute(
        {
            "action": "update",
            "section": "llm",
            "key": "temperature",
            "value": "0.3",
        }
    )
    assert res.get("success") is True

    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    # 改的键生效
    assert reloaded["llm"]["temperature"] == 0.3
    # 同段其他键保留
    assert reloaded["llm"]["model"]
    # 其他段保留
    assert reloaded["tools"]["enabled"] is not None
    # 未知 key 保留（P0 红线核心：整体覆盖会丢它）
    assert reloaded.get("my_unknown_flag") == "keep-me"
    # 平台块保留
    assert any(pl.get("id") == "dingtalk" for pl in reloaded.get("platforms", []))


def test_update_merges_section_not_overwrite(monkeypatch, tmp_path):
    cfg = _base_config()
    p = _write_tmp_config(tmp_path, cfg)
    monkeypatch.setattr(mgt, "get_config_path", lambda: str(p))

    res = ConfigManageTool().execute(
        {
            "action": "update",
            "section": "tools",
            "key": "enabled",
            "value": True,
        }
    )
    assert res.get("success") is True

    reloaded = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert reloaded["tools"]["enabled"] is True
    # 同段其他键不被整体覆盖丢掉
    assert reloaded["tools"]["available"] == ["a", "b"]


def test_placeholder_password_rejected_fail_closed():
    """config.yaml.example 的占位密码必须 fail-closed 拒绝启动，强制用户设置真实密码。

    回归护栏：曾漏将占位哨兵加入已知默认口令清单，导致抄模板后未改密码即以公开口令登入。
    """
    from src.config_models import WebConfig

    with pytest.raises(ValueError):
        WebConfig(auth_enabled=True, auth_password="REPLACE_WITH_YOUR_STRONG_PASSWORD")


def test_example_ships_default_password():
    """示例模板须随附「可用」的出厂默认口令 Admin@P0sw0rd（非 fail-closed 占位符），
    使 cp 模板即可启动并登录；该口令在**本地/内网绑定**下必须放行，
    否则等于没给默认密码（v0.5.4 修复的「没默认密码、登不进去」回归）。

    真正的 fail-closed 守卫仍由 test_placeholder_password_rejected_fail_closed 守护：
    REPLACE_WITH_YOUR_STRONG_PASSWORD 等占位/弱口令依旧被拒（任何绑定下）。
    公网暴露场景由 test_factory_password_rejected_when_publicly_exposed 守护。
    """
    from src.config_models import WebConfig

    example = yaml.safe_load(
        (REPO_ROOT / "config.yaml.example").read_text(encoding="utf-8")
    )
    pw = example["web"]["auth_password"]
    assert pw == "Admin@P0sw0rd"
    # 出厂默认口令在本地/内网绑定下必须可用（不触发 fail-closed），否则等于没给默认密码
    for host in ("127.0.0.1", "localhost", "::1", "192.168.1.10", "10.0.0.5", ""):
        WebConfig(auth_enabled=True, auth_password=pw, host=host)  # 不应抛 ValueError


def test_factory_password_rejected_when_publicly_exposed():
    """公网暴露（host 非回环/非私网）时，出厂默认口令必须 fail-closed 拒绝启动。

    背景：Admin@P0sw0rd 公开在 git 跟踪的 config.yaml.example 中。保留它是为了
    「cp 模板即可登录」的开箱体验（v0.5.4），但一旦服务对外可路由，任何人都能用
    这个公开口令登录管理后台——此时必须 fail-closed。
    """
    from src.config_models import WebConfig

    # 对外暴露的各种写法：0.0.0.0 / :: / 公网 IP / 主机名，均须拒绝
    # （203.0.113.x 是 RFC 文档保留段，ipaddress 归为 private，故用真实公网 IP 8.8.8.8）
    for host in ("0.0.0.0", "::", "8.8.8.8", "linkora.example.com"):
        with pytest.raises(ValueError, match="出厂默认口令"):
            WebConfig(auth_enabled=True, auth_password="Admin@P0sw0rd", host=host)


def test_strong_password_allowed_when_publicly_exposed():
    """公网暴露 + 自有强密码（含 PBKDF2 哈希）→ 正常放行（防过度拦截）。"""
    from src.config_models import WebConfig

    WebConfig(auth_enabled=True, auth_password="my-own-strong-pass-9271", host="0.0.0.0")
    # PBKDF2 哈希串同样放行
    WebConfig(
        auth_enabled=True,
        auth_password="pbkdf2_sha256$200000$c2FsdA==$aGFzaA==",
        host="0.0.0.0",
    )


def test_weak_passwords_rejected_on_all_bindings():
    """弱口令/占位哨兵在任何绑定下都拒绝（不因分级策略而放宽）。"""
    from src.config_models import WebConfig

    for pw in ("", "   ", "admin", "password", "changeme",
               "please-change-me", "REPLACE_WITH_YOUR_STRONG_PASSWORD"):
        for host in ("127.0.0.1", "0.0.0.0"):
            with pytest.raises(ValueError):
                WebConfig(auth_enabled=True, auth_password=pw, host=host)
