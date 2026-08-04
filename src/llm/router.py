"""工具路由模块（smart / all / keyword）。

从 ``src.llm.agent`` 拆出——工具路由 + 过期结果检测全部归此。``LLMAgent``
上同名方法作为 1 行委托保留。

设计要点：
- **三类路由模式**（``_resolve_routing_mode``）：``"smart"``（默认）/ ``"all"`` /
  ``"keyword"``。``smart`` 用关键词 + 语义 + 技能契约 + 主动行动意图做精准暴露，
  无命中时回退基础 + 意图工具（已排除检索类避免弱模型循环搜）。
- **技能契约隔离**：技能激活且声明 ``allowed_tools`` 时独占工具暴露；
  技能未激活时排除技能自动包装工具，避免与同领域内置工具重复。
- **过期结果 TTL**：见 ``_TOOL_RESULT_TTL``，默认 10 分钟；用于提醒 LLM 重新拉取。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime as _dt
from typing import Any

from src.intent import default_registry
from src import semantic as semantic_index

logger = logging.getLogger(__name__)


# 基础工具——无论意图命中与否都注入。
# （见 _build_user_message 中的 _retrieve_relevant_knowledge），避免双重检索浪费 token。
# kb_search 仅作为「显式要求搜索知识库」时的可选工具（见其 intent_keywords）。
BASE_TOOL_NAMES: set[str] = {"send_message", "save_memory", "recall_memory"}

# 意图过滤降级策略：当没有匹配到任何意图特定工具时，
# 发送的兜底工具列表（覆盖最常见的使用场景）
FALLBACK_TOOL_NAMES: set[str] = {
    "send_message", "save_memory", "recall_memory",
    "web_search", "get_weather",
}

# 工具结果 TTL（秒）：不同类型工具的结果有效期
TOOL_RESULT_TTL: dict[str, int] = {
    "get_weather": 1800,      # 30 分钟
    "web_search": 600,        # 10 分钟
    "kb_search": 3600,        # 1 小时
    "search_doc": 3600,       # 1 小时
    "recall_memory": 0,       # 不过期
    "save_memory": 0,         # 不过期
    "send_message": 0,        # 不过期
}
TOOL_RESULT_DEFAULT_TTL: int = 600  # 默认 10 分钟

# web 搜索类意图类别：RAG 已接地时应抑制，优先用知识库回答
WEB_SEARCH_INTENT_CATEGORIES = frozenset({"domain.web_search"})


def rag_grounded_confident(agent: Any) -> bool:
    """RAG 已 confidently 接地：知识库已有高置信答案，无需联网搜索。

    供「RAG 优先于 web 搜索」规则使用——RAG 高置信命中时抑制 domain.web_search
    类技能与工具，让 agent 直接基于注入的知识库内容回答，避免无谓联网（含 60s 超时）。

    判定以相似度分数为准（复用项目已调好的 _RAG_GROUND_SIMILARITY=0.65 接地阈值），
    而不用 intent_ok：rag_inject 中 intent_ok = (not rag_intent_only) or _is_document_query，
    rag_intent_only 默认 False 时 intent_ok 几乎恒为 True，若据此门控会把任何弱 KB 命中
    （best_score 低至 0.35）都判为接地，错误地剥夺 web_search 这一外部事实源兜底。
    """
    if not getattr(agent, "_last_kb_hit", False):
        return False
    best = getattr(agent, "_last_kb_best_score", None)
    if best is None:
        return False
    try:
        from src.llm.style import _RAG_GROUND_SIMILARITY
        return float(best) >= float(_RAG_GROUND_SIMILARITY)
    except (TypeError, ValueError, ImportError):
        return False


def check_stale_tool_results(agent: Any, messages: list[dict]) -> str | None:
    """检测对话历史中的工具结果是否过期。

    返回过期提醒消息（如有），None 表示无过期。
    """
    stale_tools: list[str] = []
    now = _dt.now()

    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        ts_str = parsed.get("_ts")
        tool_name = parsed.get("_tool", "")
        if not ts_str or not tool_name:
            continue
        try:
            ts = _dt.fromisoformat(ts_str)
        except (ValueError, TypeError) as _exc:
            logger.debug(f"check_stale_tool_results: swallowed exception: {_exc}")
            continue
        ttl = TOOL_RESULT_TTL.get(tool_name, TOOL_RESULT_DEFAULT_TTL)
        if ttl <= 0:
            continue
        age = (now - ts).total_seconds()
        if age > ttl:
            stale_tools.append(f"{tool_name}（已过期 {int(age - ttl)}s）")

    if stale_tools:
        return ("注意：以下工具结果已过期，可能不再准确，"
                "请考虑重新获取：\n" + "\n".join(stale_tools))
    return None


def is_tool_for_platform(agent: Any, tool_name: str) -> bool:
    """检查工具是否适用于当前平台。

    platforms 为空列表时表示通用工具（全平台可用）；
    否则只有当当前平台在 platforms 列表中时才可用。
    """
    if not agent.platform_id or not agent.tool_router:
        return True
    tool = agent.tool_router._tools.get(tool_name)
    if not tool:
        return False
    platforms = getattr(tool, "platforms", None) or []
    if not platforms:
        return True
    return agent.platform_id in [p.lower() for p in platforms]


def filter_schemas_by_platform(agent: Any, schemas: list[dict]) -> list[dict]:
    """按当前平台过滤工具 schema 列表。"""
    if not agent.platform_id or not agent.tool_router:
        return schemas
    result = []
    for schema in schemas:
        name = schema.get("function", {}).get("name")
        if name and is_tool_for_platform(agent, name):
            result.append(schema)
    return result


def keyword_match_tool_names(
    agent: Any,
    message_text: str,
    query_embedding: list[float] | None = None,
) -> set[str]:
    """返回 BASE 工具 + 被意图命中（具体场景词优先，抽象意图兜底）的工具名集合。

    纯规则、零 LLM 调用。作为『智能混合』路由的快速路径：
    工具命中条件：
      1) 工具自身 intent_keywords（具体场景词）命中 —— 主要精准信号，向后兼容；
      2) 否则，若工具在 TOOL_ACTION_MAP 声明了抽象行动意图且其证据词命中 —— 兜底补充；
      3) Phase 2 语义兜底：子串未命中时，消息向量与各工具语义向量余弦相似度 >=
         阈值即判定相关，覆盖同义/错别字/口语化表达。
    BASE 工具恒含；未命中（模糊/闲聊）交由上层决定（smart 回退全量 / keyword 回退 FALLBACK）。
    """
    if not agent.tool_router:
        return set()
    all_tool_names = agent.tool_router.get_available_tool_names()
    # 按平台过滤：只保留当前平台可用的工具
    all_tool_names = [n for n in all_tool_names if is_tool_for_platform(agent, n)]
    if not all_tool_names:
        return set()

    text = message_text.strip()
    text_lower = text.lower()
    matched: set[str] = set()

    # 始终包含基础工具
    for name in BASE_TOOL_NAMES:
        if name in all_tool_names:
            matched.add(name)

    registry = default_registry
    for name in all_tool_names:
        if name in BASE_TOOL_NAMES:
            continue  # 已包含
        tool = agent.tool_router._tools.get(name)
        if not tool:
            continue

        # 1) 具体场景词（精确，向后兼容）：工具经 intent_categories 解析出的有效意图关键词。
        keywords = getattr(tool, "effective_intent_keywords", None)
        if keywords:
            if any(kw.lower() in text_lower for kw in keywords):
                matched.add(name)
            continue

        # 2) 抽象行动意图（兜底补充）：仅当工具未声明任何具体场景词时，
        cat_ids = registry.tool_action_categories(name)
        if cat_ids and any(
            registry.category_matches(cid, text, text_lower) for cid in cat_ids
        ):
            matched.add(name)
            continue

    # 3) Phase 2 语义兜底：子串未命中具体工具时，用语义相似度补充相关工具。
    if query_embedding is not None:
        sem_hits = semantic_tool_matches(agent, query_embedding)
        for name in sem_hits:
            if name in all_tool_names:
                if name not in matched:
                    logger.debug("[语义路由] 工具 %s 语义命中 sim=%.3f", name, sem_hits[name])
                matched.add(name)

    return matched


def semantic_tool_matches(agent: Any, query_embedding: list[float]) -> dict[str, float]:
    """Phase 2：返回消息向量与各工具语义向量相似度 >= 阈值的工具名及相似度。"""
    tools_cfg = getattr(agent.tool_router, "config", None) if agent.tool_router else None
    if not getattr(tools_cfg, "semantic_routing", True):
        return {}
    threshold = getattr(tools_cfg, "semantic_tool_threshold", 0.42)
    tool_texts: list[tuple[str, str]] = []
    if not agent.tool_router:
        return {}
    for name in agent.tool_router.get_available_tool_names():
        if name in BASE_TOOL_NAMES:
            continue
        tool = agent.tool_router._tools.get(name)
        if not tool:
            continue
        sem_text = " ".join([
            getattr(tool, "name", name),
            getattr(tool, "description", ""),
            " ".join(getattr(tool, "effective_intent_keywords", []) or []),
        ]).strip()
        tool_texts.append((name, sem_text))
    return semantic_index.match_tools(query_embedding, tool_texts, threshold=threshold)


def filter_tools_by_intent(
    agent: Any,
    message_text: str,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    """纯关键词过滤（mode=keyword）：BASE + 命中意图工具 + 技能联动工具。

    对应旧 expose_all_tools=False 行为。语义盲区最大（关键词未覆盖的意图会漏），
    但 Phase 2 已用语义兜底补充相关工具（见 keyword_match_tool_names）。
    """
    if not agent.tool_router:
        return []
    all_tool_names = agent.tool_router.get_available_tool_names()
    if not all_tool_names:
        return []

    matched = keyword_match_tool_names(agent, message_text, query_embedding=query_embedding)
    router = agent.tool_router

    # 技能联动工具
    skill_activated = False
    if agent.skill_router and agent.skills_config.enabled:
        skill_name = agent.skill_router.get_activated_skill_name()
        if skill_name:
            skill_activated = True
        # 先加入声明的 allowed_tools
        for t in agent.skill_router.get_activated_tools():
            if t in all_tool_names:
                matched.add(t)
        # 补充：通过 router 来源标记找回技能自动包装工具（热加载后 allowed_tools 可能丢失）
        if skill_name and hasattr(router, 'get_skill_sourced_tools'):
            auto_tools = router.get_skill_sourced_tools()
            for sname in agent.skill_router.get_activated_skill_names():
                st_name = sname.replace("-", "_")
                if st_name in auto_tools and st_name not in matched:
                    matched.add(st_name)

    # 【技能工具隔离】技能未激活时，排除技能自动包装的工具，避免与内置工具重复
    if not skill_activated and hasattr(router, 'get_skill_sourced_tools'):
        skill_sourced = router.get_skill_sourced_tools()
        matched = matched - skill_sourced

    intent_matched = matched - BASE_TOOL_NAMES
    if not intent_matched:
        for name in FALLBACK_TOOL_NAMES:
            if name in all_tool_names:
                matched.add(name)
        # 兜底路径也排除技能工具
        if not skill_activated and hasattr(router, 'get_skill_sourced_tools'):
            matched = matched - router.get_skill_sourced_tools()

    logger.info("工具过滤: 已选择 %d/%d 个工具（匹配: %s）",
                len(matched), len(all_tool_names),
                sorted(matched - BASE_TOOL_NAMES))
    return agent.tool_router.filter_schemas_by_names(sorted(matched))


def resolve_routing_mode(agent: Any) -> str:
    """解析当前工具路由模式（兼容旧 expose_all_tools 二值开关）。

    返回 "smart" | "all" | "keyword"。
    """
    router = agent.tool_router
    if not router:
        return "smart"
    cfg = getattr(router, "config", None)
    if cfg is None:
        # 非标准 router（测试 mock 等）
        return "smart"
    try:
        fs = getattr(cfg, "model_fields_set", set()) or set()
    except Exception as _exc:
        logger.debug(f"resolve_routing_mode: swallowed exception: {_exc}")
        fs = set()
    if "tool_routing_mode" in fs:
        mode = cfg.tool_routing_mode
    elif "expose_all_tools" in fs:
        mode = "all" if cfg.expose_all_tools else "keyword"
    else:
        mode = "smart"
    if mode not in ("all", "keyword", "smart"):
        mode = "smart"
    return mode


def merge_proactive_action_tools(
    agent: Any,
    base_names: set[str],
    message_text: str,
    skill_activated: bool,
    skill_tools: set[str],
) -> set[str]:
    """把行动层【主动意图】（action.monitor / action.subscribe）映射工具并入暴露集。

    这是把行动层从「纯观测」接入真实路由的关键落点：命中主动意图时，
    其映射工具（monitor→create_todo+send_ding、subscribe→save_memory+create_todo+send_ding）
    被并入本轮暴露给 LLM 的工具集，使模型能直接采取主动动作。

    【只处理真正的主动意图】行动层其他类别（action.query/execute/analyze/...）是工具的
    通用动作描述，几乎每条业务消息都会命中（如"股票"→action.query），若一并强并会让 smart
    路由退化为"每轮全量"，违背精准暴露初衷。故仅 monitor/subscribe 计入。

    【技能契约隔离】仅当技能未接管（非 `skill_activated and skill_tools`）时并入；
    技能契约接管时工具暴露由技能 allowed_tools 独占，不并入主动意图工具，避免破坏技能隔离。
    """
    # 仅 monitor / subscribe 计入主动意图
    _PROACTIVE_ACTION_CATS = {"action.monitor", "action.subscribe"}
    matched_actions = default_registry.match_action_categories(message_text or "")
    proactive_actions = [a for a in matched_actions if a in _PROACTIVE_ACTION_CATS]
    agent._last_action_intents = proactive_actions
    if not proactive_actions:
        return base_names
    if skill_activated and skill_tools:
        # 技能契约独占工具暴露，主动意图并入会破坏技能隔离
        return base_names
    proactive: set[str] = set()
    for ac in proactive_actions:
        proactive |= set(default_registry.tools_for_action_category(ac))
    if not proactive:
        return base_names
    extra = proactive - base_names
    if extra:
        logger.info("[行动意图] 命中主动意图 %s，并入工具 %s",
                    proactive_actions, sorted(extra))
    return base_names | proactive


def select_tools(
    agent: Any,
    message_text: str,
    query_embedding: list[float] | None = None,
) -> list[dict]:
    """按配置的路由策略，选出本轮应暴露给主 LLM 的工具 schema 列表。

    策略（tool_routing_mode）：
    - "smart"（默认）：关键词命中意图工具 → 精准暴露相关工具；
      无明确意图关键词 → 回退全量（交给主模型自行判断是否调用，保证不漏）。
    - "all"：每轮全量暴露所有工具。
    - "keyword"：纯 intent_keywords 过滤（无命中回退 FALLBACK）。

    兼容旧 expose_all_tools 二值开关（见 ToolsConfig.tool_routing_mode 注释）。
    """
    router = agent.tool_router
    if not router:
        return []
    cfg = getattr(router, "config", None)
    if cfg is None:
        # 非标准 router（测试 mock 等），不暴露工具
        return []

    mode = resolve_routing_mode(agent)

    # 技能关联工具（智能引擎联动）
    skill_tools: set[str] = set()
    skill_activated = False
    skill_domain_cats: list[str] = []
    if agent.skill_router and agent.skills_config.enabled:
        skill_name = agent.skill_router.get_activated_skill_name()
        if skill_name:
            skill_activated = True
            skill_tools = set(agent.skill_router.get_activated_tools())
            # 补充：自动检测技能自动包装的工具
            if hasattr(router, 'get_skill_sourced_tools'):
                auto_tools = router.get_skill_sourced_tools()
                for sname in agent.skill_router.get_activated_skill_names():
                    st_name = sname.replace("-", "_")
                    if st_name in auto_tools and st_name not in skill_tools:
                        skill_tools.add(st_name)
                        logger.debug("[工具路由] 通过来源标记找回技能自动包装工具: %s", st_name)
            # 收集所有激活技能的意图类别
            all_cats: set[str] = set()
            for m in getattr(agent.skill_router, 'last_matches', []) or []:
                skill_obj = (
                    agent.skill_router._manager.get(m.name)
                    if hasattr(agent.skill_router, '_manager') else None
                )
                if skill_obj:
                    all_cats.update(getattr(skill_obj, 'intent_categories', []) or [])
                else:
                    all_cats.update(getattr(m, 'intent_categories', []) or [])
            skill_domain_cats = list(all_cats)

    # 【RAG 优先于 web 搜索】RAG 已接地时，抑制 web 搜索类工具，优先用知识库回答
    web_tools: set[str] = set()
    if rag_grounded_confident(agent):
        web_tools = {
            t.name for t in router._tools.values()
            if WEB_SEARCH_INTENT_CATEGORIES & set(getattr(t, "intent_categories", []) or [])
        }
        if web_tools:
            skill_tools = skill_tools - web_tools
            logger.info("[工具路由] RAG 已接地，抑制 web 搜索工具: %s", sorted(web_tools))

    def _finalize(schemas):
        """剥离 web 搜索类工具后做平台过滤（RAG 接地时生效）。"""
        if not web_tools:
            return filter_schemas_by_platform(agent, schemas)
        kept = [s for s in schemas
                if s.get("function", {}).get("name") not in web_tools]
        if len(kept) != len(schemas):
            logger.debug("[工具路由] _finalize 剔除 web 工具 %d 个",
                        len(schemas) - len(kept))
        return filter_schemas_by_platform(agent, kept)

    if mode == "all":
        return _finalize(router.get_schemas())

    if mode == "keyword":
        return _finalize(
            filter_tools_by_intent(agent, message_text, query_embedding=query_embedding),
        )

    # smart（默认）
    if skill_activated and not skill_tools:
        # 技能已激活但未声明 allowed_tools → 走 smart 关键词匹配 + 技能域工具补充
        skill_name = agent.skill_router.get_activated_skill_name()
        matched = keyword_match_tool_names(agent, message_text, query_embedding=query_embedding)
        if agent.skill_router:
            skill_cats = (
                getattr(agent.skill_router.last_match, 'intent_categories', []) or []
            )
            if skill_cats:
                available = set(router.get_available_tool_names())
                for t in router._tools.values():
                    t_cats = list(getattr(t, 'intent_categories', []))
                    if any(
                        c in skill_cats or any(sc in c for sc in skill_cats)
                        for c in t_cats
                    ):
                        if t.name in available:
                            matched.add(t.name)
        matched |= BASE_TOOL_NAMES
        logger.info("[工具路由] 技能 %s 无 allowed_tools，smart 匹配 %d 个工具（非全量）",
                    skill_name, len(matched))
        return _finalize(router.filter_schemas_by_names(sorted(matched)))

    # 技能已激活且声明了 allowed_tools → 技能契约接管
    if skill_activated and skill_tools:
        available = set(router.get_available_tool_names())
        contracted = (skill_tools & available) | BASE_TOOL_NAMES
        logger.info("[工具路由] 技能 %s 接管，约定 %d + 基础 %d，跳过关键词匹配",
                    agent.skill_router.get_activated_skill_name(),
                    len(skill_tools & available), len(BASE_TOOL_NAMES))
        return _finalize(router.filter_schemas_by_names(sorted(contracted)))

    matched = keyword_match_tool_names(agent, message_text, query_embedding=query_embedding)

    # 【技能工具隔离】技能未激活时，排除技能自动包装的工具
    if not skill_activated and hasattr(router, 'get_skill_sourced_tools'):
        skill_sourced = router.get_skill_sourced_tools()
        skill_tools_in_matched = matched & skill_sourced
        if skill_tools_in_matched:
            matched = matched - skill_tools_in_matched
            logger.debug("[工具路由] 技能未激活，排除技能工具 %s",
                         sorted(skill_tools_in_matched))

    # 【技能停用级联】disabled skill 声明的 intent_categories 对应的工具也从暴露集移除
    disabled_tools = set()
    if agent.skill_manager:
        _tool_domain_map = {
            t.name: list(getattr(t, "intent_categories", []))
            for t in router._tools.values()
        }
        disabled_tools = agent.skill_manager.get_disabled_skill_owned_tools(_tool_domain_map)
    blocked_here = (disabled_tools & matched) if disabled_tools else set()
    if blocked_here:
        matched = matched - blocked_here
        agent._last_blocked_by_disabled_skill = sorted(blocked_here)
        logger.info("[工具路由] 已停用技能级联屏蔽 %d 个工具: %s",
                    len(blocked_here), sorted(blocked_here))

    intent_matched = matched - BASE_TOOL_NAMES
    if intent_matched:
        logger.info("[工具路由] 模式=smart，关键词命中 %d 个意图工具，精准暴露（共 %d）: %s",
                    len(intent_matched), len(matched), sorted(intent_matched))
        exposed = merge_proactive_action_tools(
            agent, matched, message_text, skill_activated, skill_tools,
        )
        return _finalize(router.filter_schemas_by_names(sorted(exposed)))
    # smart 兜底：排除库内检索工具（kb_search/search_doc 已由 RAG 自动注入覆盖，
    # 暴露反而诱发弱模型"换关键词反复搜"循环）；web_search 保留——它是实时/事实类
    # 问题的唯一外部事实源，排除它会迫使模型凭训练记忆编造（限流 512/h 兜底成本）。
    #
    # 【修复 2026-07-31】当 RAG 自动注入未命中（_last_kb_hit=False）时，保留 kb_search
    # 作为兜底检索通道。否则系统提示词要求"无相关知识则调 kb_search"，但工具被排除，
    # 形成死锁 → LLM 退回到先前上下文（如钉盘讨论）给出问非所答的回复。
    retrieval_tools: set[str] = {"kb_search", "search_doc"}
    if not getattr(agent, "_last_kb_hit", True):
        retrieval_tools = {"search_doc"}  # 保留 kb_search 作为兜底
        logger.debug("[工具路由] RAG 未命中，保留 kb_search 作为兜底检索工具")
    available = set(router.get_available_tool_names())
    safe_tools = (BASE_TOOL_NAMES | available) - retrieval_tools
    if not skill_activated and hasattr(router, 'get_skill_sourced_tools'):
        safe_tools = safe_tools - router.get_skill_sourced_tools()
    blocked_fb = (safe_tools & disabled_tools) if disabled_tools else set()
    if blocked_fb:
        safe_tools = safe_tools - blocked_fb
        agent._last_blocked_by_disabled_skill = sorted(
            set(agent._last_blocked_by_disabled_skill) | blocked_fb,
        )
        logger.info("[工具路由] 已停用技能级联屏蔽(兜底) %d 个工具: %s",
                    len(blocked_fb), sorted(blocked_fb))
    logger.info("[工具路由] 模式=smart，无明确意图关键词，回退基础+意图工具 %d 个（已排除检索类）",
                len(safe_tools))
    exposed = merge_proactive_action_tools(
        agent, safe_tools, message_text, skill_activated, skill_tools,
    )
    return _finalize(router.filter_schemas_by_names(sorted(exposed)))