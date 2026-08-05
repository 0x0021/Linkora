"""主人风格画像页面路由。

端点：
- GET  /api/persona                 读画像（自动 + 手动覆盖 + few-shot + 主人名）
- POST /api/persona/override        保存手动覆盖（支持 platform 作用域）
- POST /api/persona/few-shot        整体替换 few-shot
- POST /api/persona/few-shot/adopt  一键采纳单条推荐样例（增量落库、去重）
- POST /api/persona/reanalyze       重新抽样自动画像
- GET  /api/persona/versions         列出画像历史版本（#3 画像版本管理）
- GET  /api/persona/versions/{id}    读取某历史版本完整画像（对比查看）
- POST /api/persona/versions/rollback 回滚到指定历史版本（当前版自动归档）

复用既有：
- sqlite_store.compute_style_profile / get_style_profile / save_style_profile
- AppConfig.persona_style_prompt / persona_style_prompts / few_shot_examples
- main.reload_config 清理 agent._style_prompt_cache
"""

from __future__ import annotations

import re
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from src.config import AppConfig
from src.shared_state import get_app_instance
from src.utils.llm_json import extract_json
from web.dependencies import get_store as _dep_get_store, get_current_platform, logger

router = APIRouter()


class PersonaOverrideUpdate(BaseModel):
    """手动覆盖开关 + 文本。
    platform：空 / 'all' / 'global' 表示全局覆盖；否则按平台覆盖（写入 persona_style_prompts[platform]）。
    """
    enabled: bool
    prompt: str = ""
    platform: str = ""


class PersonaFewShotUpdate(BaseModel):
    """整体替换 few-shot 样例列表。platform 指定写入哪个平台（默认当前平台）。"""
    examples: list[dict] = Field(default_factory=list)
    platform: str = ""


class PersonaFewShotAdopt(BaseModel):
    """一键采纳单条推荐样例。platform 指定写入哪个平台（默认当前平台）。"""
    example: dict = Field(default_factory=dict)
    platform: str = ""


class PersonaVersionRollback(BaseModel):
    """回滚到指定历史版本（version_id = style_profile_versions.id）。"""
    version_id: int


def _load_config() -> AppConfig:
    """惰性读 config（优先共享单例，回退磁盘，与 web.api._get_cfg 同语义）。"""
    from web.api import _get_cfg
    cfg = _get_cfg()
    if cfg is None:
        # 极少见：配置文件读不到。回退到新实例，避免上层崩溃
        return AppConfig()
    return cfg


def _owner_name(platform: str | None = None) -> str:
    """主人姓名：按平台取对应 PlatformContext 的 current_user_name。

    平台隔离核心：每平台有独立 poller，current_user_name 不同（钉钉/微信/飞书
    主人可能不同）。缺省 platform 时读请求级平台上下文（由 web.api 中间件写入）。
    """
    if platform is None:
        try:
            platform = get_current_platform()
        except Exception:
            logger.warning("[resilience] silent exception in _owner_name", exc_info=True)
            platform = "dingtalk"
    inst = get_app_instance()
    if inst is None:
        return ""
    ctx = getattr(inst, "platforms", {}).get(platform)
    if ctx is not None:
        name = getattr(ctx, "current_user_name", "") or ""
        if name:
            return name
    # 兜底：运行期实例级单值（主平台 dingtalk 场景）
    return getattr(inst, "current_user_name", "") or ""


def _get_store(platform: str | None = None):
    """平台感知的 store：自动落到对应平台的独立 DB（数据隔离）。

    通过 web.dependencies.get_store() 走请求级平台上下文，无需显式传 platform
    （前端 api.js 已自动给所有请求追加 ?platform=）。
    """
    try:
        return _dep_get_store(platform)
    except Exception as e:
        logger.warning("[persona] 取平台 store 失败，回退单例: %s", e)
        inst = get_app_instance()
        if inst is None:
            return None
        return getattr(inst, "store", None)


def _save_and_reload(new_config: AppConfig) -> None:
    """把更新后的 config 落盘 + 触发热重载（清 agent._style_prompt_cache）。"""
    from web.api import _write_config
    _write_config(new_config.model_dump(),
                  changed_keys={"llm"})
    try:
        from src.shared_state import get_config_reload_callback
        cb = get_config_reload_callback()
        if cb:
            cb()
    except Exception as e:
        logger.warning("[persona] 触发配置重载失败（不阻塞返回）: %s", e)


def _compute_freshness(updated_at_iso: str | None) -> dict:
    """画像新鲜度：由 updated_at 推算距今天数与是否建议重算（>=30 天）。"""
    from datetime import datetime as _dt

    result = {"updated_at": updated_at_iso, "days_since_update": None, "stale": False}
    if not updated_at_iso:
        return result
    try:
        dt = _dt.fromisoformat(updated_at_iso.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        days = (_dt.now() - dt).days
        result["days_since_update"] = days
        result["stale"] = days >= 30
    except Exception:
        logger.warning("[resilience] silent exception in _compute_freshness", exc_info=True)
    return result


# ── LLM 画像生成 ──────────────────────────────────────────
_LLM_PERSONA_PROMPT = """你是沟通风格分析专家。下面是{owner}真实发出的{n}条历史消息（已清洗去噪）。
请分析这些消息，为{owner}的数字分身生成一段简洁的沟通风格画像提示词（2~4 句话）。

要求：
1. 只提炼语气倾向、用词习惯、开场方式、情绪温度等「口吻」特征
2. 用"你"的第二人称描述（将注入数字分身的 system prompt）
3. 不要罗列统计数据，用自然语言描述
4. 不要编造没有证据的特征
5. 不要描述回复长度 / 格式 / 结构（长度由系统统一约束；画像只管口吻，避免与回复铁律冲突）

消息样本：
{samples}

统计辅助：平均{avg_len}字 | emoji率{emoji_rate} | 礼貌词率{polite_rate} | 口语词率{casual_rate} | 短回复率{short_rate}

请直接输出画像提示词，不要加任何前缀说明。"""


def _enrich_with_llm(profile: dict, owner: str) -> dict:
    """用 LLM 生成画像描述，失败时回退到统计拼接。"""
    samples = profile.pop("sample_messages", None)
    if not samples or not owner:
        return profile

    cfg = None
    try:
        cfg = _load_config()
    except Exception:
        logger.warning("[resilience] silent exception in _enrich_with_llm", exc_info=True)
    if not cfg:
        return profile

    try:
        from src.llm.client import LLMClient
        from src.memory.sqlite_store import _redact_pii
        client = LLMClient(cfg.llm)
        # 隐私护栏：送 LLM 前先脱敏样本，避免 PII 泄露给外部模型
        samples_text = "\n".join(f"- {_redact_pii(s)}" for s in samples[:50])
        user_msg = _LLM_PERSONA_PROMPT.format(
            owner=owner,
            n=len(samples),
            samples=samples_text,
            avg_len=profile.get("avg_len", 0),
            emoji_rate=profile.get("emoji_rate", 0),
            polite_rate=profile.get("polite_rate", 0),
            casual_rate=profile.get("casual_rate", 0),
            short_rate=profile.get("short_rate", 0),
        )
        messages = [
            {"role": "system", "content": "你是沟通风格分析专家，擅长从对话样本中提炼人物口吻特征。"},
            {"role": "user", "content": user_msg},
        ]
        resp = client.chat(messages, temperature=0.3)
        llm_prompt = (resp.content or "").strip()
        if llm_prompt and len(llm_prompt) > 10:
            # 隐私护栏（#6 升级）：先脱敏样本，二次脱敏画像文本，再做残留校验——
            # 若 LLM 在画像中「编造」出新的手机号/地址等 PII，残校会发现并再次脱敏，
            # 同时记 warning 便于监控护栏被触发的频率。
            redacted = _redact_pii(llm_prompt)
            from src.memory.sqlite_store import _has_residual_pii
            if _has_residual_pii(redacted):
                redacted = _redact_pii(redacted)
                logger.warning("[persona] LLM 画像脱敏后仍有 PII 残留，已二次脱敏")
            profile["prompt"] = redacted
            profile["prompt_source"] = "llm"
            logger.info("[persona] LLM 画像生成成功 (len=%d)", len(llm_prompt))
        else:
            logger.warning("[persona] LLM 返回内容过短，保留统计画像")
    except Exception as e:
        logger.warning("[persona] LLM 画像生成失败，回退统计画像: %s", e)

    return profile


@router.get("/api/persona")
async def get_persona():
    """读主人风格画像完整状态。

    首次访问（DB 中无自动画像、但主人已有历史消息）→ 主动跑一次 compute 并落盘，
    避免前端看到一片「—」还需要手点「重新分析」。
    """
    try:
        def _work():
            cfg = _load_config()
            platform = None
            try:
                platform = get_current_platform()
            except Exception:
                logger.warning("[resilience] silent exception in get_persona", exc_info=True)
                platform = "dingtalk"
            store = _get_store(platform)
            owner = _owner_name(platform)

            # ---- 抽取来源元数据：扫描当前平台 messages 表，让前端直观看到“画像从哪来” ----
            # 不依赖 store 封装方法，直接轻量 count（总条数 + 主人发出的条数），失败则降级为 None。
            source = {
                "platform": platform,
                "owner": owner,
                "total_messages": None,
                "owner_messages": None,
                "sample_limit": 1000,  # 与 sqlite_store.compute_style_profile 默认 sample_limit 对齐（随机抽样上限）
            }
            if store and hasattr(store, "conn"):
                try:
                    cur_platform = get_current_platform()
                    msg_repo = store._message_repo
                    source["total_messages"] = msg_repo.count_messages_with_content(
                        platform=cur_platform
                    )
                    if owner:
                        source["owner_messages"] = msg_repo.count_messages_from_sender(
                            owner, platform=cur_platform
                        )
                except Exception as e:
                    logger.debug("[persona] 读消息计数失败（不影响返回）: %s", e)

            # 自动画像（DB 里的最新一份）
            auto_profile: dict = {}
            if store and hasattr(store, "_memory_ops_repo"):
                try:
                    auto_profile = store._memory_ops_repo.get_style_profile() or {}
                except Exception as e:
                    logger.debug("[persona] 读自动画像失败: %s", e)

            # 首访自动生成：如果 auto_profile 为空且 store 支持 compute、且有 owner
            # → 后台同步跑一次 compute + LLM 画像 + save（仅限于冷启动场景）
            if (
                not auto_profile
                and store
                and owner
                and hasattr(store, "_memory_ops_repo")
                and hasattr(store, "_memory_ops_repo")
            ):
                try:
                    prof = store._memory_ops_repo.compute_style_profile(owner)
                    if prof:
                        prof = _enrich_with_llm(prof, owner)
                        _cfg = _load_config()
                        max_v = getattr(_cfg.llm, "persona_history_max_versions", 10) or 10
                        store._memory_ops_repo.save_style_profile(prof, max_versions=max_v)
                        auto_profile = prof
                        logger.info("[persona] 首访自动生成画像成功 sample_count=%s", prof.get("sample_count"))
                except Exception as e:
                    logger.debug("[persona] 首访自动生成失败（不影响返回）: %s", e)

            # 手动覆盖状态：全局 persona_style_prompt + 按平台 persona_style_prompts
            # 优先级：平台专属 > 全局（与 agent._get_style_prompt 一致）。
            global_override = (getattr(cfg.llm, "persona_style_prompt", "") or "")
            platform_overrides = getattr(cfg.llm, "persona_style_prompts", {}) or {}
            platform_specific = ""
            if platform and isinstance(platform_overrides, dict):
                platform_specific = platform_overrides.get(platform, "") or ""
            effective_override = platform_specific if platform_specific else global_override
            override_enabled = bool(effective_override.strip())

            return {
                "owner": owner,
                "source": source,                    # 抽取来源元数据：平台/主人名/扫描消息数/抽样上限
                "auto_profile": auto_profile,        # {sample_count, avg_len, emoji_rate, ...}
                "freshness": _compute_freshness(auto_profile.get("updated_at")),  # 时效：距今天数/是否建议重算
                "override": {
                    "enabled": override_enabled,
                    "prompt": effective_override,                # 当前平台生效文本（平台 > 全局）
                    "global_prompt": global_override,            # 全局覆盖文本
                    "platform_prompt": platform_specific,        # 当前平台专属覆盖（如有）
                    "platform": platform,
                    "is_platform_specific": bool(platform_specific),  # 当前平台是否走专属覆盖
                },
            # few-shot 按平台隔离：读取本平台 DB 中的样例（与画像同库）。
            # 首次迁移：若本平台 DB 为空且全局 config 有遗留样例，则把全局样例
            # 种子化进主平台(dingtalk) DB（一次性、幂等），避免已有数据丢失。
            "few_shot_examples": _read_few_shot_for_platform(store, platform, cfg),
            # ---- #5 冷启动引导：低置信度画像给出「继续积累消息」的引导 ----
            # 阈值与 compute_style_profile 对齐：≥150 high / 30~149 medium / <30 low。
            "cold_start": _build_cold_start(auto_profile, source.get("owner_messages")),
        }
        return await run_in_threadpool(_work)
    except Exception as e:
        logger.error("[persona] 读画像失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


def _build_cold_start(auto_profile: dict, owner_messages) -> dict:
    """低置信度画像的冷启动引导数据。

    返回进度与目标缺口，前端据此展示引导卡片（进度条 + 达中等/高所需条数 + 重新分析建议）。
    当画像尚为空（无分析）时标记 is_cold=True 并透出 owner_messages，引导用户先积累消息。
    """
    _conf = (auto_profile or {}).get("confidence", "")
    _cc = int((auto_profile or {}).get("cleaned_count", 0) or 0)
    _has = bool(auto_profile)
    _is_cold = (not _has) or _conf == "low"
    try:
        _owner_msg = int(owner_messages) if owner_messages is not None else None
    except Exception:
        logger.warning("[resilience] silent exception in _build_cold_start", exc_info=True)
        _owner_msg = None
    return {
        "is_cold": _is_cold,
        "has_profile": _has,
        "confidence": _conf,
        "cleaned_count": _cc,
        "owner_messages": _owner_msg,
        "needed_for_medium": max(0, 30 - _cc),
        "needed_for_high": max(0, 150 - _cc),
        "progress_to_medium": min(100, round(_cc / 30 * 100)) if _cc else 0,
        "progress_to_high": min(100, round(_cc / 150 * 100)) if _cc else 0,
    }


@router.post("/api/persona/override")
async def update_persona_override(update: PersonaOverrideUpdate):
    """保存手动覆盖（enabled=false 时清空文本并禁用）。

    platform 指定时按平台覆盖（写入 persona_style_prompts[platform]），
    否则写入全局 persona_style_prompt。多平台隔离：各平台可独立设定口吻覆盖。
    """
    try:
        cfg = _load_config()
        plat = (update.platform or "").strip().lower()
        is_global = plat in ("", "all", "global")
        text = (update.prompt or "").strip() if update.enabled else ""
        if is_global:
            cfg.llm.persona_style_prompt = text
        else:
            if text:
                # 确保字段存在且为 dict（防御旧配置缺字段）
                if not isinstance(cfg.llm.persona_style_prompts, dict):
                    cfg.llm.persona_style_prompts = {}
                cfg.llm.persona_style_prompts[plat] = text
            else:
                # 禁用 → 移除该平台专属覆盖，回退到全局
                if isinstance(cfg.llm.persona_style_prompts, dict):
                    cfg.llm.persona_style_prompts.pop(plat, None)
        _save_and_reload(cfg)
        # 返回当前生效文本与作用域
        if is_global:
            effective = cfg.llm.persona_style_prompt
            scope = "global"
        else:
            effective = cfg.llm.persona_style_prompts.get(plat, "") if isinstance(cfg.llm.persona_style_prompts, dict) else ""
            scope = "platform" if plat in (cfg.llm.persona_style_prompts or {}) else "global"
        return {
            "success": True,
            "enabled": bool(effective),
            "prompt": effective,
            "scope": scope,
            "platform": plat,
        }
    except Exception as e:
        logger.error("[persona] 保存手动覆盖失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/persona/few-shot")
async def update_few_shot(update: PersonaFewShotUpdate):
    """整体替换 few-shot 样例（前端用单条编辑也走这里，传全量列表）。

    按平台隔离：样例写入【当前平台】的 DB（kv 表），不再写入全局 config。
    各平台独立 SQLite DB，天然隔离。
    """
    try:
        def _work():
            platform = _resolve_platform(update.platform)
            store = _get_store(platform)
            if store is None or not hasattr(store, "_few_shot_repo"):
                raise HTTPException(status_code=500, detail="store 不支持 few-shot 写入")
            store._few_shot_repo.set_few_shot_examples(update.examples or [])
            logger.info("[persona] 平台[%s] few-shot 已整体替换为 %d 条", platform, len(update.examples or []))
            return {"success": True, "count": len(update.examples or []), "platform": platform}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 保存 few-shot 失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/persona/few-shot/adopt")
async def adopt_few_shot(payload: PersonaFewShotAdopt):
    """一键采纳单条推荐样例：追加进【当前平台】DB 的 few-shot（去重）。

    与「整体替换」端点不同，采纳是增量追加——不影响已有样例，适合从推荐列表
    逐条挑选用例直接入库，免去先编辑再整体保存的往返。
    """
    try:
        ex = payload.example or {}
        if not isinstance(ex, dict):
            raise HTTPException(status_code=400, detail="example 必须是 {user, assistant}")
        u = (ex.get("user") or "").strip()
        a = (ex.get("assistant") or "").strip()
        if not u or not a:
            raise HTTPException(status_code=400, detail="样例 user/assistant 不能为空")

        def _work():
            platform = _resolve_platform(payload.platform)
            store = _get_store(platform)
            if store is None or not hasattr(store, "_few_shot_repo"):
                raise HTTPException(status_code=500, detail="store 不支持 few-shot 写入")
            count = store._few_shot_repo.append_few_shot_example({"user": u, "assistant": a})
            logger.info("[persona] 平台[%s] 采纳 few-shot，当前共 %d 条", platform, count)
            return {"success": True, "adopted": True, "count": count, "platform": platform}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 采纳 few-shot 失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


def _resolve_platform(platform: str | None) -> str:
    """解析端点传入的 platform：空则取请求级平台上下文。"""
    p = (platform or "").strip().lower()
    if not p or p in ("all", "global"):
        try:
            return get_current_platform()
        except Exception:
            logger.warning("[resilience] silent exception in _resolve_platform", exc_info=True)
            return "dingtalk"
    return p


def _read_few_shot_for_platform(store, platform: str, cfg) -> list[dict]:
    """读取某平台的 few-shot 样例（平台 DB 优先）。

    首次迁移：主平台(dingtalk) DB 为空且全局 config 有遗留样例时，
    种子化进 DB（幂等，仅在 DB 为空时执行），避免既有数据丢失。
    """
    if store is not None and hasattr(store, "_few_shot_repo"):
        examples = store._few_shot_repo.get_few_shot_examples()
        if examples:
            return examples
        # 一次性迁移：仅主平台从全局 config 继承（其他平台本就该从空起步）
        if platform == "dingtalk":
            legacy = getattr(cfg.llm, "few_shot_examples", []) or []
            if legacy:
                try:
                    store._few_shot_repo.set_few_shot_examples(legacy)
                    logger.info("[persona] 迁移全局 few-shot %d 条 → 主平台 DB", len(legacy))
                except Exception as e:
                    logger.warning("[persona] 迁移 few-shot 失败（忽略）: %s", e)
                return legacy
        return []


def _count_owner_messages(store, owner: str) -> int:
    """统计主人发出的历史消息数（用于回测基线的样本量标注）。"""
    try:
        return int(store._message_repo.count_messages_from_sender(
            owner, platform=get_current_platform()
        ))
    except Exception:
        logger.warning("[resilience] silent exception in _count_owner_messages", exc_info=True)
        return 0


@router.post("/api/persona/reanalyze")
async def reanalyze():
    """重新从主人历史消息抽取自动画像（写入 style_profiles 表）。

    流程：compute 统计 + 清洗 → LLM 生成画像描述 → 落盘。
    LLM 失败时回退到统计拼接的 prompt。
    """
    try:
        def _work():
            store = _get_store()
            owner = _owner_name()
            if not store:
                raise HTTPException(status_code=400, detail="存储未就绪")
            if not owner:
                raise HTTPException(status_code=400, detail="主人姓名未配置（current_user_name 为空）")
            if not hasattr(store, "_memory_ops_repo"):
                raise HTTPException(status_code=500, detail="store 不支持 compute_style_profile")
            prof = store._memory_ops_repo.compute_style_profile(owner, platform=get_current_platform())
            if not prof:
                return {"success": False, "message": "历史消息中找不到主人发出的样本，无法生成画像"}
            prof = _enrich_with_llm(prof, owner)
            _cfg = _load_config()
            max_v = getattr(_cfg.llm, "persona_history_max_versions", 10) or 10
            store._memory_ops_repo.save_style_profile(prof, max_versions=max_v)
            return {"success": True, "profile": prof}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 重新分析失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/persona/versions")
async def list_persona_versions(limit: int = 20):
    """列出风格画像历史版本（version_no 倒序，最新在前），供前端对比/回滚。"""
    try:
        def _work():
            store = _get_store()
            if not store or not hasattr(store, "_memory_ops_repo"):
                raise HTTPException(status_code=500, detail="store 不支持版本管理")
            versions = store._memory_ops_repo.list_style_profile_versions(limit=min(int(limit), 100))
            return {"success": True, "versions": versions}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 读版本列表失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/persona/versions/{version_id}")
async def get_persona_version(version_id: int):
    """读取某一历史版本的完整画像（含 prompt / 各维度指标），用于对比查看。"""
    try:
        def _work():
            store = _get_store()
            if not store or not hasattr(store, "_memory_ops_repo"):
                raise HTTPException(status_code=500, detail="store 不支持版本管理")
            prof = store._memory_ops_repo.get_style_profile_version(int(version_id))
            if prof is None:
                raise HTTPException(status_code=404, detail="版本不存在")
            return {"success": True, "version_id": int(version_id), "profile": prof}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 读版本详情失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/persona/versions/rollback")
async def rollback_persona_version(req: PersonaVersionRollback):
    """回滚到指定历史版本（当前版本自动归档进历史，不丢失）。"""
    try:
        def _work():
            store = _get_store()
            if not store or not hasattr(store, "_memory_ops_repo"):
                raise HTTPException(status_code=500, detail="store 不支持版本管理")
            ok = store._memory_ops_repo.rollback_style_profile(int(req.version_id))
            if not ok:
                raise HTTPException(status_code=404, detail="版本不存在或回滚失败")
            # 回滚后读取最新画像返回，便于前端直接刷新
            prof = store._memory_ops_repo.get_style_profile() or {}
            return {"success": True, "profile": prof, "rollback_to": int(req.version_id)}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 回滚版本失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/persona/backtest")
def backtest(limit: int = 6):
    """客观还原度回测（best-effort）。

    留出主人近期 (user→owner) 配对，用【当前生效画像】让 LLM 克隆回复，
    再由 LLM 评委对「克隆口吻 vs 主人真实口吻」打 0~100 分，返回均分与逐条明细。
    用于量化数字分身有多像本人，而非主观判断。

    声明为同步 `def`：函数体是「逐条 LLM 克隆 + LLM 评委打分」的串行循环
    （最多 10 轮，每轮两次 LLM 往返，分钟级），Starlette 会自动放入线程池，
    否则一次回测会把整个管理端 UI 冻住。
    """
    try:
        limit = max(1, min(limit, 500))
        store = _get_store()
        platform = None
        try:
            platform = get_current_platform()
        except Exception:
            logger.warning("[resilience] silent exception in backtest", exc_info=True)
            platform = "dingtalk"
        owner = _owner_name(platform)
        if not store or not owner:
            raise HTTPException(status_code=400, detail="存储未就绪或主人未配置")
        if not hasattr(store, "_baseline_repo"):
            raise HTTPException(status_code=500, detail="store 不支持 recommend_few_shot_pairs")
        limit = max(1, min(int(limit), 10))  # 安全上限 10 条，避免 LLM 调用过多

        # 当前生效 agent（与线上同实例）：回测克隆复用 agent._build_system_prompt，
        # 使「被打分的克隆回复」与「线上真实回复」使用同一条人设管线（含 dynamic_few_shot
        # 动态样例、style_prompt、护栏），从而评分 = 真实回复的人设保真度，而非另一条独立克隆通道。
        inst = get_app_instance()
        agent = getattr(inst, "platforms", {}).get(platform, None) if inst else None
        agent = getattr(agent, "llm_agent", None) if agent else None

        # 排除已采纳样例（按平台隔离：读本平台 DB），回测聚焦未覆盖的真实配对
        cfg = _load_config()
        adopted = _read_few_shot_for_platform(store, platform, cfg)
        pairs = store._baseline_repo.recommend_few_shot_pairs(owner, limit=limit, exclude=adopted)
        if not pairs:
            return {"success": False, "message": "未找到足够的 (用户→主人) 配对样本"}

        from src.llm.client import LLMClient
        client = LLMClient(cfg.llm)
        if not getattr(cfg.llm, "api_key", ""):
            raise HTTPException(status_code=400, detail="LLM 未配置 api_key，无法回测")

        details = []
        scores = []
        fail_reasons: list[str] = []
        for p in pairs:
            user_msg = (p.get("user") or "").strip()
            truth = (p.get("assistant") or "").strip()
            if not user_msg or not truth:
                continue
            # 1) 克隆回复：复用生产人设管线（agent._build_system_prompt），与线上真实回复
            #    同构；exclude 当前 (user,truth) 防动态检索把真相当 few-shot 注入造成虚高。
            clone = _clone_reply_production(client, cfg, agent, user_msg, truth)
            # 2) 评委打分
            score, reason = _judge_clone(client, cfg, owner, clone, truth)
            if score is not None:
                scores.append(score)
                details.append({
                    "user": user_msg,
                    "truth": truth,
                    "clone": clone,
                    "score": score,
                    "reason": reason,
                })
            else:
                fail_reasons.append(reason or "未知原因")
        if not scores:
            # 把评委失败的具体原因回传前端，避免只给一句无从下手的「LLM 不可用」
            uniq = list(dict.fromkeys(fail_reasons))[:3]
            detail = ("；".join(uniq)) if uniq else "LLM 不可用或返回异常"
            return {"success": False, "message": f"回测执行失败：{detail}（共 {len(pairs)} 条样本全部评分失败）"}
        mean = round(sum(scores) / len(scores), 1)
        # 记录回测基线（平台隔离：写入本平台 DB），供趋势追踪
        if store is not None and hasattr(store, "_baseline_repo"):
            try:
                sample_count = _count_owner_messages(store, owner)
                store._baseline_repo.record_backtest(mean_score=mean, count=len(scores), sample_count=sample_count)
            except Exception as e:
                logger.debug("[persona] 记录回测基线失败（不影响返回）: %s", e)
        return {
            "success": True,
            "mean_score": mean,
            "count": len(scores),
            "details": details,
        }
    except HTTPException:
        raise
    except Exception as e:
        # 详细 stacktrace（包含了 LLM 调用链上下文）
        logger.error("[persona] 回测失败: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


def _clone_reply_production(client, cfg, agent, user_msg: str,
                             truth: str | None = None) -> str:
    """用生产（真实）人设管线生成克隆回复，使回测分数 = 真实回复的人设保真度。

    直接复用 agent._build_system_prompt —— 与线上 agent 同一条 system prompt 组装
    逻辑（含 dynamic_few_shot 动态样例检索、style_prompt、护栏），仅把「生成」调用
    从 agent 主循环换成单次 client.chat（不含 RAG 注入 / 工具调用噪声），专注衡量
    「人设口吻」本身。这样被打分的克隆回复与线上真实回复同构，评分即真实人设保真度，
    而非另一条独立克隆通道——避免「分数涨了但真实回复人设没变」的失真。

    truth 用于 exclude 防真相泄漏：避免 dynamic_few_shot 把当前 (user,truth) 配对当
    few-shot 注入，导致克隆「抄答案」虚高。agent 为 None（bot 未运行）时返回空串。
    """
    if agent is None:
        return ""
    try:
        query_embedding = None
        llm_cfg = getattr(agent, "config", None)
        dyn = getattr(llm_cfg, "dynamic_few_shot", False) if llm_cfg else False
        if dyn:
            try:
                embed_fn = getattr(agent, "_embed_message", None)
                if embed_fn:
                    query_embedding = embed_fn(user_msg)
            except Exception as e:
                logger.debug("[persona] 回测 embedding 计算失败: %s", e)
        exclude = [{"user": user_msg, "assistant": truth}] if truth else None
        sys = agent._build_system_prompt(
            user_query=user_msg,
            query_embedding=query_embedding,
            exclude=exclude,
        )
        temperature = float(getattr(llm_cfg, "temperature", 0.3) or 0.3)
        resp = client.chat([
            {"role": "system", "content": sys},
            {"role": "user", "content": user_msg},
        ], temperature=temperature)
        return (resp.content or "").strip()[:600]
    except Exception as e:
        logger.debug("[persona] 生产管线克隆回复失败: %s", e)
        return ""


def _judge_clone(client, cfg, owner: str, clone: str, truth: str):
    """LLM 评委：对克隆口吻与真实口吻的贴合度打分 0~100。返回 (score, reason)。

    严格模式（默认）：字面口吻/用词重合度，口语极简回复容易低分。
    宽松模式（cfg.llm.backtest_judge_loose=True）：改为评估「意图匹配 + 风格类别
    一致」，容忍措辞差异，避免主人极简口语被过度惩罚（还原度回测更贴近真实观感）。
    """
    if not clone:
        return None, "克隆回复为空，无法评分"
    loose = getattr(cfg.llm, "backtest_judge_loose", False) if getattr(cfg, "llm", None) else False
    if loose:
        sys = (
            "你是严格的沟通风格评委，但采用宽松口径。对比「数字分身克隆回复」与「主人真实回复」，"
            "只要两者在「语义意图」和「风格类别（极简/详细/命令式/反问/口语化）」上一致即可给高分，"
            "不必要求用词字面相同。评判要点：\n"
            "1) 意图是否一致（都回答了同一件事、给了同类信息）；\n"
            "2) 风格类别是否一致（主人极简则克隆也极简，主人详尽则克隆也详尽）；\n"
            "3) 容忍措辞/同义替换，不因字面不同扣分。\n"
            "给出 0~100 的整数分（100=意图与风格类别完全一致）。"
        )
    else:
        sys = "你是严格的沟通风格评委。对比「数字分身克隆回复」与「主人真实回复」，评估口吻/语气/用词习惯的贴合度，给出 0~100 的整数分（100=完全一致）。"
    usr = (
        f"主人真实回复：\n「{truth}」\n\n"
        f"数字分身克隆回复：\n「{clone}」\n\n"
        "请只输出 JSON：{\"score\": <0-100整数>, \"reason\": \"<一句话理由>\"}\n"
        "不要使用 markdown 代码块包裹，不要输出 JSON 以外的任何解释文字。"
    )
    try:
        resp = client.chat([
            {"role": "system", "content": sys},
            {"role": "user", "content": usr},
        ], temperature=0.2)
        text = (resp.content or "").strip()
        if not text:
            # 推理模型常把正文全塞进 reasoning_content（客户端已剥离），content 为空
            logger.warning("[persona] 评委返回空内容（疑似推理模型只输出 reasoning_content）")
            return None, "评委返回空内容"
        score, reason = _parse_judge_output(text)
        if score is None:
            return None, "评分解析失败"
        return score, reason
    except Exception as e:
        logger.debug("[persona] 评委打分失败: %s", e)
        return None, "评委调用失败"


def _parse_judge_output(text: str):
    """解析评委返回文本 → (score, reason)；无法解析返回 (None, 原因)。

    LLM「只输出 JSON」并不可靠（markdown 围栏 / 前后缀寒暄 / 思考链标签都出现过），
    故三级降级：
      1) extract_json 稳健抽取 JSON 对象（去围栏 + 扫描首个 {...}）；
      2) 正则抓 "score": NN 键值对（JSON 被截断时仍可救回分数）；
      3) 正则抓首个 0~100 整数，reason 退化为原文摘要。
    任何一级失败都不抛异常、不打 traceback —— 这是预期内的模型措辞抖动，
    不是程序缺陷，打整段 traceback 只会污染日志（历史上曾被误读为崩溃）。
    """
    obj = extract_json(text)
    if isinstance(obj, dict) and "score" in obj:
        try:
            score = int(float(obj.get("score")))
        except (TypeError, ValueError):
            score = -1
        reason = str(obj.get("reason", "") or "").strip()
        if 0 <= score <= 100:
            return score, reason
    # 降级：记录原文（截断）便于定位是哪种污染形态，而非丢一整屏 traceback
    logger.warning("[persona] 评委未返回合法 JSON，降级正则解析；原文=%r", text[:200])
    m = re.search(r'"?score"?\s*[:：=]\s*(\d{1,3})', text) or re.search(r"\b(\d{1,3})\b", text)
    if not m:
        return None, "评分解析失败"
    score = int(m.group(1))
    if not (0 <= score <= 100):
        return None, "评分解析失败"
    r = re.search(r'"?reason"?\s*[:：]\s*"?([^"\n}]{2,80})', text)
    return score, (r.group(1).strip() if r else text[:80])


@router.get("/api/persona/backtest/history")
async def backtest_history(limit: int = 30):
    """读取口吻还原度回测趋势（平台隔离：读本平台 DB 的基线历史）。

    返回按时间升序的基线序列，前端用于画趋势曲线：分数随样本量/画像成熟度增长的变化。
    """
    try:
        def _work():
            limit_ = max(1, min(limit, 500))
            platform = None
            try:
                platform = get_current_platform()
            except Exception:
                logger.warning("[resilience] silent exception in backtest_history", exc_info=True)
                platform = "dingtalk"
            store = _get_store(platform)
            if store is None or not hasattr(store, "_baseline_repo"):
                raise HTTPException(status_code=500, detail="store 不支持回测历史")
            history = store._baseline_repo.get_backtest_history(limit=limit_)
            return {"success": True, "platform": platform, "count": len(history), "history": history}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 读回测历史失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/persona/recommend-few-shot")
async def recommend_few_shot(limit: int = 6):
    """从主人历史对话推荐高质量 few-shot 样例（user→assistant 配对）。

    返回 Top-N 候选对，前端「从我的历史推荐样例」按钮调用后可一键采纳。
    """
    try:
        def _work():
            limit_ = max(1, min(limit, 500))
            store = _get_store()
            owner = _owner_name()
            if not store:
                raise HTTPException(status_code=400, detail="存储未就绪")
            if not owner:
                raise HTTPException(status_code=400, detail="主人姓名未配置（current_user_name 为空）")
            if not hasattr(store, "_baseline_repo"):
                raise HTTPException(status_code=500, detail="store 不支持 recommend_few_shot_pairs")
            # 获取当前平台上下文
            platform = "dingtalk"
            try:
                platform = get_current_platform()
            except Exception:
                pass
            # 排除已采纳样例，避免推荐列表中重复出现用户已采纳的 pair
            cfg = _load_config()
            adopted = _read_few_shot_for_platform(store, platform, cfg)
            examples = store._baseline_repo.recommend_few_shot_pairs(owner, limit=limit_, exclude=adopted)
            return {"success": True, "count": len(examples), "examples": examples}
        return await run_in_threadpool(_work)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[persona] 推荐 few-shot 失败: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
