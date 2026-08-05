from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

# 在项目入口统一加载 .env（若存在），使 LLM_API_KEY / LLM_FALLBACK_API_KEY 等
# 密钥可经环境变量覆盖 config.yaml（.env 已在 .gitignore 中，不会进版本库）。
from dotenv import load_dotenv

load_dotenv()

from pydantic import ValidationError
from src.config import load_config, DEFAULT_STORAGE_PATH
from src.db_backup import DatabaseBackup, DatabaseBackupCoordinator
from src.llm.summary_scheduler import SummaryScheduler
from src.doc_sync_scheduler import DocSyncScheduler
# 注意：本模块被 runtime_*.py 以 `from .base import *` 星号转导入，DwsAdapter 由
# runtime_lifecycle._build_dws() 在运行时实际构造使用（本文件内不再直接引用，但
# 不是死导入，删除会导致 LifecycleMixin NameError）。
from src.dws_adapter import DwsAdapter
from src.im_adapter.feishu import FeishuCliAdapter
from src.im_adapter.wecom import WecomCliAdapter
from src.llm.agent import LLMAgent
from src.llm.client import LLMClient, seconds_since_rate_limit
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError

# 瞬时发送失败（DWS 异常）后的退避窗口：避免每条消息每轮轮询(5s)硬刷重发刷日志。
SEND_RETRY_BACKOFF_SECONDS = 30
from src.memory.embedding import EmbeddingClient
from src.memory.sqlite_store import SQLiteStore
from src.im_adapter.base_adapter import BaseIMAdapter
from src.skills.manager import SkillManager
from src.models import Message
from src.poller import MessagePoller
from src.rule_engine import RuleEngine
from src.intent import validate_tool_action_coverage
from src.decision_tracker import tracker
from src.shared_state import set_app_instance, set_config, set_config_reload_callback
from src.tools.base import ToolRouter
# KBSearchTool 仍在 _rebuild_kb_search_tool 中直接重建使用；其余内置工具改由
# src/tools/registry.py 自动发现注册，故此处不再逐类导入。
from src.tools.kb_search import KBSearchTool
from src.utils.logger import setup_logger
from src.utils.request_id import request_id_scope, get_request_id, install_log_filter

logger = logging.getLogger(__name__)

# ============ 多平台隔离：运行期平台上下文 ============
# 与 Phase 2（web 层 ?platform= ContextVar）对称，主进程用独立 ContextVar 在
# 派发线程内标记「当前正在处理哪个平台」，使 self.store/self.dws/self.poller/
# self.llm_agent 这四个属性自动落到对应平台组件，无需改动 _handle_message_impl
# 及其 100+ 处调用点。默认 "dingtalk" 保证后台任务（非请求上下文）回退主平台。
_active_platform_ctx: ContextVar[str] = ContextVar("active_platform", default="dingtalk")


@dataclass
class PlatformContext:
    """单个平台的运行期组件集合（多平台隔离的核心单元）。

    main.py 为每个已配置且启用的平台维护一个 PlatformContext；self.store/dws/
    poller/llm_agent 四个属性按 _active_platform_ctx 解析到对应 ctx，从而让所有
    既有业务逻辑零改动地获得平台隔离能力。
    """

    id: str
    display_name: str
    enabled: bool
    adapter_type: str
    store: "SQLiteStore | None" = None
    dws: "BaseIMAdapter | None" = None
    poller: "MessagePoller | None" = None
    llm_agent: "LLMAgent | None" = None
    config: object | None = None  # PlatformConfig 快照
    reply_semaphore: "threading.Semaphore | None" = None  # 平台级回复并发控制
    summary_scheduler: "SummaryScheduler | None" = None  # H2-A 后台异步摘要调度器



class BackgroundLLMThrottle:
    """后台 LLM 任务（对话摘要 / 记忆提取）的限速器。

    三重保护，针对免费 LLM 额度易被限频的痛点：
    1. 最小间隔：两次后台 LLM 调用之间至少间隔 background_min_interval_seconds；
    2. 空闲降频：超过 idle_threshold_seconds 无真实消息时，改用更长的 idle_min_interval_seconds；
    3. 限流退避：主模型触发 429/超时后，rate_limit_backoff_seconds 内直接跳过后台任务。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._last_bg_call = 0.0
        self._last_real_msg = 0.0
        self._lock = threading.Lock()

    def note_real_message(self) -> None:
        """记录一次真实用户消息处理，用于空闲判断。"""
        self._last_real_msg = time.time()

    def _is_idle(self, now: float) -> bool:
        return (now - self._last_real_msg) > self.cfg.idle_threshold_seconds

    def _in_backoff(self, now: float) -> bool:
        if not self.cfg.enabled:
            return False
        return seconds_since_rate_limit() < self.cfg.rate_limit_backoff_seconds

    def acquire(self) -> bool:
        """尝试获取一次后台 LLM 调用配额。

        Returns:
            True  = 允许调用（已满足最小间隔/空闲降频，并刷新时间戳）；
            False = 处于限流退避期，应跳过本次后台任务。
        """
        if not self.cfg.enabled:
            return True
        now = time.time()
        if self._in_backoff(now):
            logger.info(
                "[限速] 主模型近期限流(429/超时)，后台 LLM 任务暂停（剩余 %.0fs）",
                max(0.0, self.cfg.rate_limit_backoff_seconds - seconds_since_rate_limit()),
            )
            return False
        gap = (
            self.cfg.idle_min_interval_seconds
            if self._is_idle(now)
            else self.cfg.background_min_interval_seconds
        )
        # 【竞态修复】"检查-sleep-更新"必须整体在锁内，避免并发线程同时通过
        # elapsed < gap 检查后各自 sleep，导致实际间隔远小于 gap。
        with self._lock:
            elapsed = time.time() - self._last_bg_call
            if elapsed < gap:
                time.sleep(gap - elapsed)
            self._last_bg_call = time.time()
        return True


def extract_card_title(text: str, default_title: str = "回复") -> tuple[str, str]:
    """从 markdown 回复中提取卡片标题。

    若正文以 markdown 标题开头（如 "## 北京天气 · 7/11-7/13"），将其作为钉钉
    markdown 卡片的标题栏，并从正文中移除，避免标题在卡片里重复出现。
    返回 (title, body)。
    """
    heading_match = re.match(r'^#{1,3}\s+(.+?)\s*$', text, re.MULTILINE)
    if heading_match:
        extracted = heading_match.group(1).strip()
        if extracted:
            body = text[heading_match.end():].lstrip("\n")
            return extracted, body
    return default_title, text


