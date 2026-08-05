"""开发态模块热加载器单元测试。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock


from src.dev_hot_reload import ModuleHotReloader, _SAFE_MODULE_PREFIXES


class TestSafeModulePrefixes:
    def test_tools_in_whitelist(self):
        assert any("src.tools." == p for p in _SAFE_MODULE_PREFIXES)

    def test_style_in_whitelist(self):
        assert any("src.llm.style" == p for p in _SAFE_MODULE_PREFIXES)

    def test_agent_not_in_whitelist(self):
        # agent 有单例/线程局部状态，不应被自动 reload
        assert not any("src.llm.agent".startswith(p) for p in _SAFE_MODULE_PREFIXES)

    def test_platform_not_in_whitelist(self):
        assert not any("src.platform.".startswith(p) for p in _SAFE_MODULE_PREFIXES)


class TestModuleHotReloaderInit:
    def test_default_disabled(self, tmp_path):
        r = ModuleHotReloader(tmp_path)
        assert not r.enabled
        assert r._poll_interval == 5

    def test_enabled_flag(self, tmp_path):
        r = ModuleHotReloader(tmp_path, enabled=True)
        assert r.enabled

    def test_custom_interval(self, tmp_path):
        r = ModuleHotReloader(tmp_path, poll_interval=2.0)
        assert r._poll_interval == 2.0


class TestCallbackRegistration:
    def test_register_and_fire(self, tmp_path):
        r = ModuleHotReloader(tmp_path)
        calls = []
        r.register_post_reload_callback("test_cb", lambda: calls.append(1))
        r._fire_callbacks("src.tools.weather")
        assert calls == [1]

    def test_replace_same_name(self, tmp_path):
        r = ModuleHotReloader(tmp_path)
        calls = []
        r.register_post_reload_callback("test_cb", lambda: calls.append("old"))
        r.register_post_reload_callback("test_cb", lambda: calls.append("new"))
        r._fire_callbacks("src.tools.weather")
        assert calls == ["new"]  # 旧回调被替换

    def test_unregister(self, tmp_path):
        r = ModuleHotReloader(tmp_path)
        calls = []
        r.register_post_reload_callback("test_cb", lambda: calls.append(1))
        r.unregister_post_reload_callback("test_cb")
        r._fire_callbacks("src.tools.weather")
        assert calls == []

    def test_callback_error_doesnt_break_others(self, tmp_path):
        r = ModuleHotReloader(tmp_path)
        calls = []
        r.register_post_reload_callback("bad", lambda: (_ for _ in ()).throw(ValueError("boom")))
        r.register_post_reload_callback("good", lambda: calls.append(1))
        r._fire_callbacks("src.tools.weather")
        assert calls == [1]  # good callback still fired


class TestWatcherLifecycle:
    def test_start_and_stop(self, tmp_path):
        r = ModuleHotReloader(tmp_path, poll_interval=0.1, enabled=True)
        r.start_watcher()
        assert r._watcher_thread is not None
        assert r._watcher_thread.is_alive()
        r.stop_watcher()
        time.sleep(0.2)
        assert not r._watcher_thread.is_alive()

    def test_start_when_disabled_is_noop(self, tmp_path):
        r = ModuleHotReloader(tmp_path, enabled=False)
        r.start_watcher()  # 不应崩溃
        assert r._watcher_thread is None

    def test_stop_without_start(self, tmp_path):
        r = ModuleHotReloader(tmp_path)
        r.stop_watcher()  # 不应崩溃

    def test_double_start_noop(self, tmp_path):
        r = ModuleHotReloader(tmp_path, poll_interval=0.1, enabled=True)
        r.start_watcher()
        t1 = r._watcher_thread
        r.start_watcher()  # 不应创建第二个线程
        assert r._watcher_thread is t1
        r.stop_watcher()


class TestScanAndReload:
    """测试变更检测与 reload 逻辑（mock importlib.reload）。"""

    def _make_safe_file(self, tmp_path, relpath) -> Path:
        """在 tmp_path/src/ 下创建一个 .py 文件，返回其路径。"""
        f = tmp_path / "src" / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# test\nx = 1\n", encoding="utf-8")
        return f

    def test_no_change_no_reload(self, tmp_path):
        """无变更时不触发 reload。"""
        # 用 phantom 模块名（zz_ 前缀，项目不存在），避免与真实 src.tools.weather
        # 重名：全量测试顺序下真实模块已被前面测试 import 进 sys.modules，
        # 会让 _do_reload 误判「已导入」而 reload（其他测试显式 mock sys.modules，
        # 本测试依赖模块未导入的隐式假设，须用不存在的模块名保持隔离）。
        r = ModuleHotReloader(tmp_path, enabled=True)
        self._make_safe_file(tmp_path, "tools/zz_phantom_tool.py")
        # 建立基线指纹
        r._scan_and_reload()
        assert r._stats["total_reloads"] == 0

        # 短暂等待后再次扫描（无变更）
        time.sleep(0.2)
        r._scan_and_reload()
        assert r._stats["total_reloads"] == 0

    def test_change_triggers_reload(self, tmp_path):
        """.py 变更触发 reload。"""
        r = ModuleHotReloader(tmp_path, enabled=True)
        f = self._make_safe_file(tmp_path, "tools/weather.py")
        r._scan_and_reload()  # 基线

        # 修改文件（跨秒确保 mtime 变化）
        time.sleep(1.1)
        f.write_text("# updated\nx = 2\n", encoding="utf-8")

        with mock.patch("src.dev_hot_reload.importlib") as mock_importlib:
            mock_importlib.reload.return_value = mock.MagicMock()
            # 模拟模块已导入
            with mock.patch.dict(sys.modules, {"src.tools.weather": mock.MagicMock()}):
                r._scan_and_reload()

        assert r._stats["total_reloads"] >= 1

    def test_unsafe_module_ignored(self, tmp_path):
        """非安全前缀的模块变更不触发 reload。"""
        r = ModuleHotReloader(tmp_path, enabled=True)
        f = self._make_safe_file(tmp_path, "platform/runtime_lifecycle.py")
        r._scan_and_reload()  # 基线

        time.sleep(1.1)
        f.write_text("# updated\n", encoding="utf-8")

        with mock.patch("src.dev_hot_reload.importlib") as mock_importlib:
            r._scan_and_reload()

        # platform 不在白名单，不应 reload
        mock_importlib.reload.assert_not_called()
        assert r._stats["total_reloads"] == 0

    def test_not_yet_imported_skipped(self, tmp_path):
        """尚未导入的模块跳过 reload（避免不必要的 import）。"""
        r = ModuleHotReloader(tmp_path, enabled=True)
        f = self._make_safe_file(tmp_path, "tools/new_tool.py")
        r._scan_and_reload()  # 基线

        time.sleep(1.1)
        f.write_text("# updated\n", encoding="utf-8")

        # 确保 sys.modules 中没有这个模块
        mod_name = "src.tools.new_tool"
        if mod_name in sys.modules:
            del sys.modules[mod_name]

        with mock.patch("src.dev_hot_reload.importlib") as mock_importlib:
            r._scan_and_reload()

        mock_importlib.reload.assert_not_called()

    def test_pycache_skipped(self, tmp_path):
        """__pycache__ 中的 .pyc 变更不触发 reload。"""
        r = ModuleHotReloader(tmp_path, enabled=True)
        pyc_dir = tmp_path / "src" / "tools" / "__pycache__"
        pyc_dir.mkdir(parents=True)
        (pyc_dir / "weather.cpython-314.pyc").write_text("fake", encoding="utf-8")

        r._scan_and_reload()
        assert r._stats["total_reloads"] == 0


class TestDoReload:
    def test_successful_reload_fires_callbacks(self, tmp_path):
        r = ModuleHotReloader(tmp_path, enabled=True)
        cb_calls = []
        r.register_post_reload_callback("test", lambda: cb_calls.append(1))

        fake_module = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"src.tools.weather": fake_module}):
            with mock.patch("src.dev_hot_reload.importlib") as mock_il:
                mock_il.reload.return_value = fake_module
                r._do_reload("src.tools.weather")

        assert r._stats["total_reloads"] == 1
        assert cb_calls == [1]

    def test_failed_reload_counts_error(self, tmp_path):
        r = ModuleHotReloader(tmp_path, enabled=True)
        fake_module = mock.MagicMock()

        with mock.patch.dict(sys.modules, {"src.tools.weather": fake_module}):
            with mock.patch("src.dev_hot_reload.importlib") as mock_il:
                mock_il.reload.side_effect = RuntimeError("boom")
                r._do_reload("src.tools.weather")

        assert r._stats["total_errors"] == 1
        assert r._stats["total_reloads"] == 0

    def test_reload_missing_module_skipped(self, tmp_path):
        r = ModuleHotReloader(tmp_path, enabled=True)
        # 模块不在 sys.modules 中
        r._do_reload("src.tools.nonexistent")
        assert r._stats["total_reloads"] == 0
