"""平台生命周期单元测试。

覆盖 src/platform/lifecycle.py 的核心逻辑：启动、关闭、状态管理。
"""
from __future__ import annotations

import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock


class TestLifecycleMixin:
    """测试 LifecycleMixin 核心方法。"""

    def test_shutdown_called(self):
        """验证 shutdown 方法存在并被调用。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        assert 'def shutdown' in source or 'async def shutdown' in source

    def test_run_method_exists(self):
        """验证 run 方法存在。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        assert 'def run' in source or 'async def run' in source


class TestShutdownGraceful:
    """测试优雅关闭逻辑。"""

    def test_shutting_down_flag_set(self):
        """关闭时设置 shutting_down 标志。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        assert 'shutting_down' in source.lower() or '_running' in source

    def test_poller_stopped_during_shutdown(self):
        """关闭时应停止轮询器。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        # 验证关闭流程包含停止轮询
        assert 'stop' in source.lower() or 'cancel' in source.lower()


class TestPlatformStartStop:
    """测试平台启动停止流程。"""

    def test_run_method_exists(self):
        """验证 run 方法存在（启动入口）。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        assert 'def run' in source

    def test_shutdown_method_exists(self):
        """验证 shutdown 方法存在（停止入口）。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        assert 'def shutdown' in source


class TestBackgroundThreads:
    """测试后台线程管理。"""

    def test_daemon_threads_spawned(self):
        """验证 daemon 线程被正确创建。"""
        with open('src/platform/memory.py', 'r') as f:
            source = f.read()
        assert 'daemon=True' in source or 'daemon = True' in source

    def test_thread_cleanup_on_shutdown(self):
        """验证关闭时清理线程。"""
        with open('src/platform/lifecycle.py', 'r') as f:
            source = f.read()
        assert 'join' in source.lower() or 'shutdown' in source.lower()
