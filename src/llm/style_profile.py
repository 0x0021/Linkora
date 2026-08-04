"""主人风格画像模块。

从 src.llm.style 拆出——风格画像加载与 prompt 生成。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

def load_style_profile(agent: Any) -> dict | None:
    """从当前平台 store 读取自动画像（best-effort）。"""
    if agent.store and hasattr(agent.store, "_memory_ops_repo"):
        try:
            prof = agent.store._memory_ops_repo.get_style_profile()
            if isinstance(prof, dict):
                return prof
        except Exception as e:
            logger.debug("[风格] 读取画像失败: %s", e)
    return None


def get_style_prompt(agent: Any) -> str:
    """获取主人沟通风格画像文本（带一次缓存）。

    优先级：
      1. config.persona_style_prompts[platform_id] 按平台手动覆盖
      2. config.persona_style_prompt 全局手动覆盖
      3. 当前平台 sqlite_store 自动画像
      4. fallback_store（主平台底模）自动画像
    各平台为独立 SQLite DB，自动画像已天然隔离；本方法仅处理
    「当前平台无画像时回退主平台」与「手动覆盖按平台区分」。
    """
    if getattr(agent, "_style_prompt_cache", None) is not None:
        return agent._style_prompt_cache
    result = ""
    cfg = agent.config
    # 1) 按平台手动覆盖（最高优先级）
    platform_overrides = getattr(cfg, "persona_style_prompts", None) or {}
    platform_override = (
        platform_overrides.get(agent.platform_id, "")
        if isinstance(platform_overrides, dict) else ""
    )
    # 2) 全局手动覆盖
    global_override = getattr(cfg, "persona_style_prompt", "") or ""
    if platform_override:
        result = platform_override
    elif global_override:
        result = global_override
    else:
        prof = load_style_profile(agent)
        if not prof and agent.fallback_store is not None and agent.fallback_store is not agent.store:
            # 当前平台无画像 → 回退主平台底模（best-effort，不跨平台硬套）
            try:
                fb = agent.fallback_store._memory_ops_repo.get_style_profile()
                if isinstance(fb, dict) and fb.get("prompt"):
                    prof = fb
                    logger.debug("[风格] 平台 %s 无画像，回退主平台底模", agent.platform_id)
            except Exception as e:
                logger.debug("[风格] 回退主平台底模失败: %s", e)
        # 低置信度（样本过少）→ 退回保守中性风格 + 护栏提示，避免生硬套用不可靠画像
        if prof and isinstance(prof, dict) and prof.get("confidence") == "low":
            logger.info("[风格] 平台 %s 自动画像置信度低，回退中性风格+护栏", agent.platform_id)
            result = _LOW_CONF_NEUTRAL_STYLE + _LOW_CONF_GUARDRAIL
        else:
            result = (prof or {}).get("prompt", "") or ""
    agent._style_prompt_cache = result
    return result

