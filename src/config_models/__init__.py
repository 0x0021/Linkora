"""配置数据模型（Pydantic）——按领域拆分为包。

P2-7（2026-10-27）：本模块原为单文件 ``src/config_models.py``（1127 行 /
28 个 Pydantic 模型），现拆为包 ``src/config_models/``：

- ``_const.py``         共享路径常量（零依赖，供各子模块运行时 import）
- ``platform.py``       平台级配置（钉钉 / 适配器覆盖 / 轮询 / 存储 / 平台 RAG·LLM·工具）
- ``llm.py``            LLM 与向量嵌入配置
- ``rules_tools.py``    规则、工具与 OCR 后处理配置
- ``memory_skills.py``  记忆、技能与检索配置
- ``system.py``         系统级配置（日志 / 安全 / 死信 / 主动摘要 / Web / OA 审批）
- ``app.py``            应用根配置（汇总全部子配置）

本 ``__init__.py`` 只做三件事：
1. **re-export** 全部模型与常量 —— ``from src.config_models import X`` 的
   导入面对消费者**完全不变**（历史消费者无需改动一行）；
2. 提供模型解析/重建辅助（``_resolve_model_cls`` / ``_ensure_models_rebuilt``
   / ``_ConfigState``）；
3. 用 ``__all__`` 显式声明公共 API 面。

⚠️ 跨模块依赖必须用**运行时** import（不可用 ``TYPE_CHECKING``）—— Pydantic v2
依据 ``cls.__module__`` 取该模块 globals 求值注解，仅类型检查期的导入会让
注解解析失败（``PydanticUndefinedAnnotation``）。各子模块已按此约定实现。
"""
from __future__ import annotations

import logging
import threading
import typing
from typing import Any

from pydantic import BaseModel

from src.config_models._const import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_DATA_DIR,
    DEFAULT_STORAGE_PATH,
    DEFAULT_TMP_IMAGES_DIR,
)
from src.config_models.app import AppConfig  # noqa: F401
from src.config_models.llm import (  # noqa: F401
    EmbeddingConfig,
    LlmAdvancedConfig,
    LlmConfig,
    LlmThrottleConfig,
)
from src.config_models.memory_skills import (  # noqa: F401
    MemoryConfig,
    RagConfig,
    SkillHubConfig,
    SkillsConfig,
)
from src.config_models.platform import (  # noqa: F401
    AdapterOverrideConfig,
    DwsConfig,
    PlatformConfig,
    PlatformLLMConfig,
    PlatformRagConfig,
    PlatformToolsConfig,
    PollerConfig,
    StorageConfig,
)
from src.config_models.rules_tools import (  # noqa: F401
    KeywordRule,
    OcrPostprocessConfig,
    RulesConfig,
    ToolsConfig,
)
from src.config_models.system import (  # noqa: F401
    DeadLetterConfig,
    LoggingConfig,
    OaApprovalConfig,
    ProactiveConfig,
    SafetyConfig,
    SummaryBackfillConfig,
    WebConfig,
)

logger = logging.getLogger(__name__)


def _resolve_model_cls(annotation: Any) -> type[BaseModel] | None:
    """从字段注解里取出嵌套的 pydantic 模型类（兼容 Optional[Model] / Model）。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


class _ConfigState:
    """配置状态（替代 global 变量）。"""

    def __init__(self) -> None:
        self._last_validated_sig: str | None = None
        self._models_rebuilt = False
        self._models_rebuild_lock = threading.Lock()

    @property
    def last_validated_sig(self) -> str | None:
        return self._last_validated_sig

    @last_validated_sig.setter
    def last_validated_sig(self, value: str) -> None:
        self._last_validated_sig = value

    @property
    def models_rebuilt(self) -> bool:
        return self._models_rebuilt

    @models_rebuilt.setter
    def models_rebuilt(self, value: bool) -> None:
        self._models_rebuilt = value

    @property
    def models_rebuild_lock(self) -> threading.Lock:
        return self._models_rebuild_lock


_config_state = _ConfigState()


_MODELS_REBUILT = False
_MODELS_REBUILD_LOCK = threading.Lock()


def _ensure_models_rebuilt() -> None:
    """PEP 563 延迟解析：Pydantic v2 需在首次使用前 rebuild 以解析前向引用。

    不能在模块顶层调用 model_rebuild()（Python 3.14 + annotationlib 会因
    ForwardRef 创建时机导致命名空间不完整），故延迟到 load_config 首调用时懒执行。
    使用双重检查锁定避免并发首次调用时重复 rebuild。
    """
    if _config_state.models_rebuilt:
        return
    with _config_state.models_rebuild_lock:
        if _config_state.models_rebuilt:
            return
        PlatformConfig.model_rebuild()
        AppConfig.model_rebuild()
        _config_state.models_rebuilt = True


__all__ = [
    # 默认路径常量
    "DEFAULT_DATA_DIR",
    "DEFAULT_STORAGE_PATH",
    "DEFAULT_TMP_IMAGES_DIR",
    "DEFAULT_BACKUP_DIR",
    # 平台
    "DwsConfig",
    "AdapterOverrideConfig",
    "PollerConfig",
    "StorageConfig",
    "PlatformRagConfig",
    "PlatformLLMConfig",
    "PlatformToolsConfig",
    "PlatformConfig",
    # 规则与工具
    "KeywordRule",
    "RulesConfig",
    "OcrPostprocessConfig",
    "ToolsConfig",
    # LLM 与嵌入
    "LlmAdvancedConfig",
    "LlmConfig",
    "LlmThrottleConfig",
    "EmbeddingConfig",
    # 记忆 / 技能 / 检索
    "MemoryConfig",
    "SkillsConfig",
    "SkillHubConfig",
    "RagConfig",
    # 系统
    "LoggingConfig",
    "SafetyConfig",
    "DeadLetterConfig",
    "ProactiveConfig",
    "SummaryBackfillConfig",
    "WebConfig",
    "OaApprovalConfig",
    # 根
    "AppConfig",
]
