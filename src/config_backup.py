"""配置文件每日滚动备份（启动触发 + 仅变更才备份）。

设计要点
--------
- 触发方式：在应用启动时调用 :func:`maybe_backup`（bot 的 ``lifecycle.main``
  与 web 后台的 ``run_web`` 均已挂载）。**不再依赖固定时间的定时任务**。
- 双重门禁（每天至多一次 + 仅变更才落盘）：
  1. 今天已有备份文件（``config_daily_YYYYMMDD.yaml``）则直接跳过 —— 保证每天至多一份。
  2. 当前 ``config.yaml`` 内容与「最近一次备份」逐字节相同则跳过 —— 无变化不浪费副本。
- 目录：``data/config-daily-backups/``，文件名 ``config_daily_YYYYMMDD.yaml``。
  该目录已被 ``.gitignore`` 忽略，永远不会被提交到 git；``config.yaml`` 本身同样忽略。
- 滚动保留：按修改时间倒序保留最近的 ``KEEP`` 份（默认 16），超出自动删除。
- 原子写入：先写临时文件再 ``os.replace``，避免备份写到一半被读取或进程中断产生半截文件。
- 零依赖：仅用标准库，不解析 YAML、不改动原文件，只做文件级副本 + 内容比对。
- 容错：源文件缺失时跳过（返回 False），不抛错中断应用启动。
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
import tempfile

# ----------------------------- 配置（可按需调整） -----------------------------
KEEP = 16                 # 滚动保留份数（最近的 N 天）
PREFIX = "config_daily_"  # 每日备份文件名前缀
# src/config_backup.py -> dirname(Linkora/src) -> dirname(Linkora) = 仓库根
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "config.yaml")
DEST_DIR = os.path.join(REPO_ROOT, "data", "config-daily-backups")
# -----------------------------------------------------------------------------


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _latest_backup(dest_dir: str, prefix: str) -> str | None:
    """返回目录中最新的一份备份（按修改时间倒序），无则 None。"""
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return None
    cands = [
        os.path.join(dest_dir, n)
        for n in names
        if n.startswith(prefix) and n.endswith(".yaml") and os.path.isfile(os.path.join(dest_dir, n))
    ]
    if not cands:
        return None
    cands.sort(key=os.path.getmtime, reverse=True)
    return cands[0]


def _atomic_copy(src: str, dst: str) -> None:
    """先写临时文件，再原子替换（含覆盖当天已有文件）。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst), prefix=os.path.basename(dst) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fout, open(src, "rb") as fin:
            while True:
                chunk = fin.read(1 << 20)
                if not chunk:
                    break
                fout.write(chunk)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(dest_dir: str, prefix: str, keep: int) -> int:
    """删除超出保留份数的旧备份，返回删除数量。"""
    try:
        names = os.listdir(dest_dir)
    except OSError:
        return 0
    files = [
        os.path.join(dest_dir, n)
        for n in names
        if n.startswith(prefix) and n.endswith(".yaml") and os.path.isfile(os.path.join(dest_dir, n))
    ]
    files.sort(key=os.path.getmtime, reverse=True)
    removed = 0
    for old in files[keep:]:
        try:
            os.remove(old)
            removed += 1
        except OSError:
            pass
    return removed


def maybe_backup(
    src: str = SRC,
    dest_dir: str = DEST_DIR,
    keep: int = KEEP,
    prefix: str = PREFIX,
) -> bool:
    """启动触发：每天至多一次、且仅当配置相较最近备份有变化才落盘。

    返回 True 表示实际生成了新备份，False 表示被门禁拦截或源缺失。
    """
    if not os.path.isfile(src):
        print(f"[{_now()}] [config-backup] 源配置文件不存在，跳过：{src}", flush=True)
        return False

    os.makedirs(dest_dir, exist_ok=True)
    today = _dt.date.today().strftime("%Y%m%d")
    today_path = os.path.join(dest_dir, f"{prefix}{today}.yaml")

    # 门禁 1：今天已备份过 -> 每天至多一份（但仍执行滚动清理，保证上限始终生效）
    if os.path.isfile(today_path):
        _prune(dest_dir, prefix, keep)
        return False

    cur = _read_bytes(src)
    if cur is None:
        return False

    # 门禁 2：与最近一次备份内容相同 -> 无变化不备份（且不创建当天文件，
    # 以便同一天内后续「有变化」的启动仍能触发当日首份备份）
    latest = _latest_backup(dest_dir, prefix)
    if latest is not None and _read_bytes(latest) == cur:
        _prune(dest_dir, prefix, keep)
        return False

    _atomic_copy(src, today_path)
    removed = _prune(dest_dir, prefix, keep)
    print(
        f"[{_now()}] [config-backup] 已备份 -> {today_path} "
        f"({os.path.getsize(today_path)} bytes)，保留 {keep} 份/清理 {removed} 份",
        flush=True,
    )
    return True


def main() -> int:
    maybe_backup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
