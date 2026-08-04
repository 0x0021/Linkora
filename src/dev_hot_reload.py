"""开发态 Python 模块热加载器。

监视指定源码目录中的 .py 文件变更，对「安全模块」执行 importlib.reload()，
并通过回调钩子让调用方修复 reload 后丢失的注册关系（如工具重新注册）。

设计原则：
- 仅用于开发环境（config.dev.module_hot_reload=True），生产默认关闭。
- 只reload「无持久实例状态」的模块（tools、纯函数、常量）。
- 不尝试 reload 有单例/线程局部状态的模块（agent/router/platform），
  那类变更仍需重启 bot。
- 每个 module 只 reload 一次（同一轮变更内去重），避免级联副作用。
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# 默认轮询间隔（秒）
_DEFAULT_POLL_INTERVAL = 5

# 「安全」可热重载的模块前缀白名单——只有匹配的 .py 变更才触发 reload。
# 这些模块的特点：每次调用重新实例化（tools）、或纯函数/常量（style），
# 不持有跨请求的单例状态。
_SAFE_MODULE_PREFIXES = (
    "src.tools.",
    "src.llm.style",
    "src.llm.rag_inject",
    "src.llm.prompt_builder",
    "src.llm.system_prompt",
)


class ModuleHotReloader:
    """开发态模块热加载器。

    用法：
        reloader = ModuleHotReloader(project_root="/path/to/linkora")
        reloader.register_post_reload_callback("rebuild_tools", my_rebuild_fn)
        reloader.start_watcher()
        ... 修改 src/tools/weather.py ...
        # ~5s 内自动 reload(src.tools.weather) 并调用 my_rebuild_fn
        reloader.stop_watcher()
    """

    def __init__(
        self,
        project_root: str | Path,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        enabled: bool = False,
    ):
        self._root = Path(project_root).resolve()
        self._poll_interval = poll_interval
        self._enabled = enabled
        # 变更指纹：module_name -> last_reload_mtime
        self._mtimes: dict[str, float] = {}
        self._lock = threading.Lock()
        # Watcher 线程
        self._watcher_thread: threading.Thread | None = None
        self._watcher_stop = threading.Event()
        # Post-reload 回调列表：name -> callable
        # reload 成功后按注册顺序依次调用，用于修复丢失的注册关系
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        # 统计
        self._stats = {"total_reloads": 0, "total_errors": 0}

    # ── 回调注册 ─────────────────────────────────────────────

    def register_post_reload_callback(self, name: str, fn: Callable[[], None]) -> None:
        """注册 reload 后执行的回调（如重建工具注册表）。

        同名回调会替换旧的（而非追加），保证幂等。
        """
        with self._lock:
            # 移除同名旧回调
            self._callbacks = [(n, f) for n, f in self._callbacks if n != name]
            self._callbacks.append((name, fn))
            logger.debug("[ModuleHotReload] 注册回调: %s", name)

    def unregister_post_reload_callback(self, name: str) -> None:
        with self._lock:
            self._callbacks = [(n, f) for n, f in self._callbacks if n != name]

    # ── Watcher 生命周期 ─────────────────────────────────────

    def start_watcher(self) -> None:
        if not self._enabled:
            logger.info("[ModuleHotReload] 未启用（dev.module_hot_reload=False），跳过")
            return
        if self._watcher_thread and self._watcher_thread.is_alive():
            return

        self._watcher_stop.clear()
        t = threading.Thread(
            target=self._watch_loop,
            name="ModuleHotReloader",
            daemon=True,
        )
        t.start()
        self._watcher_thread = t
        logger.info(
            "[ModuleHotReloader] 已启动，轮询间隔 %.1fs，监控范围: src/",
            self._poll_interval,
        )

    def stop_watcher(self) -> None:
        self._watcher_stop.set()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=5)
        logger.info("[ModuleHotReloader] 已停止")

    # ── 核心逻辑 ─────────────────────────────────────────────

    def _watch_loop(self) -> None:
        """后台轮询循环。"""
        while not self._watcher_stop.is_set():
            try:
                self._watcher_stop.wait(timeout=self._poll_interval)
                if self._watcher_stop.is_set():
                    break
                self._scan_and_reload()
            except Exception as e:
                logger.warning("[ModuleHotReload] 轮询异常: %s", e)

    def _scan_and_reload(self) -> None:
        """扫描 src/ 下 .py 文件，对变更的安全模块执行 reload。"""
        src_dir = self._root / "src"
        if not src_dir.is_dir():
            return

        changed_modules: list[str] = []
        now = time.time()

        for py_file in sorted(src_dir.rglob("*.py")):
            # 跳过 __pycache__ / dist / node_modules 等
            if any(p in py_file.parts for p in ("__pycache__", ".pyc", "dist", "node_modules")):
                continue

            try:
                mtime = py_file.stat().st_mtime
            except OSError:
                continue

            # 转为模块名（如 src/tools/weather.py -> src.tools.weather）
            rel = py_file.relative_to(self._root)
            module_name = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")

            # 检查是否在安全白名单中
            if not any(module_name.startswith(prefix) for prefix in _SAFE_MODULE_PREFIXES):
                continue

            # 检查是否有变更（1s 容差防抖）
            last_mtime = self._mtimes.get(module_name, 0)
            if mtime - last_mtime < 1.0:
                continue

            # 记录新 mtime（无论 reload 是否成功，避免重复尝试）
            self._mtimes[module_name] = mtime
            changed_modules.append(module_name)

        if not changed_modules:
            return

        # 执行 reload
        for mod_name in changed_modules:
            self._do_reload(mod_name)

    def _do_reload(self, module_name: str) -> None:
        """对单个模块执行 importlib.reload() 并触发回调。"""
        try:
            # 检查模块是否已被导入
            if module_name not in sys.modules:
                logger.debug("[ModuleHotReload] %s 尚未导入，跳过", module_name)
                return

            old_module = sys.modules[module_name]
            logger.info("[ModuleHotReloader] 正在 reload: %s", module_name)

            # 执行 reload
            new_module = importlib.reload(old_module)

            self._stats["total_reloads"] += 1
            logger.info(
                "[ModuleHotReload] ✅ reload 成功: %s (累计 %d 次)",
                module_name,
                self._stats["total_reloads"],
            )

            # 触发 post-reload 回调
            self._fire_callbacks(module_name)

        except Exception as e:
            self._stats["total_errors"] += 1
            logger.error(
                "[ModuleHotReload] ❌ reload 失败: %s — %s",
                module_name,
                e,
                exc_info=False,
            )

    def _fire_callbacks(self, reloaded_module: str) -> None:
        """按注册顺序触发所有 post-reload 回调。"""
        with self._lock:
            callbacks = list(self._callbacks)

        for name, fn in callbacks:
            try:
                fn()
                logger.debug(
                    "[ModuleHotReload] 回调执行成功: %s (触发模块: %s)",
                    name,
                    reloaded_module,
                )
            except Exception as e:
                logger.error(
                    "[ModuleHotReload] 回调执行失败: %s — %s",
                    name,
                    e,
                    exc_info=False,
                )

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    @property
    def enabled(self) -> bool:
        return self._enabled


# 延迟导入 sys（避免循环依赖 / 启动时开销）
import sys  # noqa: E402
