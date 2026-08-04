"""生命周期 Mixin 单测。

覆盖：shutdown 流程、main 入口参数解析。
"""

from __future__ import annotations

import os

import pytest
from unittest.mock import MagicMock, patch

from src.platform.lifecycle import LifecycleMixin, main


class FakeLifecycle(LifecycleMixin):
    """模拟 LifecycleMixin 的最小依赖。"""

    def __init__(self):
        self._running = True
        self._shutdown_event = MagicMock()
        self._timer_lock = MagicMock()
        self._pending_timers = {}
        self._pending_first_seen = {}
        self._pending_incomplete_wait = {}
        self._bg_threads = []
        self.doc_sync_scheduler = MagicMock()
        self.db_backup = MagicMock()
        self.memory_cleanup_scheduler = MagicMock()
        self.conversation_summary_scheduler = MagicMock()
        self.platforms = {}
        self.monitor = None
        self.store = MagicMock()


@pytest.fixture
def lc():
    lt = FakeLifecycle()
    lt._pending_timers["k"] = MagicMock()
    return lt


# ---- shutdown ----

def test_shutdown_sets_running_false(lc):
    lc.shutdown(timeout=5)
    assert not lc._running


def test_shutdown_stops_all_timers(lc):
    lc.shutdown(timeout=5)
    assert len(lc._pending_timers) == 0


def test_shutdown_stops_schedulers(lc):
    lc.shutdown(timeout=5)
    lc.doc_sync_scheduler.stop.assert_called_once()
    lc.db_backup.stop.assert_called_once()


def test_shutdown_with_schedulers_missing():
    lc2 = FakeLifecycle()
    lc2.doc_sync_scheduler = None
    lc2.db_backup = None
    lc2.memory_cleanup_scheduler = None
    lc2.conversation_summary_scheduler = None
    lc2.shutdown(timeout=5)


def test_shutdown_no_pending_timers():
    lc2 = FakeLifecycle()
    lc2._pending_timers = {}
    lc2.shutdown(timeout=5)


# ---- main 入口 ----
# LinkoraEngine 是懒导入（from .core import LinkoraEngine），需 patch src.platform.core

@patch("os.path.exists", return_value=False)  # 跳过 PID 单例检查，消除跨运行/并发 PID 竞态（P0 可重定位后 PID 落到真实 data 目录）
@patch("src.platform.core.LinkoraEngine")
@patch("src.platform.lifecycle.load_config")
def test_main_default_paths(mock_load, mock_ai, mock_exists):
    mock_config = MagicMock()
    mock_config.web.port = 8888
    mock_config.poller.enabled = False
    mock_config.pollers = {}
    mock_load.return_value = mock_config
    mock_ai.return_value.run = MagicMock(side_effect=SystemExit(0))

    # main() 会写单例 PID 锁到 {root}/data/linkora.pid。测试用固定 root，
    # 若上次运行（或本环境其他进程）残留 PID 文件且 PID 恰被复用为活进程，
    # 会误判「已有实例」而 exit(1)。清理自身 PID 文件，保证测试幂等、不被跨运行污染。
    pid_file = os.path.join("/tmp/fake_project", "data", "linkora.pid")
    if os.path.exists(pid_file):
        os.remove(pid_file)
    try:
        with pytest.raises(SystemExit, match="0"):
            main("/tmp/fake_project")
    finally:
        if os.path.exists(pid_file):
            os.remove(pid_file)

    mock_load.assert_called_once()
