from __future__ import annotations
from .base import BackgroundLLMThrottle, PlatformContext, SEND_RETRY_BACKOFF_SECONDS, _active_platform_ctx, extract_card_title, logger  # noqa: F401
import time  # noqa: F401
from concurrent.futures import ThreadPoolExecutor  # noqa: F401
from contextvars import ContextVar  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from datetime import datetime  # noqa: F401
from dotenv import load_dotenv  # noqa: F401
from pydantic import ValidationError  # noqa: F401
from src.config import DEFAULT_STORAGE_PATH, load_config  # noqa: F401
from src.db_backup import DatabaseBackup, DatabaseBackupCoordinator  # noqa: F401
from src.decision_tracker import tracker  # noqa: F401
from src.doc_sync_scheduler import DocSyncScheduler  # noqa: F401
from src.dws_adapter import DwsAdapter  # noqa: F401
from src.im_adapter.base_adapter import BaseIMAdapter  # noqa: F401
from src.im_adapter.feishu import FeishuCliAdapter  # noqa: F401
from src.im_adapter.wecom import WecomCliAdapter  # noqa: F401
from src.intent import validate_tool_action_coverage  # noqa: F401
from src.llm.agent import LLMAgent  # noqa: F401
from src.llm.client import LLMClient, seconds_since_rate_limit  # noqa: F401
from src.llm.display_summary_scheduler import DisplaySummaryScheduler  # noqa: F401
from src.llm.dynamic_summary_scheduler import DynamicSummaryScheduler  # noqa: F401
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError  # noqa: F401
from src.llm.summary_scheduler import SummaryScheduler  # noqa: F401
from src.memory.embedding import EmbeddingClient  # noqa: F401
from src.memory.sqlite_store import SQLiteStore  # noqa: F401
from src.models import Message  # noqa: F401
from src.poller import MessagePoller  # noqa: F401
from src.rule_engine import RuleEngine  # noqa: F401
from src.shared_state import set_app_instance, set_config, set_config_reload_callback  # noqa: F401
from src.skills.manager import SkillManager  # noqa: F401
from src.tools.base import ToolRouter  # noqa: F401
from src.tools.kb_search import KBSearchTool  # noqa: F401
from src.utils.logger import setup_logger  # noqa: F401
from src.utils.request_id import get_request_id, install_log_filter, request_id_scope  # noqa: F401

from .reply_helpers import (
    ReplyHelpersMixin,  # 既有回复增强 mixin + 兼容 re-export
    _citation_relevant_to_reply,  # noqa: F401  # 兼容 test_p2_citation 直接导入
)
from .runtime_lifecycle import LifecycleMixin
from .runtime_setup import SetupMixin
from .runtime_reply_guard import (
    ReplyGuardMixin,
    REPLY_SEND_MIN_INTERVAL_DEFAULT,  # noqa: F401  # 兼容旧测试/调用方从 src.platform.runtime 导入
    REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT,  # noqa: F401
)
from .runtime_dispatch import ReplyDispatchMixin
from .runtime_inbound import InboundMixin
from .runtime_llm_reply import LLMReplyMixin


class RuntimeMixin(  # noqa: F811 组合运行时子 mixin
    LifecycleMixin,
    SetupMixin,
    ReplyGuardMixin,
    ReplyDispatchMixin,
    InboundMixin,
    LLMReplyMixin,
    ReplyHelpersMixin,  # 保持原继承位置（回复增强子系统）
):
    """组合运行时 mixin。

    原 src/platform/runtime.py 单文件 1826 行 / 66 方法（可读性债），按内聚职责拆分为
    6 个子类 mixin（生命周期/初始化、工具与 LLM 装配、回复护栏与分片、回复分发、
    入站处理与 OA、LLM 回复与死信），经多继承组合。各方法名跨组唯一，方法解析顺序(MRO)
    保持原语义，`self.xxx` 调用全部依旧解析。零行为变更。
    """
