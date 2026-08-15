"""应用配置：加载 / 校验 / 多平台 seed（拆分自 config.py）。

配置数据模型（Pydantic）已迁至 config_models.py，本模块保留：
- config.yaml 加载与 mtime 缓存（load_config）
- key 校验（validate_config_keys，防拼错静默失效）
- .env 密钥兜底（_apply_env_overrides）
- 多平台隔离 / legacy 兼容（_seed_platforms / _sync_root_poller / _build_dingtalk_platform）
- 适配器类懒加载（get_adapter_class）
所有模型与常量从 config_models 重导出，``from src.config import AppConfig`` 等
旧引用路径完全兼容。
"""
from __future__ import annotations

# 此文件是 config_models 重导出层（保 `from src.config import AppConfig` 等旧路径兼容）
# F401 抑制见 pyproject.toml [tool.ruff.lint.per-file-ignores]

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from src.config_models import (
    DwsConfig, AdapterOverrideConfig, PollerConfig, KeywordRule, RulesConfig,
    StorageConfig, PlatformRagConfig, PlatformLLMConfig, PlatformToolsConfig,
    PlatformConfig, OcrPostprocessConfig, LoggingConfig, LlmAdvancedConfig,
    LlmConfig, LlmThrottleConfig, ToolsConfig, EmbeddingConfig, MemoryConfig,
    SkillsConfig, SkillHubConfig, SafetyConfig, DeadLetterConfig, RagConfig,
    WebConfig, OaApprovalConfig, AppConfig,
    DEFAULT_DATA_DIR, DEFAULT_STORAGE_PATH, DEFAULT_TMP_IMAGES_DIR, DEFAULT_BACKUP_DIR,
    _resolve_model_cls, _ConfigState, _config_state,
    _MODELS_REBUILT, _MODELS_REBUILD_LOCK, _ensure_models_rebuilt,
)

logger = logging.getLogger(__name__)

_KNOWN_DICT_KEYS: dict[str, set[str]] = {
    "root.rules.blacklist": {"users", "groups", "enabled"},
    "root.rules.whitelist": {"enabled", "users", "groups"},
    "root.memory.cleanup": {"enabled", "max_age_days", "min_similarity_threshold", "check_interval_days"},
    "root.memory.retrieval": {"min_similarity"},
    "root.memory.conversation_summary": {
        "enabled", "max_messages_per_conversation", "summary_interval_hours", "summary_ratio",
    },
}


_last_validated_sig: str | None = None


_config_cache: dict[str, tuple[float, "AppConfig"]] = {}


_config_cache_lock = threading.Lock()


def _config_signature(raw: dict) -> str:
    """对配置内容求稳定签名，用于日志去重。"""
    try:
        text = yaml.safe_dump(raw, sort_keys=True, allow_unicode=True, default_flow_style=False)
    except Exception:
        logger.debug("配置签名 YAML dump 失败，回退 repr")
        text = repr(raw)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def validate_config_keys(raw: dict, config: AppConfig) -> list[str]:
    """校验 config.yaml 的 key 是对应 pydantic 模型字段的子集。

    pydantic 默认 extra="ignore"，config.yaml 里**拼错或多余**的 key 会被静默
    丢弃，导致「以为开了某开关、实际没生效」（例如把 llm_throttle 拼成
    llm_throtle）。本函数在加载后把这类未知 key 显式 logger.warning 出来，
    把「偷偷失效」变成「启动即见」。返回未知 key 的描述列表，便于测试断言。

    日志去重：相同配置内容（按签名）已校验过则不再重复打印，避免 Web 各 router
    直接调 load_config（绕过 web/api 的 mtime 缓存）在每个请求都刷一行。
    """
    warnings: list[str] = []

    def _check(level: str, raw_section: Any, model_cls: type[BaseModel]) -> None:
        if not isinstance(raw_section, dict):
            return
        allowed = set(model_cls.model_fields.keys())
        for key, child in raw_section.items():
            if key not in allowed:
                msg = (
                    f"config.yaml 段 [{level}] 含未知 key '{key}'"
                    f"（pydantic 已忽略，请检查是否拼错或已废弃）"
                )
                warnings.append(msg)
                continue
            nested_cls = _resolve_model_cls(model_cls.model_fields[key].annotation)
            if nested_cls is not None and isinstance(child, dict):
                _check(f"{level}.{key}", child, nested_cls)
            elif nested_cls is None and isinstance(child, dict):
                # 自由 dict 段：若其 key 集合已知（固定结构），校验子 key
                section_path = f"{level}.{key}"
                known = _KNOWN_DICT_KEYS.get(section_path)
                if known is not None:
                    for sub_key in child:
                        if sub_key not in known:
                            msg = (
                                f"config.yaml 段 [{section_path}] 含未知 key '{sub_key}'"
                                f"（请检查是否拼错或已废弃）"
                            )
                            warnings.append(msg)

    _check("root", raw, AppConfig)

    # 日志去重：相同配置内容已校验过则不重复打印（逻辑仍执行、返回值仍准确）。
    sig = _config_signature(raw)
    if sig == _config_state.last_validated_sig:
        return warnings
    _config_state.last_validated_sig = sig

    for msg in warnings:
        logger.warning(msg)
    if warnings:
        logger.warning(
            "config.yaml 共发现 %d 个未知 key，强烈建议核查（见上方 warning）",
            len(warnings),
        )
    else:
        logger.info("config.yaml key 校验通过：无未知/拼错 key")
    return warnings


def _apply_env_overrides(config: "AppConfig") -> None:
    """把 .env 中的密钥/端点兜给 config 对象，使其与运行时（llm/client.py、embedding.py）一致。

    规则（与 Embedding 段统一）：**config.yaml 中显式填写的值优先**，env 仅作兜底——
    仅当对应字段为空（None / 空串）或仍是出厂默认时才用 env 覆盖。这样用户在
    config.yaml 显式配置的密钥/端点不会被 .env 静默覆盖，restore-default 等操作也不会
    把 env 值误写回 config.yaml（见 tests/test_web_api_endpoints.py 的回归护栏）。
    """
    env = os.environ

    llm = config.llm
    # 主 LLM：config.yaml 显式值优先，env 仅兜底
    if not llm.api_key:
        llm.api_key = env.get("LLM_API_KEY") or llm.api_key
    # base_url 出厂默认是 https://api.openai.com/v1，未显式配置（空或仍是默认）时才让 env 兜底
    if not llm.base_url or llm.base_url == "https://api.openai.com/v1":
        llm.base_url = env.get("LLM_BASE_URL") or llm.base_url
    # 跨服务商备用
    if not llm.fallback_api_key:
        llm.fallback_api_key = env.get("LLM_FALLBACK_API_KEY") or llm.fallback_api_key
    if not llm.fallback_base_url:
        llm.fallback_base_url = env.get("LLM_FALLBACK_BASE_URL") or llm.fallback_base_url
    if not llm.fallback_model:
        llm.fallback_model = env.get("LLM_FALLBACK_MODEL") or llm.fallback_model
    # 第二层备用（本地部署）
    if not llm.secondary_fallback_api_key:
        llm.secondary_fallback_api_key = env.get("LLM_SECONDARY_FALLBACK_API_KEY") or llm.secondary_fallback_api_key
    if not llm.secondary_fallback_base_url:
        llm.secondary_fallback_base_url = env.get("LLM_SECONDARY_FALLBACK_BASE_URL") or llm.secondary_fallback_base_url

    # Embedding：config.yaml 显式 key 优先；仅当未设置时才回退 env（与 embedding.py 运行时一致）
    # 关键修复：.env 的 LLM_API_KEY 仅作兜底，不得覆盖 config.yaml 里显式填写的 embedding 密钥
    emb = config.embedding
    if not emb.api_key:
        emb_key = env.get("EMBEDDING_API_KEY") or env.get("LLM_API_KEY")
        if emb_key:
            emb.api_key = emb_key


def load_config(path: str = "config.yaml", *, validate: bool = True) -> AppConfig:
    """加载并缓存配置。相同文件（mtime 未变）直接返回缓存，避免重复 YAML 解析 + Pydantic 校验。

    热重载（main.reload_config）检测到文件变更后调用本函数时 mtime 已变，自动穿透缓存。
    """
    _ensure_models_rebuilt()

    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    cache_key = str(config_path)
    try:
        mtime = config_path.stat().st_mtime
    except OSError as _exc:
        logger.debug(f"load_config: swallowed exception: {_exc}")
        mtime = -1.0

    # 快速路径：mtime 未变且缓存命中 → 直接返回
    with _config_cache_lock:
        cached = _config_cache.get(cache_key)
        if cached is not None:
            cached_mtime, cached_config = cached
            if cached_mtime == mtime:
                return cached_config

    # 慢路径：解析 YAML + Pydantic 校验
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    config = AppConfig(**data)
    # 环境变量兜底：.env（load_dotenv 在 main.py 启动时已注入）是密钥真源，
    # 镜像运行时 llm/client.py / embedding.py 的优先级——env 非空则优先于 config.yaml。
    # 这样 config 对象与运行时完全一致：RAG 状态判断（直接读 config.llm.api_key）
    # 也能拿到真 key，且 config.yaml 无需写任何明文密钥。
    _apply_env_overrides(config)
    # 启动自检：config.yaml 的 key 是否均为 config.py 字段子集（防静默失效）
    if validate:
        validate_config_keys(data, config)
    # 多平台隔离：若未显式配置 platforms，用全局 dws/storage/poller 自动 seed 出
    # 一个 dingtalk 平台，保证 legacy config.yaml（无 platforms 段）零变化。
    _seed_platforms(config)
    _sync_root_poller(config, data)

    with _config_cache_lock:
        _config_cache[cache_key] = (mtime, config)
    return config


def _build_dingtalk_platform(config: AppConfig) -> "PlatformConfig":
    """用 legacy 全局 dws/storage/poller 构造 dingtalk 平台（深拷贝 storage/poller 避免别名共享）。"""
    return PlatformConfig(
        id="dingtalk",
        display_name="钉钉",
        enabled=True,
        adapter_type="dingtalk",
        # 深拷贝，避免与 config.storage / config.poller 共享可变实例
        storage=StorageConfig(**config.storage.model_dump()),
        poller=PollerConfig(**config.poller.model_dump()),
        adapter=AdapterOverrideConfig(
            cli_path=config.dws.cli_path,
            timeout=config.dws.timeout,
            retries=config.dws.retries,
            dry_run=config.dws.dry_run,
            profile=config.dws.profile,
        ),
    )


def _seed_platforms(config: AppConfig) -> None:
    """多平台隔离向后兼容：保证至少有一个 dingtalk 平台。

    - platforms 为空 → 用 legacy dws/storage/poller 自动构造 dingtalk。
    - platforms 非空但缺 dingtalk → 告警并注入 dingtalk（legacy 兼容）。
    - 其余平台（飞书/企微）按用户配置原样保留。
    """
    if config.platforms:
        if not any(p.id == "dingtalk" for p in config.platforms):
            logger.warning(
                "config.platforms 未包含 dingtalk 平台；已自动注入 dingtalk（legacy 兼容）"
            )
            config.platforms.insert(0, _build_dingtalk_platform(config))
        return
    config.platforms = [_build_dingtalk_platform(config)]


def _sync_root_poller(config: AppConfig, raw: dict) -> None:
    """多平台隔离兼容：config.poller（root 级）已废弃，poller 配置迁移到各平台块。

    - 运行时让 config.poller 别名到 dingtalk 平台块 poller，使 main.py / web 沿用
      config.poller 的读写零改动，且写入自然落入 platforms[0].poller。
    - 兼容 legacy config.yaml（仍含 root poller）：将其真实值合并进 dingtalk 平台块
      （root 优先），保证升级不丢配置项。
    """
    dt = next((p for p in config.platforms if p.id == "dingtalk"), None)
    if dt is None:
        return
    if "poller" in raw:
        # legacy：root poller 显式存在，合并进 dingtalk 平台块（root 优先）
        root_dump = config.poller.model_dump()
        plat_dump = dt.poller.model_dump()
        merged = {**plat_dump, **root_dump}
        dt.poller = PollerConfig(**merged)
    # 别名：config.poller 指向 dingtalk 平台块 poller（同一实例）
    config.poller = dt.poller


def get_adapter_class(adapter_type: str) -> type:
    """adapter_type → 适配器类（懒加载，避免 config 与 adapter 模块循环导入）。

    main 构建平台上下文时调用，按平台 adapter_type 取对应适配器类再实例化。
    """
    if adapter_type == "dingtalk":
        from src.dws_adapter import DwsAdapter
        return DwsAdapter
    if adapter_type == "feishu":
        from src.im_adapter.feishu import FeishuCliAdapter
        return FeishuCliAdapter
    if adapter_type == "wecom":
        from src.im_adapter.wecom import WecomCliAdapter
        return WecomCliAdapter
    raise ValueError(f"未知 adapter_type: {adapter_type}")
