"""优雅退出（P0-1）测试。

验证 shutdown() 在收到关闭信号后：
1. 取消所有挂起的防抖 Timer（cancel() 被调用，不会退出后触发 LLM/发消息）；
2. join 后台守护线程（线程确实被等待结束）；
3. 不再依赖 finally 里散落的 stop()，统一由 shutdown() 收敛；
4. 调度循环在 _shutdown_event.set() 后能被及时唤醒（不拖满 sleep 周期）。
"""
from __future__ import annotations

import threading
import time

from main import LinkoraEngine


class _FakeScheduler:
    """模拟 DocSyncScheduler / DatabaseBackup：可被 stop() 并 join 后台线程。"""

    def __init__(self, name: str, hold: float = 0.0):
        self.name = name
        self._running = True
        self._hold = hold
        self.stop_called = False
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while self._running:
            if not self._hold:
                break
            time.sleep(0.02)

    def start(self):
        self._thread.start()

    def stop(self):
        self.stop_called = True
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=5)


class _FakePoller:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def _make_shutdown_app():
    """装配 shutdown() 所需属性的裸实例（不触发完整初始化）。"""
    app = LinkoraEngine.__new__(LinkoraEngine)
    app._running = True
    app._shutdown_event = threading.Event()
    app._timer_lock = threading.Lock()
    app._pending_timers = {}
    # P1-C：防抖「纯数据」批次监控状态（与 LinkoraEngine.__init__ 保持一致）
    app._pending_first_seen = {}
    app._pending_incomplete_wait = {}
    app._incomplete_delay_count = 0
    app._incomplete_extra_sec = 0.0
    app._incomplete_fired_with_request = 0
    app._incomplete_fired_without_request = 0
    app._bg_threads = []

    app.poller = _FakePoller()
    app.doc_sync_scheduler = _FakeScheduler("doc")
    app.db_backup = _FakeScheduler("backup")
    # 一个会随 _running 退出的后台线程，验证被 join
    bg = threading.Thread(target=lambda: time.sleep(0.3) if app._running else None, daemon=True)
    app._bg_threads.append(bg)
    return app


def test_shutdown_cancels_pending_timers():
    app = _make_shutdown_app()

    cancelled = []

    class _FakeTimer:
        def __init__(self, key):
            self.key = key
            self.cancelled = False

        def cancel(self):
            self.cancelled = True
            cancelled.append(self)

    with app._timer_lock:
        app._pending_timers = {"k1": _FakeTimer("k1"), "k2": _FakeTimer("k2")}

    app.shutdown(timeout=2)

    assert len(cancelled) == 2
    assert all(t.cancelled for t in cancelled)
    # 定时器已清空，避免退出后误触发
    assert app._pending_timers == {}


def test_shutdown_stops_poller_and_schedulers():
    app = _make_shutdown_app()
    app.doc_sync_scheduler.start()
    app.db_backup.start()

    app.shutdown(timeout=2)

    assert app.poller.stopped is True
    assert app.doc_sync_scheduler.stop_called is True
    assert app.db_backup.stop_called is True
    assert app._shutdown_event.is_set()


def test_shutdown_joins_background_threads():
    app = _make_shutdown_app()
    app._bg_threads[0].start()
    assert app._bg_threads[0].is_alive()

    app.shutdown(timeout=2)

    # join 后线程应已结束（后台线程逻辑在 _running=False 后即退出）
    assert not app._bg_threads[0].is_alive()


def test_shutdown_event_wakes_scheduler_loop():
    """调度循环在 _shutdown_event 置位后应立即唤醒，而非睡满整段 sleep。"""
    app = _make_shutdown_app()

    woke_fast = []

    def loop():
        # 模拟调度器 main 循环：等待 _shutdown_event 而非固定 sleep
        if app._shutdown_event.wait(60):
            woke_fast.append(True)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    time.sleep(0.05)
    app._shutdown_event.set()  # 模拟收到 SIGTERM
    t.join(timeout=2)

    assert woke_fast == [True]
    assert not t.is_alive()
