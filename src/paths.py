"""统一可重定位路径解析（P0 路径改造）。

为什么需要它：
- 旧代码大量使用 cwd 相对路径（``./data/...``、``config.yaml``）或基于 ``__file__``
  推导的仓库根（``web/routers/sync.py`` 等）。装到只读目录（/opt、C:\\Program Files）
  或冻结成 PyInstaller 二进制（``sys._MEIPASS``）时会读不到/写不进，直接崩。
- 本模块把所有「运行时数据 / 配置 / 静态资源」的落点集中到一处，按以下优先级解析：

  data 目录（DB / 模型 / 备份 / PID / 配置副本 / 审计日志…）：
    1. ``--data-dir`` 覆盖（经 set_data_dir）；
    2. 开发态且 cwd 存在 ``data/`` 目录 → 沿用 cwd/data（保持旧部署与测试行为）；
    3. 其他（打包态 / 指定 HOME）→ ``<user_data_dir>/data``。

  config.yaml：
    1. ``--config`` 覆盖（经 set_config_path）；
    2. 开发态且 cwd 存在 ``config.yaml`` → 沿用；
    3. 打包态且 data 目录无 config → 从捆绑样例（_MEIPASS/config.yaml）拷贝一份，
       避免 Web 写回落进只读安装目录。

  静态资源（web/static、web/templates、内置 skills、config 样例）：
    - 开发态 = 仓库根；冻结态 = ``sys._MEIPASS``。只读，仅供读取。

所有 getter 均**不**主动建目录（避免在 import / 单元测试中意外在用户主目录落文件）；
真正启动进程时由 ``main()`` 调一次 ``ensure_runtime_dirs()`` 建好可写目录。

状态管理：覆盖为进程级一次性设置（启动 argv 传入），所有线程（主线程 / 轮询
daemon / Web uvicorn）必须看到同一份，故用模块级属性 + Lock 共享，而非
threading.local()（否则 daemon/Web 线程读不到 → 数据被劈到不同目录）。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger("linkora.paths")

APP_NAME = "linkora"
APP_AUTHOR = "Linkora"


class _PathOverrideState:
    """进程级路径覆盖状态（替代 global 变量）。

    覆盖来自启动 argv（--data-dir / --config），属进程级一次性设置。若用
    threading.local() 隔离，只有调用 set_* 的线程能看到，轮询 daemon / Web
    uvicorn 线程会读不到 → 各自回退到 cwd/data 或 user_data_dir/data，
    数据被劈成两份。故这里用普通实例属性 + Lock，保证所有线程读到同一份。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data_dir_override: str | None = None
        self._config_override: str | None = None

    def set_data_dir(self, path: str | None) -> None:
        """设置数据目录覆盖（--data-dir）。None 表示清除覆盖。"""
        with self._lock:
            self._data_dir_override = str(path) if path is not None else None

    def get_data_dir(self) -> str | None:
        """获取进程级数据目录覆盖。"""
        return self._data_dir_override

    def set_config_path(self, path: str | None) -> None:
        """设置配置文件覆盖（--config）。None 表示清除覆盖。"""
        with self._lock:
            self._config_override = str(path) if path is not None else None

    def get_config_path(self) -> str | None:
        """获取进程级配置文件覆盖。"""
        return self._config_override

    def clear(self) -> None:
        """清除所有覆盖（测试用）。"""
        with self._lock:
            self._data_dir_override = None
            self._config_override = None


# 全局单例状态（每个线程独立）
_path_state = _PathOverrideState()


def is_frozen() -> bool:
    """PyInstaller 冻结态：sys._MEIPASS 指向解包后的资源根目录。"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def get_app_root() -> Path:
    """资源根目录：开发态=仓库根；冻结态=_MEIPASS（含 web/、config.yaml 样例、内置 skills）。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", ""))
    # src/paths.py -> parents[1] = 仓库根（Linkora/）
    return Path(__file__).resolve().parents[1]


def set_data_dir(path: str | None) -> None:
    """设置数据目录覆盖（--data-dir）。None 表示清除覆盖。"""
    _path_state.set_data_dir(path)


def set_config_path(path: str | None) -> None:
    """设置配置文件覆盖（--config）。None 表示清除覆盖。"""
    _path_state.set_config_path(path)


def clear_path_overrides() -> None:
    """清除所有路径覆盖（测试用）。"""
    _path_state.clear()


def get_user_data_dir() -> Path:
    """跨平台用户数据目录（可读写，非安装目录）。"""
    env = os.environ.get("LINKORA_HOME")
    if env:
        return Path(env).expanduser()
    try:
        import platformdirs

        base = platformdirs.user_data_dir(APP_NAME, APP_AUTHOR, roaming=True)
    except Exception:  # noqa: BLE001
        # platformdirs 缺失时退化为 ~/.linkora
        base = os.path.join(os.path.expanduser("~"), f".{APP_NAME}")
    return Path(base)


def get_data_dir() -> Path:
    """可写数据目录。解析见模块 docstring，不主动建目录。"""
    override = _path_state.get_data_dir()
    if override:
        return Path(override).expanduser()
    cwd_data = Path.cwd() / "data"
    if not is_frozen() and cwd_data.is_dir():
        return cwd_data
    return get_user_data_dir() / "data"


def get_log_dir() -> Path:
    """可写日志目录。"""
    override = _path_state.get_data_dir()
    if override:
        return Path(override).expanduser() / "logs"
    cwd_logs = Path.cwd() / "logs"
    if not is_frozen() and cwd_logs.is_dir():
        return cwd_logs
    return get_user_data_dir() / "logs"


def data_path(*parts: str) -> Path:
    """拼出数据目录下的子路径，如 data_path('linkora.db') / data_path('skills', name)。"""
    return get_data_dir().joinpath(*parts)


def get_skills_root() -> Path:
    """SkillManager 期望的「项目根」：满足 ``<root>/data/skills`` == data_path('skills')。

    即数据目录的父目录（开发态=仓库根，打包态=用户数据目录）。
    """
    return get_data_dir().parent


def get_config_path() -> Path:
    """配置文件路径。解析见模块 docstring，必要时从捆绑样例拷贝（仅打包态）。"""
    override = _path_state.get_config_path()
    if override:
        return Path(override).expanduser()
    cwd_cfg = Path.cwd() / "config.yaml"
    if not is_frozen() and cwd_cfg.is_file():
        return cwd_cfg
    target = get_data_dir() / "config.yaml"
    if not target.exists():
        bundled = get_app_root() / "config.yaml"
        if bundled.is_file():
            try:
                import shutil

                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundled, target)
                logger.info("[启动] 已从捆绑样例生成配置文件: %s", target)
            except Exception as e:  # noqa: BLE001
                logger.warning("[启动] 拷贝样例配置失败（Web 写回可能失败）: %s", e)
    return target


def get_pid_file(mode: str = "both") -> Path:
    """单例锁 PID 文件路径（按进程分离模式分文件）。"""
    suffix = "" if mode == "both" else f".{mode}"
    return get_data_dir() / f"linkora{suffix}.pid"


def get_web_root() -> Path:
    return get_app_root() / "web"


def get_static_dir() -> Path:
    return get_web_root() / "static"


def get_templates_dir() -> Path:
    return get_web_root() / "templates"


def get_skill_icons_dir() -> Path:
    """SkillHub 技能图标本地缓存目录。"""
    return get_data_dir() / "skill_icons"


def ensure_runtime_dirs() -> None:
    """启动时调用一次：建好可写的数据目录与日志目录。"""
    for d in (get_data_dir(), get_log_dir(), get_skill_icons_dir()):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[启动] 创建目录失败 %s: %s", d, e)
