#!/usr/bin/env python3
"""备份文件自动清理脚本。

功能：
- 清理超过指定天数的数据库备份
- 清理超过指定天数的对话数据库
- 保留最近 N 个最新备份（即使超过天数也保留）
- 日志输出清理结果

使用示例：
    python3 scripts/cleanup_backups.py --dry-run  # 预览要删除的文件
    python3 scripts/cleanup_backups.py             # 执行清理
    python3 scripts/cleanup_backups.py --max-age-days 3  # 保留3天
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_backups(
    backup_dir: Path,
    max_age_days: int = 7,
    keep_min_count: int = 10,
) -> dict[str, int]:
    """清理过期的数据库备份文件。

    Args:
        backup_dir: 备份目录路径
        max_age_days: 最大保留天数
        keep_min_count: 最少保留备份数量（即使超过天数也保留）

    Returns:
        清理统计：{删除文件数: N, 释放空间MB: M}
    """
    if not backup_dir.exists():
        return {"files_deleted": 0, "space_freed_mb": 0}

    now = datetime.now()
    cutoff = now - timedelta(days=max_age_days)

    # 收集所有 .db 文件
    all_backups = sorted(
        backup_dir.glob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # 最新的在前
    )

    if len(all_backups) <= keep_min_count:
        logger.info("备份文件数量 (%d) 不超过最低保留数 (%d)，无需清理", len(all_backups), keep_min_count)
        return {"files_deleted": 0, "space_freed_mb": 0}

    # 标记可删除的备份（超过天数 且 不在保留列表）
    kept = set(all_backups[:keep_min_count])
    to_delete = [
        b for b in all_backups
        if b not in kept and b.stat().st_mtime < cutoff.timestamp()
    ]

    total_freed = 0
    for backup in to_delete:
        size_mb = backup.stat().st_size / 1024 / 1024
        try:
            backup.unlink()
            total_freed += size_mb
            logger.info("已删除过期备份: %s (%.2f MB)", backup.name, size_mb)
        except OSError as e:
            logger.warning("删除备份失败 %s: %s", backup.name, e)

    return {
        "files_deleted": len(to_delete),
        "space_freed_mb": round(total_freed, 2),
    }


def cleanup_conversations(
    conversations_dir: Path,
    max_age_days: int = 30,
    keep_min_count: int = 5,
) -> dict[str, int]:
    """清理过期的对话数据库文件。

    Args:
        conversations_dir: 对话数据库目录
        max_age_days: 最大保留天数
        keep_min_count: 最少保留数量

    Returns:
        清理统计
    """
    if not conversations_dir.exists():
        return {"files_deleted": 0, "space_freed_mb": 0}

    now = datetime.now()
    cutoff = now - timedelta(days=max_age_days)

    # 收集对话数据库
    all_dbs = sorted(
        conversations_dir.glob("*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if len(all_dbs) <= keep_min_count:
        return {"files_deleted": 0, "space_freed_mb": 0}

    # 标记可删除的对话数据库
    kept = set(all_dbs[:keep_min_count])
    to_delete = [
        db for db in all_dbs
        if db not in kept and db.stat().st_mtime < cutoff.timestamp()
    ]

    total_freed = 0
    for db in to_delete:
        size_mb = db.stat().st_size / 1024 / 1024
        try:
            db.unlink()
            total_freed += size_mb
            logger.info("已删除过期对话库: %s (%.2f MB)", db.name, size_mb)
        except OSError as e:
            logger.warning("删除对话库失败 %s: %s", db.name, e)

    return {
        "files_deleted": len(to_delete),
        "space_freed_mb": round(total_freed, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="清理过期的数据库备份和对话数据")
    parser.add_argument("--backup-dir", default="data/backups", help="备份目录路径")
    parser.add_argument("--conversation-dir", default="data/conversations", help="对话数据库目录")
    parser.add_argument("--max-age-days", type=int, default=7, help="备份最大保留天数")
    parser.add_argument("--conv-max-age-days", type=int, default=30, help="对话库最大保留天数")
    parser.add_argument("--keep-min", type=int, default=10, help="最少保留备份数量")
    parser.add_argument("--conv-keep-min", type=int, default=5, help="最少保留对话库数量")
    parser.add_argument("--dry-run", action="store_true", help="仅显示将要删除的文件，不实际删除")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    backup_dir = Path(args.backup_dir)
    conv_dir = Path(args.conversation_dir)

    if args.dry_run:
        logger.info("=== 干跑模式 ===")
        logger.info("备份目录: %s", backup_dir)
        logger.info("对话目录: %s", conv_dir)

        # 显示将要删除的文件
        now = datetime.now()
        cutoff = now - timedelta(days=args.max_age_days)
        backups = sorted(backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        old_backups = [b for b in backups[args.keep_min:] if b.stat().st_mtime < cutoff.timestamp()]
        for b in old_backups:
            age = (now.timestamp() - b.stat().st_mtime) / 86400
            logger.info("  [将删除] %s (%.1f MB, %.0f 天前)", b.name, b.stat().st_size/1024/1024, age)

        cutoff_conv = now - timedelta(days=args.conv_max_age_days)
        convs = sorted(conv_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        old_convs = [c for c in convs[args.conv_keep_min:] if c.stat().st_mtime < cutoff_conv.timestamp()]
        for c in old_convs:
            age = (now.timestamp() - c.stat().st_mtime) / 86400
            logger.info("  [将删除] %s (%.1f MB, %.0f 天前)", c.name, c.stat().st_size/1024/1024, age)

        return

    # 执行清理
    logger.info("=== 开始清理 ===")
    logger.info("备份目录: %s", backup_dir)
    logger.info("对话目录: %s", conv_dir)

    backup_result = cleanup_backups(
        backup_dir,
        max_age_days=args.max_age_days,
        keep_min_count=args.keep_min,
    )
    logger.info("备份清理结果: 删除 %d 个文件，释放 %.2f MB",
                backup_result["files_deleted"], backup_result["space_freed_mb"])

    conv_result = cleanup_conversations(
        conv_dir,
        max_age_days=args.conv_max_age_days,
        keep_min_count=args.conv_keep_min,
    )
    logger.info("对话库清理结果: 删除 %d 个文件，释放 %.2f MB",
                conv_result["files_deleted"], conv_result["space_freed_mb"])

    total_freed = backup_result["space_freed_mb"] + conv_result["space_freed_mb"]
    logger.info("=== 清理完成 ===")
    logger.info("总共释放空间: %.2f MB", total_freed)


if __name__ == "__main__":
    main()
