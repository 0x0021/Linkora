"""多平台运行期核心类型与共享工具。

P2-1 步骤 2（2026-10-27）前的本文件是「星号导入枢纽」：import 40+ 个第三方
符号供 12 个消费者用 `from .base import *` 转出。步骤 1 已把 12 处消费者改为
显式导入清单；本步骤进一步把本文件瘦身为**只定义自己拥有的东西**：

- 真实定义：``_active_platform_ctx`` / ``PlatformContext`` /
  ``BackgroundLLMThrottle`` / ``extract_card_title`` / ``SEND_RETRY_BACKOFF_SECONDS``
- 仅供类型注解：``TYPE_CHECKING`` 块（运行时零成本，pyright 仍可解析）

其余第三方符号此前全部是**纯重导出**（本文件内并不使用），已移除；它们各自
的源模块在别处均有主入口，删除不改变任何模块的首次导入时机。

⚠️ 本文件仍保留一次 ``load_dotenv()`` 调用（**幂等兜底**，2026-10-27 dotenv
搬迁专项）：真正的入口已移到 ``main()`` 首行（``src/dotenv_loader.py``），
但 ``src/memory/account_identity.py:38`` 等 3 处仍在**模块级**读环境变量
（``DWS_BIN`` / ``LINKORA_AUDIT_LOG`` / ``ENABLE_SEARXNG``），这些模块可能在
``main()`` 之前被导入 —— 缺了这次兜底，``DWS_BIN`` 覆盖会静默失效
（钉钉账号隔离退化为 ``dingtalk:unknown``）。``load_dotenv()`` 本身幂等，
重复调用零成本、零行为差异。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.dotenv_loader import load_env

# 幂等兜底：确保在 main() 之前被导入的模块也能拿到 .env 注入的环境变量
# （真正的入口是 main() 首行的显式调用）。
load_env()

from src.llm.client import seconds_since_rate_limit

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行时不导入
    from src.im_adapter.base_adapter import BaseIMAdapter
    from src.llm.agent import LLMAgent
    from src.llm.display_summary_scheduler import DisplaySummaryScheduler
    from src.llm.dynamic_summary_scheduler import DynamicSummaryScheduler
    from src.llm.summary_scheduler import SummaryScheduler
    from src.memory.sqlite_store import SQLiteStore
    from src.poller import MessagePoller

logger = logging.getLogger(__name__)

# 瞬时发送失败（DWS 异常）后的退避窗口：避免每条消息每轮轮询(5s)硬刷重发刷日志。
SEND_RETRY_BACKOFF_SECONDS = 30

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
    dynamic_summary_scheduler: "DynamicSummaryScheduler | None" = None  # 动态（信号驱动）摘要调度器
    display_summary_scheduler: "DisplaySummaryScheduler | None" = None  # 展示用全量摘要调度器（供 Web 摘要页）


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
