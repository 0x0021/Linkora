"""多平台 poller 配置读侧回归测试（P1-3）。

背景：``config.poller`` 在运行时是 **dingtalk 平台块 poller 的对象别名**
（``src/config.py::_sync_root_poller``），故直接读 ``config.poller.X`` 在飞书 / 企微
链路上读到的是**钉钉**的轮询设置——跨平台串扰。``resolve_poller_config`` 是读侧的
平台感知入口。

关键不变量：**回落即现状**——单平台部署、未知平台、平台未配 poller 三种情况都回落到
``config.poller``，与改动前行为完全一致。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.config import AppConfig, resolve_poller_config
from src.config_models import PlatformConfig, PollerConfig
from src.memory.platform_context import platform_scope


def _multi_platform_config() -> AppConfig:
    """构造 dingtalk + feishu 双平台配置（模拟 _sync_root_poller 后的形态）。"""
    # web 段显式关掉鉴权：WebConfig 默认 auth_enabled=True 且无密码时会 fail-closed
    # 拒绝构造（生产安全默认），与本用例无关。
    cfg = AppConfig(web={"auth_enabled": False})
    dt = PlatformConfig(id="dingtalk", adapter_type="dingtalk")
    dt.poller = PollerConfig(interval_seconds=5)
    fs = PlatformConfig(id="feishu", adapter_type="feishu")
    fs.poller = PollerConfig(interval_seconds=30)
    cfg.platforms = [dt, fs]
    # 与 _sync_root_poller 一致：根级 poller 是 dingtalk 平台块的对象别名
    cfg.poller = dt.poller
    return cfg


def test_explicit_platform_id_resolves_that_platform():
    cfg = _multi_platform_config()
    assert resolve_poller_config(cfg, "feishu").interval_seconds == 30
    assert resolve_poller_config(cfg, "dingtalk").interval_seconds == 5


def test_unknown_platform_falls_back_to_global():
    cfg = _multi_platform_config()
    assert resolve_poller_config(cfg, "wecom") is cfg.poller


def test_platform_context_drives_resolution():
    """平台上下文（消息链路内由 platform_scope 对齐）决定读哪个平台的配置。"""
    cfg = _multi_platform_config()
    with platform_scope("feishu"):
        assert resolve_poller_config(cfg).interval_seconds == 30
    with platform_scope("dingtalk"):
        assert resolve_poller_config(cfg).interval_seconds == 5


def test_single_platform_deployment_is_unchanged():
    """单平台部署：解析结果与 config.poller 是同一对象，行为零变化。"""
    cfg = AppConfig(web={"auth_enabled": False})
    dt = PlatformConfig(id="dingtalk", adapter_type="dingtalk")
    dt.poller = PollerConfig(interval_seconds=7)
    cfg.platforms = [dt]
    cfg.poller = dt.poller
    assert resolve_poller_config(cfg, "dingtalk") is cfg.poller
    assert resolve_poller_config(cfg, "dingtalk").interval_seconds == 7


def test_platform_without_poller_falls_back():
    """平台块未配 poller（None）时回落全局，不抛异常。"""
    fake = SimpleNamespace(
        platforms=[SimpleNamespace(id="feishu", poller=None)],
        poller=PollerConfig(interval_seconds=11),
    )
    assert resolve_poller_config(fake, "feishu").interval_seconds == 11


def test_no_platforms_at_all_falls_back():
    fake = SimpleNamespace(platforms=[], poller=PollerConfig(interval_seconds=13))
    assert resolve_poller_config(fake, "feishu").interval_seconds == 13
