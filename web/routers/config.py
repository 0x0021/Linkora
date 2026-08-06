"""配置 / 工具 / LLM 提示词 路由。

从 `web/api.py` 抽取（原 1993–2392、2668–2730 行），业务逻辑不变。
- load_config / CONFIG_PATH / get_app_instance / _write_config 经 `web.api` 属性访问，
  以尊重测试对 `web.api.*` 的 monkeypatch；
- 为避免与 `web/api.py` 的循环导入（web.api 在模块末尾挂载子路由
  `from web.routers.config import router`，若此处顶层 import web.api 即成环），
  此处改用惰性代理 `_api`，首次属性访问时才真正导入 web.api（此时 web.api 已完整加载）；
- ConfigUpdate / SystemPromptUpdate **必须运行时导入**：虽然
  `from __future__ import annotations` 让注解变成字符串，但 FastAPI 会用
  `typing.get_type_hints()` 对路由函数签名求值来推导请求体模型，模块 namespace
  里没有这两个名字就是 NameError（实测 update_config / update_system_prompt 的
  注解求值直接失败）。schemas 只依赖 pydantic，顶层导入无循环风险。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from src.config import AppConfig
from web.dependencies import logger
from web.schemas import ConfigUpdate, SystemPromptUpdate


class _LazyApi:
    """惰性访问 web.api 的代理，破除与 web/api.py 的循环导入。"""

    def __getattr__(self, name: str):
        import web.api as _api_mod
        return getattr(_api_mod, name)


_api = _LazyApi()

router = APIRouter()


# =============================================================================
# Helpers – per-domain config application (extracted from update_config)
# =============================================================================

def _apply_dws(update: ConfigUpdate, cfg: AppConfig):
    """DWS CLI 配置"""
    if update.dws_cli_path is not None:
        cfg.dws.cli_path = update.dws_cli_path
    if update.dws_profile is not None:
        cfg.dws.profile = update.dws_profile
    if update.dws_dry_run is not None:
        cfg.dws.dry_run = update.dws_dry_run
    if update.dws_retries is not None:
        cfg.dws.retries = update.dws_retries
    if update.dws_timeout is not None:
        cfg.dws.timeout = update.dws_timeout


def _apply_feishu_platform(update: ConfigUpdate, cfg: AppConfig):
    """飞书平台配置"""
    _fp = _ensure_platform_config(cfg, "feishu")
    if update.feishu_retries is not None:
        _fp.adapter.retries = update.feishu_retries
    if update.feishu_timeout is not None:
        _fp.adapter.timeout = update.feishu_timeout
    if update.feishu_poll_interval_seconds is not None:
        _fp.poller.interval_seconds = update.feishu_poll_interval_seconds
    if update.feishu_reply_cooldown_seconds is not None:
        _fp.poller.reply_cooldown_seconds = update.feishu_reply_cooldown_seconds


def _apply_wecom_platform(update: ConfigUpdate, cfg: AppConfig) -> None:  # noqa: ARG001
    """企微平台配置（占位，后续接入 adapter 后激活写入）"""
    _ensure_platform_config(cfg, "wecom")  # ensure it exists for API consistency


def _apply_poller_base(update: ConfigUpdate, cfg: AppConfig):
    """轮询器基础配置"""
    if update.poller_interval is not None:
        cfg.poller.interval_seconds = update.poller_interval
    if update.poller_merge_window is not None:
        cfg.poller.merge_window_seconds = update.poller_merge_window
    if update.poller_history_window is not None:
        cfg.poller.history_window = update.poller_history_window
    if update.poller_unread_conversation_count is not None:
        cfg.poller.unread_conversation_count = update.poller_unread_conversation_count
    if update.poller_messages_per_conversation is not None:
        cfg.poller.messages_per_conversation = update.poller_messages_per_conversation
    if update.poller_reply_cooldown_seconds is not None:
        cfg.poller.reply_cooldown_seconds = update.poller_reply_cooldown_seconds


def _apply_llm_base(update: ConfigUpdate, cfg: AppConfig):
    """LLM 基础配置"""
    if update.llm_provider is not None:
        cfg.llm.provider = update.llm_provider
    if update.llm_api_key is not None and update.llm_api_key != REDACTED_SENTINEL:
        cfg.llm.api_key = update.llm_api_key
    if update.llm_timeout is not None:
        cfg.llm.timeout = update.llm_timeout
    if update.llm_temperature is not None:
        cfg.llm.temperature = update.llm_temperature
    if update.llm_model is not None:
        cfg.llm.model = update.llm_model
    if update.llm_max_tokens is not None:
        cfg.llm.max_tokens = update.llm_max_tokens
    if update.llm_base_url is not None:
        cfg.llm.base_url = update.llm_base_url
    if update.llm_max_tool_rounds is not None:
        cfg.llm.max_tool_rounds = update.llm_max_tool_rounds
    if update.llm_converge_after_tool_rounds is not None:
        cfg.llm.converge_after_tool_rounds = update.llm_converge_after_tool_rounds
    if update.llm_max_retries is not None:
        cfg.llm.max_retries = update.llm_max_retries
    if update.llm_base_backoff is not None:
        cfg.llm.base_backoff = update.llm_base_backoff
    if update.llm_model_pool is not None:
        cfg.llm.model_pool = [m.strip() for m in update.llm_model_pool if m and m.strip()]
    if update.llm_fallback_model_pool is not None:
        cfg.llm.fallback_model_pool = [m.strip() for m in update.llm_fallback_model_pool if m and m.strip()]
    if update.llm_system_prompt is not None:
        cfg.llm.system_prompt = update.llm_system_prompt


def _apply_llm_fallback(update: ConfigUpdate, cfg: AppConfig):
    """LLM 备用配置"""
    if update.llm_fallback_api_key is not None and update.llm_fallback_api_key != REDACTED_SENTINEL:
        cfg.llm.fallback_api_key = update.llm_fallback_api_key
    if update.llm_fallback_base_url is not None:
        cfg.llm.fallback_base_url = update.llm_fallback_base_url
    if update.llm_fallback_model is not None:
        cfg.llm.fallback_model = update.llm_fallback_model


def _apply_model_pricing(update: ConfigUpdate, cfg: AppConfig):
    """模型单价自定义"""
    if update.model_pricing is not None:
        cleaned: dict[str, dict[str, float]] = {}
        for name, price in (update.model_pricing or {}).items():
            if not name:
                continue
            if not isinstance(price, dict):
                continue
            try:
                cleaned[str(name)] = {
                    "input": float(price.get("input", 0) or 0),
                    "output": float(price.get("output", 0) or 0),
                }
            except (TypeError, ValueError):
                continue
        cfg.llm.model_pricing = cleaned


def _apply_embedding(update: ConfigUpdate, cfg: AppConfig):
    """Embedding 配置"""
    if update.embedding_provider is not None:
        cfg.embedding.provider = update.embedding_provider
    if update.embedding_api_key is not None and update.embedding_api_key != REDACTED_SENTINEL:
        cfg.embedding.api_key = update.embedding_api_key
    if update.embedding_base_url is not None:
        cfg.embedding.base_url = update.embedding_base_url
    if update.embedding_enabled is not None:
        cfg.embedding.enabled = update.embedding_enabled
    if update.embedding_model is not None:
        cfg.embedding.model = update.embedding_model
    if update.embedding_top_k is not None:
        cfg.embedding.top_k = update.embedding_top_k
    if update.embedding_hf_token is not None and update.embedding_hf_token != REDACTED_SENTINEL:
        cfg.embedding.hf_token = update.embedding_hf_token
    if update.embedding_offline is not None:
        cfg.embedding.offline = update.embedding_offline


def _apply_tools_cfg(update: ConfigUpdate, cfg: AppConfig):
    """Tools 开关"""
    if update.tools_enabled is not None:
        cfg.tools.enabled = update.tools_enabled


def _apply_rules_cfg(update: ConfigUpdate, cfg: AppConfig):
    """Rules 开关"""
    if update.rules_enabled is not None:
        cfg.rules.enabled = update.rules_enabled


def _apply_poller_advanced(update: ConfigUpdate, cfg: AppConfig):
    """轮询器高级配置"""
    if update.poller_max_processed_msg_ids is not None:
        cfg.poller.max_processed_msg_ids = update.poller_max_processed_msg_ids
    if update.poller_list_all_time_window_minutes is not None:
        cfg.poller.list_all_time_window_minutes = update.poller_list_all_time_window_minutes
    if update.poller_list_all_first_run_minutes is not None:
        cfg.poller.list_all_first_run_minutes = update.poller_list_all_first_run_minutes
    if update.poller_empty_poll_protection_minutes is not None:
        cfg.poller.empty_poll_protection_minutes = update.poller_empty_poll_protection_minutes
    if update.poller_skip_msg_types is not None:
        cfg.poller.skip_msg_types = update.poller_skip_msg_types
    if update.poller_skip_notification_patterns is not None:
        cfg.poller.skip_notification_patterns = update.poller_skip_notification_patterns
    if update.poller_image_ocr_enabled is not None:
        cfg.poller.image_ocr_enabled = update.poller_image_ocr_enabled
    if update.poller_image_temp_dir is not None:
        cfg.poller.image_temp_dir = update.poller_image_temp_dir
    if update.poller_list_all_empty_alert_rounds is not None:
        cfg.poller.list_all_empty_alert_rounds = update.poller_list_all_empty_alert_rounds


def _apply_target_org(update: ConfigUpdate, cfg: AppConfig):
    """目标组织 + 热应用"""
    if update.poller_target_org_corp_id is not None:
        new_val = (update.poller_target_org_corp_id or "").strip()
        cfg.poller.target_org_corp_id = new_val
        try:
            app_instance = _api.get_app_instance()
            poller = app_instance.poller if app_instance and hasattr(app_instance, "poller") else None
            if poller is not None:
                poller.target_org_corp_id = new_val
                if new_val:
                    orgs = poller.dws.list_orgs()
                    if any(o.get("corp_id") == new_val for o in orgs):
                        poller.dws.use_org(new_val)
                poller.clear_cross_org_skips()
                logger.info("[配置] 目标组织已更新为 %s，已清除跨组织跳过名单重新探测", new_val or "自动(当前组织)")
        except Exception as e:
            logger.warning("[配置] 实时应用目标组织失败（将在重启后生效）: %s", e)


def _apply_llm_advanced(update: ConfigUpdate, cfg: AppConfig):
    """LLM 高级配置"""
    if update.llm_advanced_max_chars_daily_chat is not None:
        cfg.llm.advanced.max_chars_daily_chat = update.llm_advanced_max_chars_daily_chat
    if update.llm_advanced_max_chars_tech_issue is not None:
        cfg.llm.advanced.max_chars_tech_issue = update.llm_advanced_max_chars_tech_issue
    if update.llm_advanced_hard_truncation_chars is not None:
        cfg.llm.advanced.hard_truncation_chars = update.llm_advanced_hard_truncation_chars


def _apply_memory_cleanup(update: ConfigUpdate, cfg: AppConfig):
    """记忆管理 - 清理配置"""
    if update.memory_cleanup_enabled is not None:
        cfg.memory.cleanup["enabled"] = update.memory_cleanup_enabled
    if update.memory_cleanup_max_age_days is not None:
        cfg.memory.cleanup["max_age_days"] = update.memory_cleanup_max_age_days
    if update.memory_cleanup_min_similarity_threshold is not None:
        cfg.memory.cleanup["min_similarity_threshold"] = update.memory_cleanup_min_similarity_threshold
    if update.memory_cleanup_check_interval_days is not None:
        cfg.memory.cleanup["check_interval_days"] = update.memory_cleanup_check_interval_days
    if update.memory_retrieval_min_similarity is not None:
        cfg.memory.retrieval["min_similarity"] = update.memory_retrieval_min_similarity


def _apply_logging(update: ConfigUpdate, cfg: AppConfig):
    """日志配置"""
    if update.logging_file is not None:
        cfg.logging.file = update.logging_file
    if update.logging_level is not None:
        cfg.logging.level = update.logging_level
    if update.logging_max_backups is not None:
        cfg.logging.max_backups = update.logging_max_backups
    if update.logging_max_size_mb is not None:
        cfg.logging.max_size_mb = update.logging_max_size_mb


def _apply_storage(update: ConfigUpdate, cfg: AppConfig):
    """存储配置"""
    if update.storage_path is not None:
        cfg.storage.path = update.storage_path
    if update.storage_type is not None:
        cfg.storage.type = update.storage_type
    if update.storage_backup_enabled is not None:
        cfg.storage.backup_enabled = update.storage_backup_enabled
    if update.storage_backup_dir is not None:
        cfg.storage.backup_dir = update.storage_backup_dir
    if update.storage_backup_interval_hours is not None:
        cfg.storage.backup_interval_hours = update.storage_backup_interval_hours
    if update.storage_backup_max_count is not None:
        cfg.storage.backup_max_count = update.storage_backup_max_count
    if update.storage_backup_on_start is not None:
        cfg.storage.backup_on_start = update.storage_backup_on_start
    if update.storage_decisions_retention_days is not None:
        cfg.storage.decisions_retention_days = update.storage_decisions_retention_days
    if update.storage_messages_retention_days is not None:
        cfg.storage.messages_retention_days = update.storage_messages_retention_days
    if update.storage_doc_sync_interval_hours is not None:
        cfg.storage.doc_sync_interval_hours = update.storage_doc_sync_interval_hours


def _apply_safety(update: ConfigUpdate, cfg: AppConfig):
    """安全配置"""
    if update.safety_default_fallback is not None:
        cfg.safety.default_fallback = update.safety_default_fallback
    if update.safety_media_fallback_text is not None:
        cfg.safety.media_fallback_text = update.safety_media_fallback_text
    if update.safety_sensitive_words is not None:
        cfg.safety.sensitive_words = update.safety_sensitive_words


def _apply_web(update: ConfigUpdate, cfg: AppConfig):
    """Web 配置"""
    if update.web_port is not None:
        cfg.web.port = update.web_port
    if update.web_auth_enabled is not None:
        cfg.web.auth_enabled = update.web_auth_enabled
    if update.web_auth_username is not None:
        cfg.web.auth_username = update.web_auth_username
    if update.web_auth_password is not None:
        pwd = str(update.web_auth_password)
        if pwd.strip() and pwd != REDACTED_SENTINEL:
            cfg.web.auth_password = pwd


def _apply_rag_chunking(update: ConfigUpdate, cfg: AppConfig):
    """RAG 分块配置"""
    if update.rag_chunk_size is not None:
        cfg.rag.chunk_size = update.rag_chunk_size
    if update.rag_chunk_overlap is not None:
        cfg.rag.chunk_overlap = update.rag_chunk_overlap


def _apply_rag_auto_inject(update: ConfigUpdate, cfg: AppConfig):
    """RAG 自动注入"""
    if update.rag_auto_inject is not None:
        cfg.llm.advanced.rag_auto_inject = update.rag_auto_inject
    if update.rag_intent_only is not None:
        cfg.llm.advanced.rag_intent_only = update.rag_intent_only
    if update.rag_min_similarity is not None:
        cfg.llm.advanced.rag_min_similarity = update.rag_min_similarity
    if update.rag_max_results is not None:
        cfg.llm.advanced.rag_max_results = update.rag_max_results


def _apply_tool_routing(update: ConfigUpdate, cfg: AppConfig):
    """工具路由与限频"""
    if update.tool_routing_mode is not None:
        cfg.tools.tool_routing_mode = update.tool_routing_mode
    if update.tools_semantic_routing is not None:
        cfg.tools.semantic_routing = update.tools_semantic_routing
    if update.tools_semantic_tool_threshold is not None:
        cfg.tools.semantic_tool_threshold = update.tools_semantic_tool_threshold
    if update.tool_rate_limits is not None:
        for tool_name, limit_dict in update.tool_rate_limits.items():
            if cfg.tools.rate_limit is None:
                cfg.tools.rate_limit = {}
            if tool_name not in cfg.tools.rate_limit:
                cfg.tools.rate_limit[tool_name] = {}
            cfg.tools.rate_limit[tool_name]["per_hour"] = limit_dict.get("per_hour")


def _apply_rule_engine(update: ConfigUpdate, cfg: AppConfig):
    """规则引擎"""
    if update.regex_timeout_seconds is not None:
        cfg.rules.regex_timeout_seconds = update.regex_timeout_seconds
    if update.intent_filter_enabled is not None:
        if not cfg.rules.intent_filter:
            cfg.rules.intent_filter = {}
        cfg.rules.intent_filter["enabled"] = update.intent_filter_enabled
    if update.intent_filter_pure_thank_max_length is not None:
        if not cfg.rules.intent_filter:
            cfg.rules.intent_filter = {}
        cfg.rules.intent_filter["pure_thank_max_length"] = update.intent_filter_pure_thank_max_length
    if update.intent_filter_pure_ack_max_length is not None:
        if not cfg.rules.intent_filter:
            cfg.rules.intent_filter = {}
        cfg.rules.intent_filter["pure_ack_max_length"] = update.intent_filter_pure_ack_max_length
    if update.intent_filter_business_ratio_threshold is not None:
        if not cfg.rules.intent_filter:
            cfg.rules.intent_filter = {}
        cfg.rules.intent_filter["business_ratio_threshold"] = update.intent_filter_business_ratio_threshold
    if update.keyword_denylist is not None:
        cfg.rules.keyword_denylist = update.keyword_denylist


def _apply_dlq(update: ConfigUpdate, cfg: AppConfig):
    """死信队列"""
    if update.dlq_enabled is not None:
        cfg.dead_letter.enabled = update.dlq_enabled


def _apply_skills(update: ConfigUpdate, cfg: AppConfig):
    """技能引擎"""
    if update.skills_enabled is not None:
        cfg.skills.enabled = update.skills_enabled
    if update.skills_auto_activate is not None:
        cfg.skills.auto_activate = update.skills_auto_activate
    if update.skills_semantic_routing is not None:
        cfg.skills.semantic_routing = update.skills_semantic_routing
    if update.skills_semantic_skill_threshold is not None:
        cfg.skills.semantic_skill_threshold = update.skills_semantic_skill_threshold
    if update.skills_combo_enabled is not None:
        cfg.skills.combo_enabled = update.skills_combo_enabled
    if update.skills_combo_gap is not None:
        cfg.skills.combo_gap = update.skills_combo_gap


def _apply_conversation_summary(update: ConfigUpdate, cfg: AppConfig):
    """会话摘要"""
    if update.conversation_summary_enabled is not None:
        cfg.memory.conversation_summary["enabled"] = update.conversation_summary_enabled
    if update.conversation_summary_max_messages is not None:
        cfg.memory.conversation_summary["max_messages_per_conversation"] = update.conversation_summary_max_messages
    if update.conversation_summary_interval_hours is not None:
        cfg.memory.conversation_summary["summary_interval_hours"] = update.conversation_summary_interval_hours
    if update.conversation_summary_ratio is not None:
        cfg.memory.conversation_summary["summary_ratio"] = update.conversation_summary_ratio


def _apply_llm_throttle(update: ConfigUpdate, cfg: AppConfig):
    """LLM 节流"""
    if update.llm_throttle_enabled is not None:
        cfg.llm_throttle.enabled = update.llm_throttle_enabled
    if update.llm_throttle_active_interval is not None:
        cfg.llm_throttle.background_min_interval_seconds = update.llm_throttle_active_interval
    if update.llm_throttle_idle_threshold is not None:
        cfg.llm_throttle.idle_threshold_seconds = update.llm_throttle_idle_threshold
    if update.llm_throttle_idle_interval is not None:
        cfg.llm_throttle.idle_min_interval_seconds = update.llm_throttle_idle_interval
    if update.llm_throttle_backoff is not None:
        cfg.llm_throttle.rate_limit_backoff_seconds = update.llm_throttle_backoff
    if update.llm_throttle_mem_cooldown is not None:
        cfg.llm_throttle.extract_memory_cooldown_seconds = update.llm_throttle_mem_cooldown
    if update.llm_throttle_mem_min_chars is not None:
        cfg.llm_throttle.extract_memory_min_new_chars = update.llm_throttle_mem_min_chars
    if update.llm_throttle_max_summaries is not None:
        cfg.llm_throttle.max_summaries_per_cycle = update.llm_throttle_max_summaries
    if update.llm_throttle_summary_limit is not None:
        cfg.llm_throttle.summary_history_limit = update.llm_throttle_summary_limit


def _apply_poller_extra(update: ConfigUpdate, cfg: AppConfig):
    """高级轮询参数"""
    if update.poller_history_days is not None:
        cfg.poller.history_days = update.poller_history_days
    if update.poller_session_gap_minutes is not None:
        cfg.poller.history_session_gap_minutes = update.poller_session_gap_minutes
    if update.poller_empty_alert_rounds is not None:
        cfg.poller.list_all_empty_alert_rounds = update.poller_empty_alert_rounds
    if update.poller_first_run_ignore_minutes is not None:
        cfg.poller.first_run_ignore_older_than_minutes = update.poller_first_run_ignore_minutes
    if update.poller_blacklist_failures is not None:
        cfg.poller.blacklist_min_consecutive_failures = update.poller_blacklist_failures
    if update.poller_blacklist_reconcile is not None:
        cfg.poller.blacklist_reconcile_every = update.poller_blacklist_reconcile
    if update.poller_reconcile_batch is not None:
        cfg.poller.reconcile_probe_batch_size = update.poller_reconcile_batch
    if update.poller_cache_ttl is not None:
        cfg.poller.top_convs_cache_ttl_seconds = update.poller_cache_ttl
    if update.poller_min_interval is not None:
        cfg.poller.min_conversation_poll_interval_seconds = update.poller_min_interval
    if update.poller_ai_tag is not None:
        cfg.poller.ai_tag_enabled = update.poller_ai_tag
    if update.poller_mark_read is not None:
        cfg.poller.mark_read_after_process = update.poller_mark_read


# =============================================================================
# Router endpoints
# =============================================================================


def _ensure_platform_config(config: AppConfig, platform_id: str):
    """在 config.platforms 中查找或创建指定平台的 PlatformConfig 条目。"""
    from src.config import PlatformConfig, AdapterOverrideConfig, PollerConfig
    for p in config.platforms:
        if p.id == platform_id:
            return p
    new_p = PlatformConfig(
        id=platform_id,
        display_name=platform_id.title(),
        enabled=False,
        adapter=AdapterOverrideConfig(),
        poller=PollerConfig(),
    )
    config.platforms.append(new_p)
    return new_p


@router.post("/api/config/default")
async def restore_default_config():
    try:
        import shutil
        from src.config import AppConfig
        backup_path = _api.CONFIG_PATH + ".bak"
        if Path(_api.CONFIG_PATH).exists():
            shutil.copy2(_api.CONFIG_PATH, backup_path)
        default_config = AppConfig(web={"auth_enabled": True, "auth_password": "please-change-me"})
        try:
            current = _api.load_config(_api.CONFIG_PATH)
            default_config.embedding.enabled = current.embedding.enabled
            default_config.embedding.provider = current.embedding.provider
            default_config.skills.ai_intent_generation_enabled = current.skills.ai_intent_generation_enabled
            if current.tools.rate_limit:
                default_config.tools.rate_limit = current.tools.rate_limit
        except Exception:
            pass
        _api._write_config(default_config.model_dump())
        return {"success": True, "message": f"已恢复默认配置（原配置备份为 {backup_path}；已保留 embedding/skills 已开启的核心开关与限频）"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/api/config")
async def get_config(platform: str = ""):
    try:
        config = _api._get_cfg()
        data = config.model_dump()
        def _mask(v):
            return v[:4] + "****" if v else ""
        if "llm" in data:
            data["llm"]["api_key"] = _mask(data["llm"].get("api_key", ""))
            data["llm"]["fallback_api_key"] = _mask(data["llm"].get("fallback_api_key", ""))
        if "embedding" in data:
            data["embedding"]["api_key"] = _mask(data["embedding"].get("api_key", ""))
            data["embedding"]["hf_token"] = _mask(data["embedding"].get("hf_token", ""))
        if "web" in data:
            data["web"]["auth_password"] = _mask(data["web"].get("auth_password", ""))
        for p in data.get("platforms") or []:
            if p.get("llm"):
                p["llm"]["api_key"] = _mask(p["llm"].get("api_key", ""))
                p["llm"]["fallback_api_key"] = _mask(p["llm"].get("fallback_api_key", ""))
        for p in data.get("platforms") or []:
            pid = p.get("id", "")
            if pid in ("feishu", "wecom"):
                adapter = p.get("adapter") or {}
                poller = p.get("poller") or {}
                if pid == "feishu":
                    data["feishu"] = {
                        "app_id": "",
                        "app_secret": "",
                        "retries": adapter.get("retries"),
                        "timeout": adapter.get("timeout"),
                        "poll_interval_seconds": poller.get("interval_seconds"),
                        "reply_cooldown_seconds": poller.get("reply_cooldown_seconds"),
                    }
                elif pid == "wecom":
                    data["wecom"] = {
                        "corp_id": "",
                        "corp_secret": "",
                        "agent_id": "",
                        "token": "",
                        "encoding_aes_key": "",
                    }
        if "feishu" not in data:
            data["feishu"] = {}
        if "wecom" not in data:
            data["wecom"] = {}
        return data
    except Exception as e:
        logger.error("获取配置失败: %s", e)
        raise HTTPException(status_code=500, detail="内部服务器错误") from e


@router.post("/api/config")
async def update_config(update: ConfigUpdate):
    """统一配置更新入口 —— 委托给各域 helper 完成实际赋值。"""
    try:
        cfg = _api.load_config(_api.CONFIG_PATH)

        # ---- Domain applications (order matches original for reproducibility) ----
        _apply_dws(update, cfg)
        _apply_feishu_platform(update, cfg)
        _apply_wecom_platform(update, cfg)
        _apply_poller_base(update, cfg)
        _apply_llm_base(update, cfg)
        _apply_llm_fallback(update, cfg)
        _apply_model_pricing(update, cfg)
        _apply_embedding(update, cfg)
        _apply_tools_cfg(update, cfg)
        _apply_rules_cfg(update, cfg)
        _apply_poller_advanced(update, cfg)
        _apply_target_org(update, cfg)
        _apply_llm_advanced(update, cfg)
        _apply_memory_cleanup(update, cfg)
        _apply_logging(update, cfg)
        _apply_storage(update, cfg)
        _apply_safety(update, cfg)
        _apply_web(update, cfg)
        _apply_rag_chunking(update, cfg)
        _apply_rag_auto_inject(update, cfg)
        _apply_tool_routing(update, cfg)
        _apply_rule_engine(update, cfg)
        _apply_dlq(update, cfg)
        _apply_skills(update, cfg)
        _apply_conversation_summary(update, cfg)
        _apply_llm_throttle(update, cfg)
        _apply_poller_extra(update, cfg)

        # ---- Validation & save ----
        _validate_update_config(update)
        wresult = _api._write_config(cfg.model_dump(),
                           changed_keys={"llm", "rag", "poller", "memory", "skills", "embedding"})
        try:
            from src.shared_state import get_config_reload_callback
            callback = get_config_reload_callback()
            if callback:
                callback()
        except Exception as cb_err:
            logger.warning("配置重新加载回调失败: %s", cb_err)

        return {"success": True, "message": "配置更新成功并已生效", **wresult}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# —— 落盘前敏感字段校验：避免任意路径/越界端口写入导致 bot 起不来或越权写文件 ——
_FORBIDDEN_PATH_PREFIXES = (
    "/etc", "/proc", "/sys", "/", "/usr", "/bin", "/sbin", "/boot", "/dev",
    "/System", "/Library",
)


def _safe_writable_path(value: str, field: str) -> str:
    """校验路径类配置：必须是可写位置，且不能落在系统禁止区。

    防护层级：① 拒绝空值/非字符串；② 显式拒绝路径穿越（``..`` 段）；
    ③ expanduser + realpath（含符号链接解析）后确认不在系统禁止区；
    ④ 父目录必须可创建且可写。
    """
    if not value or not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field} 不能为空")
    if ".." in value.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail=f"{field} 含非法路径段: {value}")
    try:
        resolved = Path(os.path.abspath(os.path.expanduser(value)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field} 路径无法解析: {value}") from None
    rstr = str(resolved)
    for bad in _FORBIDDEN_PATH_PREFIXES:
        if rstr == bad or rstr.startswith(bad + os.sep):
            raise HTTPException(status_code=400,
                                detail=f"{field} 禁止写入系统路径: {value}")
    parent = resolved.parent if resolved.suffix else resolved
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            raise HTTPException(status_code=400,
                                detail=f"{field} 父目录不可创建: {value}") from None
    if not os.access(str(parent), os.W_OK):
        raise HTTPException(status_code=400, detail=f"{field} 父目录不可写: {value}")
    return value


def _validate_update_config(update: Any) -> None:
    """update_config 落盘前对敏感字段做语义校验，失败返回 400 而非崩溃。"""
    if update.web_port is not None:
        if not (1 <= update.web_port <= 65535):
            raise HTTPException(status_code=400,
                                detail=f"web.port 必须在 1-65535 之间: {update.web_port}")
        if update.web_port < 1024:
            raise HTTPException(status_code=400,
                                detail="web.port 需 >=1024（特权端口需 root）")
    if update.storage_path is not None:
        _safe_writable_path(update.storage_path, "storage.path")
    if update.storage_backup_dir is not None:
        _safe_writable_path(update.storage_backup_dir, "storage.backup_dir")
    if update.logging_file is not None:
        _safe_writable_path(update.logging_file, "logging.file")


# ============ Tools ============

@router.get("/api/tools")
async def tools():
    try:
        config = _api._get_cfg()
        return {
            "enabled": config.tools.enabled,
            "available": config.tools.available,
            "rate_limit": config.tools.rate_limit,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============ LLM ============

@router.get("/api/llm/prompt")
async def get_system_prompt():
    try:
        config = _api._get_cfg()
        return {"system_prompt": config.llm.system_prompt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/llm/prompt")
async def update_system_prompt(update: SystemPromptUpdate):
    try:
        config = _api.load_config(_api.CONFIG_PATH)
        config.llm.system_prompt = update.system_prompt
        _api._write_config(config.model_dump())
        return {"success": True, "message": "系统提示词更新成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ============ Message Stats ============


# ============ 配置导出脱敏 ============
REDACTED_SENTINEL = "***REDACTED***"
_SECRET_KEYS = {
    "api_key", "fallback_api_key", "hf_token",
    "auth_password", "app_secret", "corp_secret",
    "token", "encoding_aes_key", "webhook_secret",
    "secondary_fallback_api_key",
}
_SECRET_KEY_SUFFIXES = ("_api_key", "_token", "_secret", "_password", "api_key", "token", "secret", "password")


def _is_secret_key(name: str) -> bool:
    low = name.lower()
    return low in _SECRET_KEYS or low.endswith(_SECRET_KEY_SUFFIXES)


def _redact_secrets(obj):
    """递归将敏感字段（非空）替换为哨兵值。"""
    if isinstance(obj, dict):
        return {
            k: (REDACTED_SENTINEL if (_is_secret_key(k) and v not in (None, "", REDACTED_SENTINEL)) else _redact_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_secrets(v) for v in obj]
    return obj


def _restore_secrets(imported, current):
    """将 imported 中的哨兵值用 current 对应真实值还原（原地修改）。"""
    if isinstance(imported, dict):
        for k, v in list(imported.items()):
            cur = current.get(k) if isinstance(current, dict) else None
            if _is_secret_key(k) and v == REDACTED_SENTINEL:
                imported[k] = cur if cur not in (None, "", REDACTED_SENTINEL) else ""
            elif isinstance(v, (dict, list)):
                _restore_secrets(v, cur if isinstance(cur, (dict, list)) else {})
    elif isinstance(imported, list):
        for i, v in enumerate(imported):
            cur = current[i] if isinstance(current, list) and i < len(current) else None
            if isinstance(v, (dict, list)):
                _restore_secrets(v, cur if isinstance(cur, (dict, list)) else {})


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并：以 base 为基底，用 override 中出现的所有 key 覆盖；dict 递归合并，
    list / 标量整体替换（不逐元素合并，避免数组语义歧义）。返回新 dict，不修改入参。

    用于 import_config：导入文件只覆盖其声明的段/参数，其余全部保留，杜绝
    「导入不完整配置 → 静默丢弃其它段参数」的删参数类缺陷。
    """
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


@router.get("/api/config/export")
async def export_config():
    """导出当前配置文件为 YAML（密钥字段脱敏）。"""
    try:
        config = _api._get_cfg()
        import yaml
        _dump = config.model_dump()
        _dump.pop("poller", None)
        yaml_content = yaml.dump(_redact_secrets(_dump), default_flow_style=False, allow_unicode=True)
        return JSONResponse(
            content={"config": yaml_content},
            headers={
                "Content-Disposition": f"attachment; filename=config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
            }
        )
    except Exception as e:
        logger.error("导出配置失败: %s", e)
        raise HTTPException(status_code=500, detail="内部服务器错误") from e


@router.post("/api/config/import")
async def import_config(file: UploadFile = File(...)):
    """导入配置文件并热重载。

    合并语义（防删参数）：以「磁盘现有全量配置」为基底，仅用导入文件中出现的
    段/参数覆盖，其余全部保留。导入不完整配置不再静默丢弃其它段。
    """
    try:
        content = await file.read()
        text = content.decode("utf-8")

        import yaml
        imported_data = yaml.safe_load(text)
        if not isinstance(imported_data, dict):
            raise HTTPException(status_code=400, detail="无效的配置文件格式")

        required_keys = ["dws", "llm", "storage"]
        for key in required_keys:
            if key not in imported_data:
                raise HTTPException(status_code=400, detail=f"配置文件缺少必需字段: {key}")

        try:
            def _read_current():
                with open(_api.CONFIG_PATH, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            current = await run_in_threadpool(_read_current)
        except Exception as read_err:
            logger.warning("读取现有配置失败，将仅以导入文件写盘: %s", read_err)
            current = {}

        try:
            _restore_secrets(imported_data, current)
        except Exception as restore_err:
            logger.warning("导入配置时还原脱敏密钥失败（将保留文件中的值）: %s", restore_err)

        merged = _deep_merge(current, imported_data)

        from src.config import AppConfig
        try:
            AppConfig(**merged)
        except Exception as val_err:
            raise HTTPException(status_code=400, detail=f"配置数据校验失败：{val_err}") from val_err

        await run_in_threadpool(_api._write_config, merged)

        try:
            from src.shared_state import get_config_reload_callback
            callback = get_config_reload_callback()
            if callback:
                callback()
        except Exception as cb_err:
            logger.warning("配置重新加载回调失败: %s", cb_err)

        return {"success": True, "message": "配置导入成功并已生效"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("导入配置失败: %s", e)
        raise HTTPException(status_code=500, detail="内部服务器错误") from e
