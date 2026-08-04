"""SQLiteStore 工厂 / 单例模块。

提供线程安全的全局 store 获取与重置，供所有模块统一使用。
首次调用 get_store() 时从 config.yaml 读取默认数据库路径并构造单例；
后续调用返回同一实例（同一 db_path 命中缓存）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from src.memory.sqlite_store import SQLiteStore

_lock = threading.Lock()
_instances: dict[str, SQLiteStore] = {}


def get_store(db_path: Optional[str] = None) -> SQLiteStore:
    """获取 SQLiteStore 实例。

    首次调用时按 db_path 创建（未传则从 config.yaml 读取 storage.path），
    后续调用返回缓存实例。同一 db_path 只保留一个实例。

    Args:
        db_path: 数据库文件路径。None 时自动从 config.yaml 读取。

    Returns:
        SQLiteStore 实例（单例）。
    """
    if db_path is None:
        from src.config import load_config
        cfg = load_config()
        db_path = cfg.storage.path

    # 归一化为绝对路径，保证同一文件不会因相对路径差异产生重复实例
    # 使用 os.path.abspath 而非 Path.resolve()：避免在 macOS 上跟随 /var→/private/var
    # 等符号链接，导致 db_path 与调用方预期不一致。
    resolved = os.path.abspath(db_path)

    with _lock:
        if resolved not in _instances or getattr(_instances[resolved], "_closed", False):
            store = SQLiteStore(resolved)
            _instances[resolved] = store
        return _instances[resolved]


def reset_store(db_path: Optional[str] = None) -> None:
    """重置缓存，用于测试清理。

    Args:
        db_path: 重置指定路径的实例。None 则清空全部缓存。
    """
    with _lock:
        if db_path is None:
            _instances.clear()
        else:
            resolved = os.path.abspath(db_path)
            _instances.pop(resolved, None)
