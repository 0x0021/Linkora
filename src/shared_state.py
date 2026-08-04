"""全局共享状态，用于 Web API 和主进程之间的通信。"""

from __future__ import annotations

import threading
from typing import Any, Callable

_app_instance: Any | None = None
_config_reload_callback: Callable[[], None] | None = None

# 配置单一真源：由主进程（main.py）在启动与热重载时发布，Web API 直接读取，
# 避免“磁盘 yaml / 主进程内存副本 / Web 每请求重读磁盘”三处真源不一致及冗余 IO。
_config: Any | None = None
_config_lock = threading.Lock()


def set_app_instance(instance: Any) -> None:
    global _app_instance
    _app_instance = instance


def get_app_instance() -> Any | None:
    return _app_instance


def set_config_reload_callback(callback: Callable[[], None]) -> None:
    global _config_reload_callback
    _config_reload_callback = callback


def get_config_reload_callback() -> Callable[[], None] | None:
    return _config_reload_callback


def set_config(cfg: Any) -> None:
    """发布权威配置（主进程持有）。线程安全。"""
    global _config
    with _config_lock:
        _config = cfg


def get_config() -> Any | None:
    """读取共享配置单例；未启动主进程（测试 / 独立 Web）时为 None。"""
    with _config_lock:
        return _config
