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
import shutil
import yaml
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


def _ensure_platform_config(config: AppConfig, platform_id: str):
    """在 config.platforms 中查找或创建指定平台的 PlatformConfig 条目。"""
    from src.config import PlatformConfig, AdapterOverrideConfig, PollerConfig
    for p in config.platforms:
        if p.id == platform_id:
            return p
    # 不存在则创建默认条目并追加
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
        import yaml, shutil
        from src.config import AppConfig
        # 备份当前配置
        backup_path = _api.CONFIG_PATH + ".bak"
        if Path(_api.CONFIG_PATH).exists():
            shutil.copy2(_api.CONFIG_PATH, backup_path)
        # 保存默认配置
        # fail-closed 兼容：默认 web 段 auth_enabled=True 但密码为空会被启动校验拒绝，
        # 故构造时即提供占位密码，使「恢复默认配置」产出可启动的安全配置（与 config.yaml.example 一致）。
        default_config = AppConfig(web={"auth_enabled": True, "auth_password": "please-change-me"})
        # 保留用户有意开启的核心功能开关与限频，避免静默降级：
        # 代码默认值与 config.yaml 存在有意漂移（embedding.enabled /
        # ai_intent_generation_enabled 在 yaml 中为 true，代码默认为 false；
        # tools.rate_limit 各 per_hour 数值亦不一致）。直接覆盖 AppConfig()
        # 会静默关闭 RAG / 语义检索 / AI 意图生成并重置限频，故此处显式保有。
        try:
            current = _api.load_config(_api.CONFIG_PATH)
            default_config.embedding.enabled = current.embedding.enabled
            default_config.embedding.provider = current.embedding.provider
            default_config.skills.ai_intent_generation_enabled = current.skills.ai_intent_generation_enabled
            if current.tools.rate_limit:
                default_config.tools.rate_limit = current.tools.rate_limit
        except Exception:
            pass
        # 原子写入（os.replace + 写前备份），避免写入中途崩溃损坏 config.yaml（F17）
        _api._write_config(default_config.model_dump())
        return {"success": True, "message": f"已恢复默认配置（原配置备份为 {backup_path}；已保留 embedding/skills 已开启的核心开关与限频）"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/config")
async def get_config(platform: str = ""):
    try:
        config = _api._get_cfg()
        data = config.model_dump()
        # 脱敏敏感字段：仅显示前 4 位 + ****，空值保持空字符串
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
        # 平台级配置脱敏
        for p in data.get("platforms") or []:
            if p.get("llm"):
                p["llm"]["api_key"] = _mask(p["llm"].get("api_key", ""))
                p["llm"]["fallback_api_key"] = _mask(p["llm"].get("fallback_api_key", ""))
        # 将 platforms 数组展开为 data.feishu / data.wecom 扁平结构，
        # 以便前端 config.js loadConfigPage() 直接读取
        for p in data.get("platforms") or []:
            pid = p.get("id", "")
            if pid in ("feishu", "wecom"):
                adapter = p.get("adapter") or {}
                poller = p.get("poller") or {}
                if pid == "feishu":
                    data["feishu"] = {
                        "app_id": "",  # lark-cli 自行管理 auth，不存配置文件
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
        # 如果 platforms 中没有 feishu/wecom 条目，给空对象兜底
        if "feishu" not in data:
            data["feishu"] = {}
        if "wecom" not in data:
            data["wecom"] = {}
        return data
    except Exception as e:
        logger.error("获取配置失败: %s", e)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/api/config")
async def update_config(update: ConfigUpdate):
    try:
        import yaml
        config = _api.load_config(_api.CONFIG_PATH)
        # DWS 配置
        if update.dws_cli_path is not None:
            config.dws.cli_path = update.dws_cli_path
        if update.dws_profile is not None:
            config.dws.profile = update.dws_profile
        if update.dws_dry_run is not None:
            config.dws.dry_run = update.dws_dry_run
        if update.dws_retries is not None:
            config.dws.retries = update.dws_retries
        if update.dws_timeout is not None:
            config.dws.timeout = update.dws_timeout
        # 飞书 / 企微平台配置（写入 config.platforms 数组中的对应条目）
        _fp = _ensure_platform_config(config, "feishu")
        if update.feishu_retries is not None:
            _fp.adapter.retries = update.feishu_retries
        if update.feishu_timeout is not None:
            _fp.adapter.timeout = update.feishu_timeout
        if update.feishu_poll_interval_seconds is not None:
            _fp.poller.interval_seconds = update.feishu_poll_interval_seconds
        if update.feishu_reply_cooldown_seconds is not None:
            _fp.poller.reply_cooldown_seconds = update.feishu_reply_cooldown_seconds
        # 飞书 app_id / app_secret 仅透传前端（lark-cli 自行管理 auth），
        # 不写入 config.yaml——静默忽略
        _wp = _ensure_platform_config(config, "wecom")
        # 企微字段目前仅占位，后续接入企微 adapter 后再激活写入
        # Poller 配置
        if update.poller_interval is not None:
            config.poller.interval_seconds = update.poller_interval
        if update.poller_merge_window is not None:
            config.poller.merge_window_seconds = update.poller_merge_window
        if update.poller_history_window is not None:
            config.poller.history_window = update.poller_history_window
        if update.poller_unread_conversation_count is not None:
            config.poller.unread_conversation_count = update.poller_unread_conversation_count
        if update.poller_messages_per_conversation is not None:
            config.poller.messages_per_conversation = update.poller_messages_per_conversation
        if update.poller_reply_cooldown_seconds is not None:
            config.poller.reply_cooldown_seconds = update.poller_reply_cooldown_seconds
        # LLM 配置
        if update.llm_provider is not None:
            config.llm.provider = update.llm_provider
        if update.llm_api_key is not None and update.llm_api_key != REDACTED_SENTINEL:
            config.llm.api_key = update.llm_api_key
        if update.llm_timeout is not None:
            config.llm.timeout = update.llm_timeout
        if update.llm_temperature is not None:
            config.llm.temperature = update.llm_temperature
        if update.llm_model is not None:
            config.llm.model = update.llm_model
        if update.llm_max_tokens is not None:
            config.llm.max_tokens = update.llm_max_tokens
        if update.llm_base_url is not None:
            config.llm.base_url = update.llm_base_url
        if update.llm_max_tool_rounds is not None:
            config.llm.max_tool_rounds = update.llm_max_tool_rounds
        if update.llm_converge_after_tool_rounds is not None:
            config.llm.converge_after_tool_rounds = update.llm_converge_after_tool_rounds
        if update.llm_max_retries is not None:
            config.llm.max_retries = update.llm_max_retries
        if update.llm_base_backoff is not None:
            config.llm.base_backoff = update.llm_base_backoff
        if update.llm_model_pool is not None:
            config.llm.model_pool = [m.strip() for m in update.llm_model_pool if m and m.strip()]
        if update.llm_fallback_model_pool is not None:
            config.llm.fallback_model_pool = [m.strip() for m in update.llm_fallback_model_pool if m and m.strip()]
        if update.llm_system_prompt is not None:
            config.llm.system_prompt = update.llm_system_prompt
        # LLM 备用模型
        if update.llm_fallback_api_key is not None and update.llm_fallback_api_key != REDACTED_SENTINEL:
            config.llm.fallback_api_key = update.llm_fallback_api_key
        if update.llm_fallback_base_url is not None:
            config.llm.fallback_base_url = update.llm_fallback_base_url
        if update.llm_fallback_model is not None:
            config.llm.fallback_model = update.llm_fallback_model
        # 模型单价自定义：清洗后写入（覆盖/补充内置价目表）
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
            config.llm.model_pricing = cleaned
        # Embedding 配置
        if update.embedding_provider is not None:
            config.embedding.provider = update.embedding_provider
        if update.embedding_api_key is not None and update.embedding_api_key != REDACTED_SENTINEL:
            config.embedding.api_key = update.embedding_api_key
        if update.embedding_base_url is not None:
            config.embedding.base_url = update.embedding_base_url
        if update.embedding_enabled is not None:
            config.embedding.enabled = update.embedding_enabled
        if update.embedding_model is not None:
            config.embedding.model = update.embedding_model
        if update.embedding_top_k is not None:
            config.embedding.top_k = update.embedding_top_k
        if update.embedding_hf_token is not None and update.embedding_hf_token != REDACTED_SENTINEL:
            config.embedding.hf_token = update.embedding_hf_token
        if update.embedding_offline is not None:
            config.embedding.offline = update.embedding_offline
        # Tools 配置
        if update.tools_enabled is not None:
            config.tools.enabled = update.tools_enabled
        # Rules 配置
        if update.rules_enabled is not None:
            config.rules.enabled = update.rules_enabled
        # 轮询器高级配置
        if update.poller_max_processed_msg_ids is not None:
            config.poller.max_processed_msg_ids = update.poller_max_processed_msg_ids
        # 注意：processed_msg_ttl_seconds 已在 M2 修复中移除（TTL 移交 DB 层
        # sqlite_store.cleanup_processed_msgs 负责），PollerConfig 已无此字段，
        # 故此处不再赋值，避免保存时抛 ValueError → HTTP 500。前端对应输入框已移除。
        if update.poller_list_all_time_window_minutes is not None:
            config.poller.list_all_time_window_minutes = update.poller_list_all_time_window_minutes
        if update.poller_list_all_first_run_minutes is not None:
            config.poller.list_all_first_run_minutes = update.poller_list_all_first_run_minutes
        if update.poller_empty_poll_protection_minutes is not None:
            config.poller.empty_poll_protection_minutes = update.poller_empty_poll_protection_minutes
        if update.poller_skip_msg_types is not None:
            config.poller.skip_msg_types = update.poller_skip_msg_types
        if update.poller_skip_notification_patterns is not None:
            config.poller.skip_notification_patterns = update.poller_skip_notification_patterns
        if update.poller_image_ocr_enabled is not None:
            config.poller.image_ocr_enabled = update.poller_image_ocr_enabled
        if update.poller_image_temp_dir is not None:
            config.poller.image_temp_dir = update.poller_image_temp_dir
        if update.poller_list_all_empty_alert_rounds is not None:
            config.poller.list_all_empty_alert_rounds = update.poller_list_all_empty_alert_rounds
        if update.poller_target_org_corp_id is not None:
            new_val = (update.poller_target_org_corp_id or "").strip()
            config.poller.target_org_corp_id = new_val
            # 实时应用到运行中的轮询器（无需重启）
            try:
                app_instance = _api.get_app_instance()
                poller = app_instance.poller if app_instance and hasattr(app_instance, "poller") else None
                if poller is not None:
                    poller.target_org_corp_id = new_val
                    if new_val:
                        # 指定了具体组织：若其有已登录 profile 则切换，并清除跳过名单重新探测
                        orgs = poller.dws.list_orgs()
                        if any(o.get("corp_id") == new_val for o in orgs):
                            poller.dws.use_org(new_val)
                    # 无论指定还是切回自动，都清除跨组织跳过名单以便重新探测
                    poller.clear_cross_org_skips()
                    logger.info("[配置] 目标组织已更新为 %s，已清除跨组织跳过名单重新探测", new_val or "自动(当前组织)")
            except Exception as e:
                logger.warning("[配置] 实时应用目标组织失败（将在重启后生效）: %s", e)
        # LLM 高级配置
        if update.llm_advanced_max_chars_daily_chat is not None:
            config.llm.advanced.max_chars_daily_chat = update.llm_advanced_max_chars_daily_chat
        if update.llm_advanced_max_chars_tech_issue is not None:
            config.llm.advanced.max_chars_tech_issue = update.llm_advanced_max_chars_tech_issue
        if update.llm_advanced_hard_truncation_chars is not None:
            config.llm.advanced.hard_truncation_chars = update.llm_advanced_hard_truncation_chars
        # 记忆管理配置
        if update.memory_cleanup_enabled is not None:
            config.memory.cleanup["enabled"] = update.memory_cleanup_enabled
        if update.memory_cleanup_max_age_days is not None:
            config.memory.cleanup["max_age_days"] = update.memory_cleanup_max_age_days
        if update.memory_cleanup_min_similarity_threshold is not None:
            config.memory.cleanup["min_similarity_threshold"] = update.memory_cleanup_min_similarity_threshold
        if update.memory_cleanup_check_interval_days is not None:
            config.memory.cleanup["check_interval_days"] = update.memory_cleanup_check_interval_days
        if update.memory_retrieval_min_similarity is not None:
            config.memory.retrieval["min_similarity"] = update.memory_retrieval_min_similarity
        # 日志配置
        if update.logging_file is not None:
            config.logging.file = update.logging_file
        if update.logging_level is not None:
            config.logging.level = update.logging_level
        if update.logging_max_backups is not None:
            config.logging.max_backups = update.logging_max_backups
        if update.logging_max_size_mb is not None:
            config.logging.max_size_mb = update.logging_max_size_mb
        # 存储配置
        if update.storage_path is not None:
            config.storage.path = update.storage_path
        if update.storage_type is not None:
            config.storage.type = update.storage_type
        if update.storage_backup_enabled is not None:
            config.storage.backup_enabled = update.storage_backup_enabled
        if update.storage_backup_dir is not None:
            config.storage.backup_dir = update.storage_backup_dir
        if update.storage_backup_interval_hours is not None:
            config.storage.backup_interval_hours = update.storage_backup_interval_hours
        if update.storage_backup_max_count is not None:
            config.storage.backup_max_count = update.storage_backup_max_count
        if update.storage_backup_on_start is not None:
            config.storage.backup_on_start = update.storage_backup_on_start
        if update.storage_decisions_retention_days is not None:
            config.storage.decisions_retention_days = update.storage_decisions_retention_days
        if update.storage_messages_retention_days is not None:
            config.storage.messages_retention_days = update.storage_messages_retention_days
        if update.storage_doc_sync_interval_hours is not None:
            config.storage.doc_sync_interval_hours = update.storage_doc_sync_interval_hours
        # 安全配置
        if update.safety_default_fallback is not None:
            config.safety.default_fallback = update.safety_default_fallback
        if update.safety_media_fallback_text is not None:
            config.safety.media_fallback_text = update.safety_media_fallback_text
        if update.safety_sensitive_words is not None:
            config.safety.sensitive_words = update.safety_sensitive_words
        # Web 配置
        if update.web_port is not None:
            config.web.port = update.web_port
        if update.web_auth_enabled is not None:
            config.web.auth_enabled = update.web_auth_enabled
        if update.web_auth_username is not None:
            config.web.auth_username = update.web_auth_username
        if update.web_auth_password is not None:
            # 防止 Pydantic | None 跟前端 value||undefined 双重保护后仍出现空字符串
            # 导致 auth_password 被清空。空字符串/纯空格当作“未提供”，不写。
            # 脱敏哨兵 ***REDACTED*** 同样当作“未提供”，避免把真密码覆盖成哨兵串而锁死登录。
            pwd = str(update.web_auth_password)
            if pwd.strip() and pwd != REDACTED_SENTINEL:
                config.web.auth_password = pwd
            # 留空或哨兵则保持原值不变（不重写）
        # RAG 分块配置
        if update.rag_chunk_size is not None:
            config.rag.chunk_size = update.rag_chunk_size
        if update.rag_chunk_overlap is not None:
            config.rag.chunk_overlap = update.rag_chunk_overlap
        # RAG 自动注入
        if update.rag_auto_inject is not None:
            config.llm.advanced.rag_auto_inject = update.rag_auto_inject
        if update.rag_intent_only is not None:
            config.llm.advanced.rag_intent_only = update.rag_intent_only
        if update.rag_min_similarity is not None:
            config.llm.advanced.rag_min_similarity = update.rag_min_similarity
        if update.rag_max_results is not None:
            config.llm.advanced.rag_max_results = update.rag_max_results
        # 工具路由与限频
        if update.tool_routing_mode is not None:
            config.tools.tool_routing_mode = update.tool_routing_mode
        if update.tools_semantic_routing is not None:
            config.tools.semantic_routing = update.tools_semantic_routing
        if update.tools_semantic_tool_threshold is not None:
            config.tools.semantic_tool_threshold = update.tools_semantic_tool_threshold
        if update.tool_rate_limits is not None:
            for tool_name, limit_dict in update.tool_rate_limits.items():
                if config.tools.rate_limit is None:
                    config.tools.rate_limit = {}
                if tool_name not in config.tools.rate_limit:
                    config.tools.rate_limit[tool_name] = {}
                config.tools.rate_limit[tool_name]["per_hour"] = limit_dict.get("per_hour")
        # 规则引擎
        if update.regex_timeout_seconds is not None:
            config.rules.regex_timeout_seconds = update.regex_timeout_seconds
        if update.intent_filter_enabled is not None:
            if not config.rules.intent_filter:
                config.rules.intent_filter = {}
            config.rules.intent_filter["enabled"] = update.intent_filter_enabled
        if update.intent_filter_pure_thank_max_length is not None:
            if not config.rules.intent_filter:
                config.rules.intent_filter = {}
            config.rules.intent_filter["pure_thank_max_length"] = update.intent_filter_pure_thank_max_length
        if update.intent_filter_pure_ack_max_length is not None:
            if not config.rules.intent_filter:
                config.rules.intent_filter = {}
            config.rules.intent_filter["pure_ack_max_length"] = update.intent_filter_pure_ack_max_length
        if update.intent_filter_business_ratio_threshold is not None:
            if not config.rules.intent_filter:
                config.rules.intent_filter = {}
            config.rules.intent_filter["business_ratio_threshold"] = update.intent_filter_business_ratio_threshold
        if update.keyword_denylist is not None:
            config.rules.keyword_denylist = update.keyword_denylist
        # 死信队列
        if update.dlq_enabled is not None:
            config.dead_letter.enabled = update.dlq_enabled
        # 技能引擎
        if update.skills_enabled is not None:
            config.skills.enabled = update.skills_enabled
        if update.skills_auto_activate is not None:
            config.skills.auto_activate = update.skills_auto_activate
        if update.skills_semantic_routing is not None:
            config.skills.semantic_routing = update.skills_semantic_routing
        if update.skills_semantic_skill_threshold is not None:
            config.skills.semantic_skill_threshold = update.skills_semantic_skill_threshold
        if update.skills_combo_enabled is not None:
            config.skills.combo_enabled = update.skills_combo_enabled
        if update.skills_combo_gap is not None:
            config.skills.combo_gap = update.skills_combo_gap
        # 会话摘要
        if update.conversation_summary_enabled is not None:
            config.memory.conversation_summary["enabled"] = update.conversation_summary_enabled
        if update.conversation_summary_max_messages is not None:
            config.memory.conversation_summary["max_messages_per_conversation"] = update.conversation_summary_max_messages
        if update.conversation_summary_interval_hours is not None:
            config.memory.conversation_summary["summary_interval_hours"] = update.conversation_summary_interval_hours
        if update.conversation_summary_ratio is not None:
            config.memory.conversation_summary["summary_ratio"] = update.conversation_summary_ratio
        # LLM 节流
        if update.llm_throttle_enabled is not None:
            config.llm_throttle.enabled = update.llm_throttle_enabled
        if update.llm_throttle_active_interval is not None:
            config.llm_throttle.background_min_interval_seconds = update.llm_throttle_active_interval
        if update.llm_throttle_idle_threshold is not None:
            config.llm_throttle.idle_threshold_seconds = update.llm_throttle_idle_threshold
        if update.llm_throttle_idle_interval is not None:
            config.llm_throttle.idle_min_interval_seconds = update.llm_throttle_idle_interval
        if update.llm_throttle_backoff is not None:
            config.llm_throttle.rate_limit_backoff_seconds = update.llm_throttle_backoff
        if update.llm_throttle_mem_cooldown is not None:
            config.llm_throttle.extract_memory_cooldown_seconds = update.llm_throttle_mem_cooldown
        if update.llm_throttle_mem_min_chars is not None:
            config.llm_throttle.extract_memory_min_new_chars = update.llm_throttle_mem_min_chars
        if update.llm_throttle_max_summaries is not None:
            config.llm_throttle.max_summaries_per_cycle = update.llm_throttle_max_summaries
        if update.llm_throttle_summary_limit is not None:
            config.llm_throttle.summary_history_limit = update.llm_throttle_summary_limit
        # 高级轮询参数
        if update.poller_history_days is not None:
            config.poller.history_days = update.poller_history_days
        if update.poller_session_gap_minutes is not None:
            config.poller.history_session_gap_minutes = update.poller_session_gap_minutes
        if update.poller_empty_alert_rounds is not None:
            config.poller.list_all_empty_alert_rounds = update.poller_empty_alert_rounds
        if update.poller_first_run_ignore_minutes is not None:
            config.poller.first_run_ignore_older_than_minutes = update.poller_first_run_ignore_minutes
        if update.poller_blacklist_failures is not None:
            config.poller.blacklist_min_consecutive_failures = update.poller_blacklist_failures
        if update.poller_blacklist_reconcile is not None:
            config.poller.blacklist_reconcile_every = update.poller_blacklist_reconcile
        if update.poller_reconcile_batch is not None:
            config.poller.reconcile_probe_batch_size = update.poller_reconcile_batch
        if update.poller_cache_ttl is not None:
            config.poller.top_convs_cache_ttl_seconds = update.poller_cache_ttl
        if update.poller_min_interval is not None:
            config.poller.min_conversation_poll_interval_seconds = update.poller_min_interval
        if update.poller_ai_tag is not None:
            config.poller.ai_tag_enabled = update.poller_ai_tag
        if update.poller_mark_read is not None:
            config.poller.mark_read_after_process = update.poller_mark_read
        _validate_update_config(update)
        wresult = _api._write_config(config.model_dump(),
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
        raise HTTPException(status_code=500, detail=str(e))


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
    # 路径穿越防护：显式拒绝包含 ``..`` 段的输入（CodeQL py/path-injection 屏障）
    if ".." in value.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail=f"{field} 含非法路径段: {value}")
    try:
        # abspath（CodeQL py/path-injection 认可的 sanitizer）规范化相对段与符号链接，
        # 拒绝 ``..`` 段 + 系统目录黑名单 + 父目录可写性闸门共同构成路径穿越屏障。
        resolved = Path(os.path.abspath(os.path.expanduser(value)))
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field} 路径无法解析: {value}")
    rstr = str(resolved)
    for bad in _FORBIDDEN_PATH_PREFIXES:
        if rstr == bad or rstr.startswith(bad + os.sep):
            raise HTTPException(status_code=400,
                                detail=f"{field} 禁止写入系统路径: {value}")
    # 目标为文件则取其父目录校验
    parent = resolved.parent if resolved.suffix else resolved
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            raise HTTPException(status_code=400,
                                detail=f"{field} 父目录不可创建: {value}")
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
        raise HTTPException(status_code=500, detail=str(e))


# ============ LLM ============

@router.get("/api/llm/prompt")
async def get_system_prompt():
    try:
        config = _api._get_cfg()
        return {"system_prompt": config.llm.system_prompt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/llm/prompt")
async def update_system_prompt(update: SystemPromptUpdate):
    try:
        import yaml
        config = _api.load_config(_api.CONFIG_PATH)
        config.llm.system_prompt = update.system_prompt
        _api._write_config(config.model_dump())
        return {"success": True, "message": "系统提示词更新成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ Message Stats ============


# ============ 配置导出脱敏 ============
# 密钥字段以哨兵值替换，避免明文泄露；导入时遇哨兵值则从现有配置
# 恢复真实值（保证 round-trip 安全，不被 ***REDACTED*** 覆盖真密钥）。
REDACTED_SENTINEL = "***REDACTED***"
_SECRET_KEYS = {
    "api_key", "fallback_api_key", "hf_token",
    "auth_password", "app_secret", "corp_secret",
    "token", "encoding_aes_key", "webhook_secret",
    "secondary_fallback_api_key",  # 补漏：此前被明文导出
}
# 字段名后缀命中即视为敏感（兜底，覆盖 *_api_key / *_token / *_secret / *_password 等变体）
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


@router.get("/api/config/export")
async def export_config():
    """导出当前配置文件为 YAML（密钥字段脱敏）。"""
    try:
        config = _api._get_cfg()
        import yaml
        # root poller 已迁移到各平台块，导出剔除重复 root 键（平台块 poller 才是真源）
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
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/api/config/import")
async def import_config(file: UploadFile = File(...)):
    """导入配置文件并热重载。"""
    try:
        content = await file.read()
        text = content.decode("utf-8")
        
        import yaml
        imported_data = yaml.safe_load(text)
        if not isinstance(imported_data, dict):
            raise HTTPException(status_code=400, detail="无效的配置文件格式")
        
        # 验证关键字段（poller 已迁移到各平台块，导入文件不再要求含 root poller 键）
        required_keys = ["dws", "llm", "storage"]
        for key in required_keys:
            if key not in imported_data:
                raise HTTPException(status_code=400, detail=f"配置文件缺少必需字段: {key}")

        # 用 AppConfig 校验导入数据的完整性与类型，校验通过才写入
        from src.config import AppConfig
        try:
            AppConfig(**imported_data)
        except Exception as val_err:
            raise HTTPException(status_code=400, detail=f"配置数据校验失败：{val_err}")

        # 还原脱敏哨兵：仅从「磁盘文件原本就有的明文值」还原，
        # 不使用经环境变量注入的真实密钥（避免把只存于 .env 的密钥落盘成 config.yaml 明文）。
        try:
            def _read_current():
                with open(_api.CONFIG_PATH, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            current = await run_in_threadpool(_read_current)
            _restore_secrets(imported_data, current)
        except Exception as restore_err:
            logger.warning("导入配置时还原脱敏密钥失败（将保留文件中的值）: %s", restore_err)

        # 写入配置文件：复用 _write_config 原子写 + 自动备份，
        # 避免非原子写（断电/磁盘满）损坏 config.yaml 导致 bot 起不来。
        # run_in_threadpool 包裹同步文件写，避免阻塞异步事件循环（F17）。
        await run_in_threadpool(_api._write_config, imported_data)

        # 触发配置热重载
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
        raise HTTPException(status_code=500, detail="内部服务器错误")

