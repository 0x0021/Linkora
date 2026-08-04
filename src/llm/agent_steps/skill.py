"""技能激活与轮次限制。

从 src.llm.agent._activate_skills / _apply_skill_round_limit 拆出。
"""
from __future__ import annotations

import logging

from src.models import Message
from src.llm import router as _router

logger = logging.getLogger(__name__)


def activate_skills(
    agent,
    message: Message,
    messages: list[dict],
    query_vec: list[float] | None,
) -> list:
    """技能激活检测（Phase 3 组合路由：可能同时激活多个 composable 技能）。

    命中时就地把技能指令注入到 system prompt 之后、history 之前；返回激活列表。
    """
    # 先初始化为空列表：当 skill_router 为 None 或 skills 未启用时，后续路由质量
    # 记录会引用 activated，必须保证已定义，否则 UnboundLocalError。
    activated: list = []
    if agent.skill_router and agent.skills_config.enabled:
        activated = agent.skill_router.route_combo(message.content, query_embedding=query_vec)
        if activated:
            # 【RAG 优先于 web 搜索】RAG 已接地时，抑制 web 搜索类技能，
            # 让 agent 直接基于注入的知识库内容回答，避免无谓联网（含 60s 超时浪费）。
            if _router.rag_grounded_confident(agent):
                suppressed = [
                    m for m in activated
                    if _router.WEB_SEARCH_INTENT_CATEGORIES & set(
                        getattr(agent.skill_router._manager.get(m.name),
                                "intent_categories", []) or [])
                ]
                if suppressed:
                    kept = [m for m in activated if m not in suppressed]
                    logger.info("[Skill] RAG 已接地，抑制 web 搜索技能: %s",
                                ", ".join(m.name for m in suppressed))
                    activated = kept
                    # 同步 skill_router 线程局部状态，使下游工具路由不再视为已激活
                    agent.skill_router._tl.last_matches = kept
                    agent.skill_router._tl.last_match = kept[0] if kept else None
            if activated:
                skill_prompt = agent.skill_router.combine_prompts(activated)
                if skill_prompt:
                    # 将技能指令注入到 system prompt 之后、history 之前
                    messages.insert(1, {"role": "system", "content": skill_prompt})
                primary = activated[0]
                names = ", ".join(m.name for m in activated)
                combo_tag = f" [组合激活 {len(activated)} 个]" if len(activated) > 1 else ""
                logger.info("[Skill] 激活技能: %s (source=%s, score=%.2f)%s",
                            names, primary.source, primary.score, combo_tag)
    return activated


def apply_skill_round_limit(
    agent,
    activated: list,
    max_rounds: int,
    messages: list[dict],
) -> int:
    """技能激活时降低工具轮次上限，并就地注入并行调用提示；返回生效的轮次上限。"""
    # 技能激活时降低轮次上限（技能有明确工具指引，不需要 6 轮探索）
    # 数据查询类任务（westock-data 等）通常 2-3 轮足够：搜索+查询→综合回答
    if activated:
        skill_max = min(max_rounds, 4)  # 技能场景上限 4 轮
        if skill_max < max_rounds:
            logger.info("[工具路由] 技能已激活，轮次上限从 %d 降至 %d", max_rounds, skill_max)
            max_rounds = skill_max
            # 注入并行调用提示，减少串行多轮
            if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "user":
                parallel_hint = {
                    "role": "system",
                    "content": (
                        "【效率要求】当前已激活专业技能。请在同一轮次中**并行发出所有需要的工具调用**"
                        "（如：先 search 拿代码 + 同轮 kline 查行情），"
                        "不要每轮只调一个工具然后等下一轮。目标：最少数量的轮次完成任务。"
                    ),
                }
                # 插入到 user 消息之前（即最后一条 system 级位置）
                messages.insert(-1, parallel_hint)
    return max_rounds
