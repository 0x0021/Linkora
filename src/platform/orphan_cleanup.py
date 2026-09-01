"""启动期孤儿会话库检测与 ``tmp_images`` 回收（D4）。

``data/conversations/`` 下可能存在不再绑定任何活跃平台的遗留分库（账号解绑 /
重装 / 迁移残留）。这些库的 ``messages`` 表仍引用 ``data/tmp_images`` 下的图片，
但活跃清理链路（按 ``ctx.store``）扫不到它们，图片成孤儿永久累积。

本模块在启动期一次性扫描并回收这部分孤儿图片：
- 跳过活跃平台库（``db_path`` ∈ ``active_db_paths``）与备份类文件；
- 对每个孤儿库只读打开，取 ``messages.image_path`` 引用的本地图片，按真实
  ``tmp_images`` 根回收；
- **不删除孤儿库本身**（保留设计决策，待显式处理）；
- 单库异常不影响其余库。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# 备份/遗留类文件名关键词：不对其做 tmp_images 回收，避免影响可恢复备份。
_BACKUP_TOKENS = ("bak", "backup", ".bak_", "_pre_cleanup", "_full_bak")


def _is_backup_name(name: str) -> bool:
    low = name.lower()
    return any(tok in low for tok in _BACKUP_TOKENS)


def collect_orphan_image_paths(db_path: Path) -> list[str]:
    """只读打开孤儿库，收集 ``messages`` 表引用的本地图片相对路径（去重、排序）。

    库可能损坏或表结构不同，任何错误都降级返回空列表（不阻断主流程）。
    """
    rels: set[str] = set()
    conn = None
    try:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except (sqlite3.Error, OSError):
            conn = sqlite3.connect(str(db_path))
        cur = conn.execute(
            "SELECT DISTINCT image_path FROM messages "
            "WHERE image_path IS NOT NULL AND image_path <> ''"
        )
        for (val,) in cur.fetchall():
            if val:
                rels.add(val)
    except (sqlite3.Error, OSError) as e:
        logger.debug("读孤儿库 messages 失败（表可能不存在）: %s | %s", db_path.name, e)
    finally:
        if conn is not None:
            conn.close()
    return sorted(rels)


def scan_and_reclaim_orphan_tmp_images(
    conversations_dir: str | Path,
    active_db_paths: set[str],
    tmp_images_root: str | Path,
) -> tuple[list[str], int]:
    """扫描 ``conversations_dir``，回收孤儿库引用的 ``tmp_images``。

    返回 ``(孤儿库文件名列表, 回收文件数)``。

    - 活跃库（``db_path`` ∈ ``active_db_paths``，比较时均 resolve）跳过；
    - 备份类文件名跳过；
    - 每个孤儿库只读收集 ``messages.image_path``，按 ``tmp_images_root`` 回收；
    - 不删除孤儿库本体。单库异常仅告警并跳过。
    """
    from src.memory.image_cleanup import purge_orphan_images

    conv_dir = Path(conversations_dir)
    if not conv_dir.exists():
        return [], 0
    active = {Path(p).resolve() for p in active_db_paths}
    orphan_names: list[str] = []
    reclaimed = 0
    tmp_root = str(tmp_images_root)

    for db_file in sorted(conv_dir.glob("*.db")):
        try:
            rp = db_file.resolve()
            if rp in active:
                continue
            if _is_backup_name(db_file.name):
                continue
            orphan_names.append(db_file.name)
            rels = collect_orphan_image_paths(rp)
            if rels:
                reclaimed += purge_orphan_images(str(rp), rels, base_dir=tmp_root)
        except Exception as e:  # noqa: BLE001
            logger.warning("孤儿库扫描处理 %s 失败（已跳过）: %s", db_file.name, e)

    return orphan_names, reclaimed
