"""CLI 版本自检与后台异步更新。

每次项目启动时，在后台 daemon 线程中检查三个 IM CLI 的版本，并在：
  - 未安装时自动安装（``install_cmd``）；
  - 有更新时自动升级（各 CLI 自带升级命令，不依赖 Homebrew）。

  - wecom-cli   企业微信 CLI         安装 ``npm i -g @wecom/cli``        升级 ``npm i -g @wecom/cli@latest``
  - lark-cli    飞书 Lark CLI        安装 ``npm i -g lark-cli``          升级 ``lark-cli update``
  - dws         钉钉 Workspace CLI   安装 ``npm i -g dingtalk-workspace-cli``  升级 ``dws upgrade -y``

检测逻辑：
  1. 用 ``shutil.which`` 解析 ``<cli>`` 二进制；未找到则（AUTO_INSTALL 开启时）执行 ``install_cmd`` 安装；
  2. 调 ``<cli> --version`` 解析当前安装版本；
  3. 用各 CLI 自带的 ``check_cmd`` 判断是否官方有更新（解析方式按 ``check_kind`` 区分）；
  4. 若官方有更新且 ``AUTO_UPDATE`` 开启，则在后台执行 ``update_cmd``。

``check_kind`` 三种：
  - ``"json"``：JSON 输出（lark-cli），对比 current/latest 或读 ``outdated`` 布尔；
  - ``"text"``：文本输出（dws），关键词命中表示有更新；
  - ``"npm"`` ：纯版本号输出（wecom-cli），与已装版本号对比。

设计原则：
  - 全程在后台线程执行，**绝不阻塞启动**；任何异常仅记日志，不影响主流程。
  - 结果写入 ``<data_root>/data/cli_versions.json``，便于排查与审计。
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from src.paths import data_path

logger = logging.getLogger(__name__)

# 是否自动执行更新命令。关闭后仅检测 + 记录 + 日志告警。
AUTO_UPDATE: bool = True

# 是否未安装时自动安装 CLI（执行 install_cmd）。关闭后未安装仅记录、不安装。
AUTO_INSTALL: bool = True

# 版本号正则：提取第一个 X.Y.Z
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")

# 文本检查类（dws 等）判定"有更新"的关键词
_UPDATE_KEYWORDS_RE = re.compile(
    r"可升级|有新版本|可更新|有更新|有可用|update available|newer version|outdated",
    re.IGNORECASE,
)


@dataclass
class CliSpec:
    name: str
    display: str
    version_args: list[str] = None  # type: ignore[assignment]
    check_cmd: Optional[list[str]] = None   # 判断官方是否有更新（各 CLI 自带命令）
    update_cmd: Optional[list[str]] = None  # 后台更新命令（各 CLI 自带命令）
    install_cmd: Optional[list[str]] = None  # 未安装时的安装命令（如 npm i -g ...）
    check_kind: str = "text"  # json | text | npm：check_cmd 输出的解析方式

    def __post_init__(self) -> None:
        if self.version_args is None:
            self.version_args = ["--version"]


# 三个 IM CLI 的默认定义。更新命令均为各 CLI 自带（不再走 brew）。
# dws 的 beta 通道：用户原话为 `dws upgrade --bate`，实测官方 flag 为 `--beta`；
# 此处使用稳定通道 `-y`（跳过交互确认）自动升级。
CLI_DEFINITIONS: dict[str, CliSpec] = {
    "wecom-cli": CliSpec(
        "wecom-cli", "企业微信 CLI",
        check_cmd=["npm", "view", "@wecom/cli", "version"],
        update_cmd=["npm", "install", "-g", "@wecom/cli@latest"],
        install_cmd=["npm", "install", "-g", "@wecom/cli"],
        check_kind="npm",
    ),
    "lark-cli": CliSpec(
        "lark-cli", "飞书 Lark CLI",
        check_cmd=["lark-cli", "update", "--check", "--json"],
        update_cmd=["lark-cli", "update"],
        install_cmd=["npm", "install", "-g", "lark-cli"],
        check_kind="json",
    ),
    "dws": CliSpec(
        "dws", "钉钉 Workspace CLI",
        check_cmd=["dws", "upgrade", "--check"],
        update_cmd=["dws", "upgrade", "-y"],
        install_cmd=["npm", "install", "-g", "dingtalk-workspace-cli"],
        check_kind="text",
    ),
}

# wecom-cli / lark-cli 的 fallback 路径（优先 shutil.which）。dws 走 PATH 即可。
_FALLBACK_PATHS: dict[str, list[str]] = {
    "wecom-cli": ["/opt/homebrew/bin/wecom-cli", "/usr/local/bin/wecom-cli"],
    "lark-cli": ["/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli"],
}


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _resolve_binary(name: str) -> Optional[str]:
    """解析 CLI 二进制路径：优先 PATH，其次常见 fallback 路径。"""
    found = shutil.which(name)
    if found:
        return found
    for p in _FALLBACK_PATHS.get(name, []):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _parse_version(text: str) -> Optional[tuple[int, int, int]]:
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    try:
        return tuple(int(x) for x in m.group(1).split("."))
    except ValueError as _exc:
        logger.debug(f"_parse_version: swallowed exception: {_exc}")
        return None


def fetch_version(spec: CliSpec, binary: str, timeout: int = 15) -> Optional[str]:
    """调 ``<cli> --version`` 解析并返回版本字符串（如 '0.1.9'），失败返回 None。"""
    try:
        r = subprocess.run([binary, *spec.version_args], capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("版本检测 %s 失败: %s", spec.name, e)
        return None
    v = _parse_version(out)
    return ".".join(map(str, v)) if v else None


def _run_cmd(cmd: Optional[list[str]], timeout: int) -> Optional[subprocess.CompletedProcess]:
    if not cmd:
        return None
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        logger.debug("命令执行失败 %s: %s", cmd, e)
        return None


def _has_upstream_update(spec: CliSpec, installed: str, timeout: int = 60) -> bool:
    """通过 check_cmd 判断官方是否有更新，解析方式按 check_kind 区分。"""
    if not spec.check_cmd:
        return False
    r = _run_cmd(spec.check_cmd, timeout)
    if r is None:
        return False
    blob = (r.stdout or "") + (r.stderr or "")

    if spec.check_kind == "npm":
        # npm view 返回纯版本号，与已装对比
        m = _VERSION_RE.search(blob)
        if not m:
            return False
        latest = _parse_version(m.group(1))
        cur = _parse_version(installed or "0.0.0")
        return bool(latest and cur and latest > cur)

    if spec.check_kind == "json":
        # lark-cli --json：对比 current/latest，或读 outdated 布尔
        try:
            data = json.loads(blob)
        except Exception as _exc:  # noqa: BLE001
            logger.debug(f"_has_upstream_update: swallowed exception: {_exc}")
            return _UPDATE_KEYWORDS_RE.search(blob) is not None
        cur = data.get("current") or data.get("installed") or data.get("version")
        lat = data.get("latest") or data.get("available") or data.get("target")
        if cur and lat and _parse_version(str(lat)) > _parse_version(str(cur)):
            return True
        for k in ("outdated", "updateAvailable", "hasUpdate", "needUpdate", "update_available"):
            if data.get(k):
                return True
        return False

    # text（dws 等）：关键词命中表示有更新
    return _UPDATE_KEYWORDS_RE.search(blob) is not None


def load_state(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as _exc:  # noqa: BLE001
        logger.warning(f"load_state: swallowed exception: {_exc}")
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _apply_update(spec: CliSpec, entry: dict, timeout: int = 300) -> None:
    """后台执行更新命令，并刷新安装版本。"""
    logger.info("[版本自检] 后台执行更新: %s", " ".join(spec.update_cmd or []))
    r = _run_cmd(spec.update_cmd, timeout)
    if r is None:
        entry["update_status"] = "error"
        return
    if r.returncode == 0:
        binary = _resolve_binary(spec.name)
        if binary:
            new_ver = fetch_version(spec, binary)
            if new_ver:
                entry["installed"] = new_ver
        entry["last_updated"] = _now()
        entry["update_status"] = "updated"
        logger.info("[版本自检] %s 更新完成 → %s", spec.display, entry.get("installed"))
    else:
        entry["update_status"] = "update_failed"
        logger.warning("[版本自检] %s 更新失败: %s",
                       spec.display, (r.stderr or r.stdout or "")[:300])


def _apply_install(spec: CliSpec, entry: dict, timeout: int = 300) -> None:
    """后台执行安装命令（install_cmd），装完重新解析版本。"""
    logger.info("[版本自检] 后台执行安装: %s", " ".join(spec.install_cmd or []))
    r = _run_cmd(spec.install_cmd, timeout)
    if r is None:
        entry["install_status"] = "error"
        return
    if r.returncode == 0:
        binary = _resolve_binary(spec.name)
        if binary:
            new_ver = fetch_version(spec, binary)
            if new_ver:
                entry["installed"] = new_ver
        entry["last_installed"] = _now()
        entry["install_status"] = "installed"
        logger.info("[版本自检] %s 安装完成 → %s", spec.display, entry.get("installed"))
    else:
        entry["install_status"] = "install_failed"
        logger.warning("[版本自检] %s 安装失败: %s",
                       spec.display, (r.stderr or r.stdout or "")[:300])


def run_checks(data_root: str | None = None) -> dict:
    """执行一次完整的版本自检 + 按需后台更新。返回各 CLI 的检测结果。

    state_path 走可重定位数据目录（src.paths.data_path），data_root 仅保留为兼容参数。
    """
    state_path = str(data_path("cli_versions.json"))
    state = load_state(state_path)
    result: dict[str, dict] = {}

    for name, spec in CLI_DEFINITIONS.items():
        binary = _resolve_binary(name)
        if not binary:
            # 未安装：AUTO_INSTALL 开启且有 install_cmd → 后台安装；否则仅记录
            if AUTO_INSTALL and spec.install_cmd:
                entry: dict = {"installed": None, "status": "not_installed",
                               "last_checked": _now()}
                logger.info("[版本自检] %s 未安装，尝试自动安装", spec.display)
                _apply_install(spec, entry)
                result[name] = entry
                state[name] = entry
                continue
            logger.info("[版本自检] %s 未安装，跳过（AUTO_INSTALL 关闭）", spec.display)
            result[name] = {"installed": None, "status": "not_installed"}
            continue

        installed = fetch_version(spec, binary)
        if not installed:
            logger.warning("[版本自检] %s 版本解析失败", spec.display)
            result[name] = {"installed": None, "status": "parse_error"}
            continue

        prev = state.get(name, {})
        prev_ver = prev.get("installed")
        entry: dict = {"installed": installed, "previous": prev_ver, "last_checked": _now()}

        upstream = _has_upstream_update(spec, installed)
        cur = _parse_version(installed) or (0, 0, 0)
        old = _parse_version(prev_ver or "0.0.0") or (0, 0, 0)
        changed = cur > old

        if upstream:
            logger.info("[版本自检] %s 发现官方更新可用（当前 %s）", spec.display, installed)
            entry["status"] = "update_available"
            if AUTO_UPDATE and spec.update_cmd:
                _apply_update(spec, entry)
        elif prev_ver is None:
            logger.info("[版本自检] %s 当前版本 %s（首次记录）", spec.display, installed)
            entry["status"] = "recorded"
        elif changed:
            logger.info("[版本自检] %s 版本已变化 %s → %s", spec.display, prev_ver, installed)
            entry["status"] = "changed"
        else:
            logger.info("[版本自检] %s 已是最新 %s", spec.display, installed)
            entry["status"] = "up_to_date"

        state[name] = entry
        result[name] = entry

    save_state(state_path, state)
    return result


def _safe_run(data_root: str) -> None:
    try:
        run_checks(data_root)
    except Exception:  # noqa: BLE001
        logger.warning("[版本自检] 异常（不影响启动）", exc_info=True)


def start_cli_version_check(data_root: str | None = None) -> None:
    """在后台 daemon 线程异步执行版本自检与更新，不阻塞启动。

    data_root 已废弃（state 落点统一走 src.paths 可重定位数据目录）。
    """
    t = threading.Thread(target=lambda: _safe_run(data_root), daemon=True, name="cli-version-check")
    t.start()
