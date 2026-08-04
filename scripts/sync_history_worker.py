#!/usr/bin/env python3
"""手动同步历史消息的 CLI 入口（薄壳）。

历史说明：早期版本这个文件包含完整 bootstrap + sync 主逻辑，由 ``web/routers/sync.py``
通过 ``subprocess.Popen(..., start_new_session=True)`` 拉起独立子进程。该方案在
PyInstaller 冻结态下不可用（脚本不被 spec 打包、``sys.executable`` 是二进制本身）。
现在主逻辑已迁到 ``src.platform.sync_history.run_sync_history``，web 端改为进程内
后台线程调用本函数；本文件保留为 CLI 包装，dev 态手动调试 / 自动化脚本仍可直接用。

用法::

    .venv/bin/python scripts/sync_history_worker.py --days 7 --platform dingtalk
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能 import 项目模块（项目根 = scripts/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.platform.sync_history import run_sync_history  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Linkora 历史消息同步（CLI）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--platform", default="dingtalk")
    ap.add_argument("--job-id", default="")
    ap.add_argument("--config", default=None,
                    help="配置文件路径；默认走 src.paths.get_config_path()")
    ap.add_argument("--full", action="store_true", help="全部历史（逐 30 天窗）")
    ap.add_argument("--conversation-id", default="", help="仅同步该 openConversationId")
    ap.add_argument("--chat-types", default="", help="逗号分隔：single,group（空=全部）")
    ap.add_argument("--scope", default="global", help="current | global（仅用于状态展示）")
    ap.add_argument("--range-label", default="", help="时间范围标签（仅用于状态展示）")
    args = ap.parse_args()

    chat_types = [t.strip() for t in args.chat_types.split(",") if t.strip()] or None
    return run_sync_history(
        days=args.days,
        platform=args.platform,
        job_id=args.job_id,
        scope=args.scope,
        range_label=args.range_label,
        full=args.full,
        conversation_id=args.conversation_id,
        chat_types=chat_types,
        config_path=args.config,
    )


if __name__ == "__main__":
    sys.exit(main())
