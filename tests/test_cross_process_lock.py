"""跨进程互斥锁 cross_process_lock 的回归测试。

验证：
- 空闲时可正常获取（yield True）；
- 已被其他进程/ fd 持有时跳过（yield False），不阻塞。
"""
from __future__ import annotations

import fcntl
import os
import tempfile

from src.tools.utils import cross_process_lock


def test_cross_process_lock_acquires_when_free():
    with cross_process_lock("test-free", tempfile.gettempdir()) as acquired:
        assert acquired is True


def test_cross_process_lock_skips_when_held():
    lock_path = os.path.join(tempfile.gettempdir(), "dingtalk-test-held.lock")
    with open(lock_path, "w") as f:
        # 模拟另一个进程已持有锁
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with cross_process_lock("test-held", tempfile.gettempdir()) as acquired:
            assert acquired is False
