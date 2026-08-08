from __future__ import annotations

# 兼容门面：保留 `from main import LinkoraEngine` 等历史导入路径，避免破坏 14+ 个测试。
# 实际实现已拆分到 src/platform/（详见 docs/main_refactor_design.md）。

# --- 核心类 / 函数 re-export ---
from src.platform import (
    LinkoraEngine,
    PlatformContext,
    BackgroundLLMThrottle,
    extract_card_title,
    _active_platform_ctx,
)
from src.platform.lifecycle import main

# --- 历史模块级符号：被部分测试 monkeypatch / 直接引用（main.load_config / main.tracker / main.SQLiteStore 等）---
from src.config import load_config, DEFAULT_STORAGE_PATH
from src.db_backup import DatabaseBackup, DatabaseBackupCoordinator
from src.llm.summary_scheduler import SummaryScheduler
from src.doc_sync_scheduler import DocSyncScheduler
from src.dws_adapter import DwsAdapter
from src.im_adapter.feishu import FeishuCliAdapter
from src.im_adapter.wecom import WecomCliAdapter
from src.llm.agent import LLMAgent
from src.llm.client import LLMClient, seconds_since_rate_limit
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError
from src.memory.embedding import EmbeddingClient
from src.memory.sqlite_store import SQLiteStore
from src.skills.manager import SkillManager
from src.models import Message
from src.poller import MessagePoller
from src.rule_engine import RuleEngine
from src.intent import validate_tool_action_coverage
from src.decision_tracker import tracker as tracker
from src.shared_state import set_app_instance, set_config, set_config_reload_callback
from src.tools.base import ToolRouter
from src.tools.kb_search import KBSearchTool
from src.utils.logger import setup_logger
from src.utils.request_id import request_id_scope, get_request_id, install_log_filter
from src.memory.store_factory import get_store
from src.intent import default_registry
from src.tools.registry import bind_kb_search_embedding, register_builtin_tools
from src.skills.tool_wrapper import SkillTool

# --- stdlib：部分测试通过 `main.<module>` 路径 monkeypatch（main.signal.signal / main.os.path.isfile 等）---
import logging as logging
import os as os
import re as re
import shutil as shutil
import signal as signal
import sys as sys
import tempfile as tempfile
import threading as threading
import time as time
import uuid as uuid

# 本模块是**兼容门面**：上面每一个 import 都是有意的对外再导出，而非"没用到的
# 导入"。__all__ 此前只列了 10 个，导致其余 32 个再导出被 ruff 判为 F401；
# 又因 CI 的 lint 范围是 `src tests web scripts`（不含根目录 main.py），这批告警
# 长期不可见。此处把全部再导出符号显式登记，既表达意图，也让 main.py 可被纳入
# lint 范围而不产生噪声。新增再导出时请同步追加到本列表。
__all__ = [
    # ── 核心类 / 函数 ──
    "LinkoraEngine",
    "PlatformContext",
    "BackgroundLLMThrottle",
    "extract_card_title",
    "_active_platform_ctx",
    "main",
    # ── 配置 / 存储 ──
    "load_config",
    "DEFAULT_STORAGE_PATH",
    "DatabaseBackup",
    "DatabaseBackupCoordinator",
    "SQLiteStore",
    "get_store",
    "EmbeddingClient",
    # ── 调度 / 同步 ──
    "SummaryScheduler",
    "DocSyncScheduler",
    # ── 平台适配器 ──
    "DwsAdapter",
    "FeishuCliAdapter",
    "WecomCliAdapter",
    # ── LLM ──
    "LLMAgent",
    "LLMClient",
    "seconds_since_rate_limit",
    "LLMProcessingError",
    "LLMRateLimitExhaustedError",
    # ── 消息 / 轮询 / 规则 ──
    "Message",
    "MessagePoller",
    "RuleEngine",
    # ── 意图 / 工具 / 技能 ──
    "validate_tool_action_coverage",
    "default_registry",
    "ToolRouter",
    "KBSearchTool",
    "bind_kb_search_embedding",
    "register_builtin_tools",
    "SkillManager",
    "SkillTool",
    # ── 运行期状态 / 可观测性 ──
    "tracker",
    "set_app_instance",
    "set_config",
    "set_config_reload_callback",
    "setup_logger",
    "request_id_scope",
    "get_request_id",
    "install_log_filter",
    # ── stdlib 再导出（测试通过 main.<module> 路径 monkeypatch）──
    "logging",
    "os",
    "re",
    "shutil",
    "signal",
    "sys",
    "tempfile",
    "threading",
    "time",
    "uuid",
]

if __name__ == "__main__":
    # PyInstaller 冻结态必需：多进程子进程（resource_tracker / 线程池 worker）
    # 会以 `-c "from multiprocessing.resource_tracker import main;main(...)"` 形式
    # re-exec 同一个二进制并重新进入本模块的 __main__，若无此守卫会把该 argv 当成
    # 配置文件路径 → FileNotFoundError。freeze_support() 会识别子进程并正确引导后退出。
    import multiprocessing as _mp

    _mp.freeze_support()
    import os as _os

    _PROJECT_ROOT = _os.path.dirname(_os.path.abspath(__file__))
    main(_PROJECT_ROOT)
