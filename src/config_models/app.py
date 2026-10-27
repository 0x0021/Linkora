"""应用根配置模型（汇总全部子配置）。

P2-7 第 3 步（2026-10-27）自 ``src/config_models/__init__.py`` 外移。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.config_models._const import (  # noqa: F401
    DEFAULT_BACKUP_DIR,
    DEFAULT_DATA_DIR,
    DEFAULT_STORAGE_PATH,
    DEFAULT_TMP_IMAGES_DIR,
)

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


class AppConfig(BaseModel):
    dws: DwsConfig = Field(default_factory=DwsConfig)
    platforms: list[PlatformConfig] = Field(default_factory=list)  # 多平台隔离；空 → 自动 seed 出 dingtalk
    poller: PollerConfig = Field(default_factory=PollerConfig)
    oa_approval: OaApprovalConfig = Field(default_factory=OaApprovalConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ocr_postprocess: OcrPostprocessConfig = Field(default_factory=OcrPostprocessConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    dead_letter: DeadLetterConfig = Field(default_factory=DeadLetterConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    summary_backfill: SummaryBackfillConfig = Field(default_factory=SummaryBackfillConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    skillhub: SkillHubConfig = Field(default_factory=SkillHubConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    llm_throttle: LlmThrottleConfig = Field(default_factory=LlmThrottleConfig)
