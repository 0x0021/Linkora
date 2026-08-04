"""Pydantic request/response models for the web API layer.

Extracted from web/api.py to separate data models from routing logic.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class RuleKeyword(BaseModel):
    match_pattern: str
    reply_text: str
    category: str = "default"
    match_type: str = "fuzzy"
    priority: int = 0


class KeywordUpdate(BaseModel):
    match_pattern: Optional[str] = None
    reply_text: Optional[str] = None
    category: Optional[str] = None
    match_type: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[int] = None


class ConfigUpdate(BaseModel):
    # DWS 配置
    dws_cli_path: str | None = None
    dws_profile: str | None = None
    dws_dry_run: bool | None = None
    dws_retries: int | None = None
    dws_timeout: int | None = None
    # 飞书平台配置（存储在 config.platforms[feishu] 中）
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_retries: int | None = None
    feishu_timeout: int | None = None
    feishu_poll_interval_seconds: int | None = None
    feishu_reply_cooldown_seconds: int | None = None
    # 企业微信平台配置（存储在 config.platforms[wecom] 中）
    wecom_corp_id: str | None = None
    wecom_corp_secret: str | None = None
    wecom_agent_id: str | None = None
    wecom_token: str | None = None
    wecom_encoding_aes_key: str | None = None
    # 轮询器配置
    poller_interval: int | None = None
    poller_merge_window: int | None = None
    poller_history_window: int | None = None
    poller_unread_conversation_count: int | None = None
    poller_max_processed_msg_ids: int | None = None
    poller_list_all_time_window_minutes: int | None = None
    poller_list_all_first_run_minutes: int | None = None
    poller_messages_per_conversation: int | None = None
    poller_reply_cooldown_seconds: int | None = None
    poller_empty_poll_protection_minutes: int | None = None
    poller_skip_msg_types: list[str] | None = None
    poller_skip_notification_patterns: list[str] | None = None
    poller_image_ocr_enabled: bool | None = None
    poller_image_temp_dir: str | None = None
    poller_list_all_empty_alert_rounds: int | None = None
    poller_target_org_corp_id: str | None = None
    # LLM 配置
    llm_provider: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_max_tokens: int | None = None
    llm_temperature: float | None = None
    llm_timeout: int | None = None
    llm_max_tool_rounds: int | None = None
    llm_converge_after_tool_rounds: int | None = None
    llm_max_retries: int | None = None
    llm_base_backoff: float | None = None
    llm_model_pool: list[str] | None = None
    llm_fallback_model_pool: list[str] | None = None
    llm_system_prompt: str | None = None
    # LLM 备用模型
    llm_fallback_api_key: str | None = None
    llm_fallback_base_url: str | None = None
    llm_fallback_model: str | None = None
    # 模型单价自定义（USD / 百万 token），覆盖/补充内置价目表
    model_pricing: dict | None = None
    # LLM 高级配置
    llm_advanced_max_chars_daily_chat: int | None = None
    llm_advanced_max_chars_tech_issue: int | None = None
    llm_advanced_hard_truncation_chars: int | None = None
    # Embedding 配置
    embedding_provider: str | None = None
    embedding_enabled: bool | None = None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_top_k: int | None = None
    embedding_hf_token: str | None = None
    embedding_offline: bool | None = None
    # 功能开关
    tools_enabled: bool | None = None
    rules_enabled: bool | None = None
    # 记忆管理配置
    memory_cleanup_enabled: bool | None = None
    memory_cleanup_max_age_days: int | None = None
    memory_cleanup_min_similarity_threshold: float | None = None
    memory_cleanup_check_interval_days: int | None = None
    memory_retrieval_min_similarity: float | None = None
    # 日志配置
    logging_file: str | None = None
    logging_level: str | None = None
    logging_max_backups: int | None = None
    logging_max_size_mb: int | None = None
    # 存储配置
    storage_path: str | None = None
    storage_type: str | None = None
    storage_backup_enabled: bool | None = None
    storage_backup_dir: str | None = None
    storage_backup_interval_hours: int | None = None
    storage_backup_max_count: int | None = None
    storage_backup_on_start: bool | None = None
    storage_decisions_retention_days: int | None = None
    storage_messages_retention_days: int | None = None
    storage_doc_sync_interval_hours: float | None = None
    # 安全配置
    safety_default_fallback: str | None = None
    safety_media_fallback_text: str | None = None
    safety_sensitive_words: list[str] | None = None
    # Web 配置
    web_port: int | None = None
    web_auth_enabled: bool | None = None
    web_auth_username: str | None = None
    web_auth_password: str | None = None
    # RAG 分块配置
    rag_chunk_size: int | None = None
    rag_chunk_overlap: int | None = None
    # RAG 自动注入
    rag_auto_inject: bool | None = None
    rag_intent_only: bool | None = None
    rag_min_similarity: float | None = None
    rag_max_results: int | None = None
    # 工具路由与限频
    tool_routing_mode: str | None = None
    tools_semantic_routing: bool | None = None
    tools_semantic_tool_threshold: float | None = None
    tool_rate_limits: dict | None = None
    # 规则引擎
    regex_timeout_seconds: float | None = None
    intent_filter_enabled: bool | None = None
    intent_filter_pure_thank_max_length: int | None = None
    intent_filter_pure_ack_max_length: int | None = None
    intent_filter_business_ratio_threshold: float | None = None
    keyword_denylist: list[str] | None = None
    # 死信队列
    dlq_enabled: bool | None = None
    # 技能引擎
    skills_enabled: bool | None = None
    skills_auto_activate: bool | None = None
    skills_semantic_routing: bool | None = None
    skills_semantic_skill_threshold: float | None = None
    skills_combo_enabled: bool | None = None
    skills_combo_gap: float | None = None
    # 会话摘要
    conversation_summary_enabled: bool | None = None
    conversation_summary_max_messages: int | None = None
    conversation_summary_interval_hours: int | None = None
    conversation_summary_ratio: float | None = None
    # LLM 节流
    llm_throttle_enabled: bool | None = None
    llm_throttle_active_interval: int | None = None
    llm_throttle_idle_threshold: int | None = None
    llm_throttle_idle_interval: int | None = None
    llm_throttle_backoff: int | None = None
    llm_throttle_mem_cooldown: int | None = None
    llm_throttle_mem_min_chars: int | None = None
    llm_throttle_max_summaries: int | None = None
    llm_throttle_summary_limit: int | None = None
    # 高级轮询参数
    poller_history_days: int | None = None
    poller_session_gap_minutes: int | None = None
    poller_empty_alert_rounds: int | None = None
    poller_first_run_ignore_minutes: int | None = None
    poller_blacklist_failures: int | None = None
    poller_blacklist_reconcile: int | None = None
    poller_reconcile_batch: int | None = None
    poller_cache_ttl: int | None = None
    poller_min_interval: int | None = None
    poller_ai_tag: bool | None = None
    poller_mark_read: bool | None = None


class RagQuery(BaseModel):
    query: str
    top_k: int = 5
    min_similarity: float = 0.0


class SystemPromptUpdate(BaseModel):
    system_prompt: str


class DingTalkDocSync(BaseModel):
    query: str = ""


class KbDocumentCreate(BaseModel):
    title: str
    content: str
    doc_type: str = "text"
    source: str = "manual"


class KeywordMatchTest(BaseModel):
    text: str


class KeywordBatchOp(BaseModel):
    ids: list[int]
    action: str
    category: Optional[str] = None


class RagChatQuery(BaseModel):
    query: str
    top_k: int = 5
    use_llm: bool = False


class DingTalkDocImportKb(BaseModel):
    doc_id: str


class AutoSyncUpdate(BaseModel):
    auto_sync: bool


class ExternalFriendCreate(BaseModel):
    name: str
    open_dingtalk_id: str
    chat_id: str = ""
    notes: str = ""
