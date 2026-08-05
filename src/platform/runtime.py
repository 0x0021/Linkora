from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403
from .base import _active_platform_ctx
from .reply_helpers import ReplyHelpersMixin, _citation_relevant_to_reply  # 既有回复增强 mixin + 兼容 re-export
from .runtime_reply_guard import (  # 兼容 re-export：原 runtime.py 经这些名字对外暴露（含单测的 import 与 monkeypatch）
    SHARD_SEND_INTERVAL_SECONDS,
    REPLY_SEND_MIN_INTERVAL_DEFAULT,
    REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT,
    _RATE_LIMIT_HINTS,
)
from .runtime_lifecycle import LifecycleMixin
from .runtime_setup import SetupMixin
from .runtime_reply_guard import ReplyGuardMixin
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
