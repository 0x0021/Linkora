"""Shared faiss index lock — used by SQLiteStore and its extracted repos.

All faiss vector index operations (add/search/save/load/rebuild) are serialized
through a module-level RLock, independent of the per-thread DB connection strategy.
"""

from __future__ import annotations

import functools
import threading

_INDEX_LOCK = threading.RLock()


def with_index_lock(func):
    """Decorator: serialize all faiss vector index operations.

    faiss C++底层并非线程安全，此前无锁的多线程 add/search 存在竞态。
    用独立的模块级 RLock 包裹，不影响 DB 操作的 per-thread 连接策略。
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _INDEX_LOCK:
            return func(*args, **kwargs)
    return wrapper
