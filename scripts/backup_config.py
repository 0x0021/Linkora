#!/usr/bin/env python3
"""配置文件每日滚动备份 —— 命令行入口（薄包装）。

真实逻辑位于 ``src/config_backup.py``（``maybe_backup`` / ``main``），
本文件仅作为独立运行入口保留，便于手动触发与历史调用兼容。

策略说明
--------
- 触发：在应用启动时由 ``lifecycle.main`` / ``run_web`` 调用，不再依赖固定时间定时任务。
- 门禁：今天已备份过 -> 跳过；当前配置相较最近备份无变化 -> 跳过。
- 目录：``data/config-daily-backups/config_daily_YYYYMMDD.yaml``（已被 ``.gitignore`` 忽略）。
- 滚动：保留最近 16 份。
"""
from __future__ import annotations

import sys

from src.config_backup import main

if __name__ == "__main__":
    sys.exit(main())
