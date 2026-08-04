"""主人风格画像 + RAG 检索模块。

从 ``src.llm.agent`` 拆出——纯函数 + 锚点常量。原 ``LLMAgent`` 上的方法
（``_is_document_query``、``_retrieve_relevant_knowledge``、``_extract_relevant_snippets``、
``_get_style_prompt``、``_load_style_profile``、``_enforce_brevity``、
``_get_embedding_client``、``_embed_message``、``_cosine``）作为 1 行委托保留。

设计要点：
- **三段降级**（意图识别 → 召回阈值 → 生成约束）：RAG 防幻觉三层防护的工程
  实现，详见 ``docs/system_design.md``；
- **锚点 embedding 缓存**：``agent._anchor_cache`` 字段保留在 LLMAgent 上，
  本模块只读写不改所有权——多 agent 实例并存时仍能各自缓存。
- **低置信度画像回退**（``_LOW_CONF_NEUTRAL_STYLE``）：样本 < 阈值时回退中性
  文本 + 护栏，避免数字分身「假装」出不存在的个人风格。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

# 局部导入重排模块（reorder-only，默认关闭，不影响未开启时的任何行为）。
try:
    from src.llm.rerank import rerank as _rerank
except Exception:  # pragma: no cover - 极端 import 失败兜底
    _rerank = None
    logger.debug("[rerank] 模块导入失败，重排功能不可用（降级为原始顺序）", exc_info=True)


@dataclass
class Citation:
    """RAG 命中的一条结构化引文（用于回复溯源与置信度呈现）。

    - source: 文档标题（kb_search 结果的 source 字段）
    - score: 相似度 0..1
    - snippet: 命中片段（首条 extract_relevant_snippets，已按配置截断）
    - doc_id: 若检索结果含 id 则带上，便于前端跳转，否则 None
    """

    source: str
    score: float
    snippet: str
    doc_id: str | None = None


# 低置信度（样本过少）时退回的保守中性风格基线，以及注入 system prompt 的护栏提示。
# 当自动画像 confidence=low 时，画像代表性弱、不可靠，不应生硬套用，
# 改为中性稳妥口吻 + 明确护栏，避免数字分身「假装」出不存在的个人风格。
_LOW_CONF_NEUTRAL_STYLE = (
    "以得体、简洁、专业的方式回复，保持中立友好；"
    "不要刻意模仿任何不确定的口头禅或个人风格。"
)
_LOW_CONF_GUARDRAIL = (
    "（风格护栏：当前账号历史样本过少，自动风格画像置信度低、仅供参考，"
    "请勿生硬套用不确定的口吻，保持中性稳妥即可。）"
)


# 行动层中属于"用户主动意图、需在路由阶段强制并入工具"的类别。
# 其余 action.*（query/execute/analyze/...）是工具的通用动作描述，命中面过宽，不强制暴露。
_PROACTIVE_ACTION_CATS = {"action.monitor", "action.subscribe"}


# ============================================================================
# 语义意图分类（替代人工维护的短语表）
# ----------------------------------------------------------------------------
# 旧实现用一份写死的 intent_phrases 元组判定「是否需查知识库」，每来一种新业务
# 域（SRM/VPN/OA…）就得手动加词，脆弱且易漏（如「帮我注册SRM账号」曾漏判）。
#
# 新方案：用本地 embedding 把 query 与两组【通用】锚点句做余弦相似度，
# 自动判定意图——无需为任何具体业务手动加词，新增知识域会被通用 how-to 锚点
# 自动覆盖。锚点 embedding 首次使用时惰性生成并缓存，之后零成本。
# ============================================================================
_KNOWLEDGE_QUERY_ANCHORS = [
    # 显式问句：how-to / 操作 / 流程
    "这个系统怎么配置和使用",
    "某个业务流程的操作步骤是什么",
    "如何申请账号或权限",
    "在哪里可以查看相关文档说明",
    "这个功能怎么操作",
    "某个规范或制度的内容是什么",
    "怎么重置密码或修改设置",
    "某个平台账号怎么开通",
    # 对话陈述 / 纠偏 / 指代（多轮对话高频模式，防意图漏判）
    "说的是某个事情或系统的相关信息",
    "指的是某个具体的平台或服务",
    "刚才提到的那件事需要查一下",
    "这个问题和某个系统或文档有关",
    # v2 新增：纠偏确认场景 — "你上次说X，确认下"式攻击
    "之前提到过某个地址或配置，帮我确认一下现在是否还正确",
    "我记得某个系统地址是XXX，你帮我查一下知识库对不对",
    # v2 新增：简短命令场景 — "给我X地址""Y链接发我"
    "把某个系统的地址或网址发给我",
    "某个平台的登录入口或链接在哪里",
    "快速查询某个服务器或服务的地址",
    # v2 新增：闲聊伪装场景 — 天气/问候后跟业务查询
    "问候之后顺便问一下某个系统的信息或配置",
    "闲聊几句后问一下某个内部平台怎么访问",
]
_CASUAL_ANCHORS = [
    "你好在吗",
    "今天天气怎么样",
    "中午吃什么",
    "谢谢祝你愉快",
    "最近忙不忙",
    "早上好晚上好",
]
# embedding 不可用时的兜底：仅抑制极少数明显闲聊（稳定、非业务词，不随 KB 增长维护）
_CASUAL_FALLBACK = ("你好", "在吗", "谢谢", "天气", "吃什么", "早", "晚安", "哈哈", "收到")
_INTENT_MARGIN = 0.06          # 知识意图相似度需高于闲聊多少才判定为知识查询（v2: 0.04→0.06，提高抗闲聊伪装）
_INTENT_FLOOR = 0.12           # 知识意图相似度下限（v3: 0.22→0.12，大幅放宽以提升纯RAG问答覆盖）
_RAG_DISPLAY_SIMILARITY = 0.35  # KB 片段展示阈值（v4: 0.50→0.35；原值过高导致 67% 命中文档被丢弃，
                                # 与 config rag_min_similarity(0.4) 解耦——展示门槛应≤检索门槛，
                                # 否则检索回来却因硬编码更高阈值被静默丢弃）
_RAG_GROUND_SIMILARITY = 0.65   # KB 接地强制阈值（v4: 0.78→0.65；原值 0.78 过严导致大量命中文档
                                # 在意图未判对时被拒。现取 BGE 实际分布的甜区：
                                # 67% 级业务文档能过（安全网生效），62% 级误匹配（如天气查到IP表）仍被拦）

_anchor_lock = threading.Lock()


def get_embedding_client(agent: Any):
    """复用 kb_search 工具已加载的本地 EmbeddingClient（避免重复实例化）。

    缓存策略：成功取到客户端则永久缓存；缓存为 None 时允许重试
    （kb_search 可能在后台异步注册，首次查询时尚未就绪）。
    """
    if not hasattr(agent, "_emb_client"):
        agent._emb_client = None  # 哨兵：标记已尝试过
    if agent._emb_client is not None:
        return agent._emb_client  # 已有真实客户端，直接返回
    # 缓存为 None → 重试一次（kb_search 可能已注册）
    kb_tool = agent.tool_router._tools.get("kb_search") if agent.tool_router else None
    if kb_tool and getattr(kb_tool, "embedding_client", None):
        agent._emb_client = kb_tool.embedding_client
    return agent._emb_client  # 可能仍为 None（工具未注册/无 embedding）


def embed_message(agent: Any, text: str) -> Optional[list[float]]:
    """Phase 2 语义路由：对消息向量化一次（语义路由禁用或 embedding 不可用时返回 None）。"""
    tools_cfg = getattr(agent.tool_router, "config", None) if agent.tool_router else None
    skills_on = getattr(agent.skills_config, "semantic_routing", True)
    tools_on = getattr(tools_cfg, "semantic_routing", True) if tools_cfg else True
    if not (skills_on or tools_on):
        return None
    emb = get_embedding_client(agent)
    if emb is None or not getattr(emb, "enabled", False):
        return None
    try:
        return emb.embed(text)
    except Exception as e:
        logger.warning("[语义路由] 消息向量化失败，降级为纯子串匹配: %s", e)
        return None


def cosine(a, b) -> float:
    """两个等长向量的余弦相似度；任一为零向量时返回 0.0。"""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def is_document_query(agent: Any, query: str, query_embedding=None) -> bool:
    """语义判定 query 是否需查知识库（替代人工维护的短语表）。

    用本地 embedding 将 query 与两组【通用】锚点句做余弦相似度：
    - 知识查询锚点（how-to/流程/配置/申请…）
    - 闲聊锚点（问候/天气/吃饭…）
    若 query 更接近知识锚点（且超过下限），判定为需检索。

    新增业务域（SRM/VPN/OA…）会被通用 how-to 锚点自动覆盖，无需手动加词。
    若 embedding 不可用，降级为仅抑制极少数明显闲聊（稳定词表，不随 KB 增长）。
    """
    q = (query or "").strip()
    if not q:
        return False
    emb = get_embedding_client(agent)
    if emb and query_embedding is not None:
        # 惰性生成并缓存锚点向量（首次使用时算一次）
        if agent._anchor_cache["knowledge"] is None:
            with _anchor_lock:
                if agent._anchor_cache["knowledge"] is None:
                    try:
                        agent._anchor_cache["knowledge"] = [
                            e for e in (emb.embed(a) for a in _KNOWLEDGE_QUERY_ANCHORS) if e
                        ]
                        agent._anchor_cache["casual"] = [
                            e for e in (emb.embed(a) for a in _CASUAL_ANCHORS) if e
                        ]
                    except Exception as e:
                        logger.warning("[RAG] 锚点向量化失败，降级为闲聊抑制: %s", e)
                        return not any(w in q for w in _CASUAL_FALLBACK)
        sim_k = max(
            (cosine(query_embedding, a) for a in agent._anchor_cache["knowledge"]),
            default=0.0,
        )
        sim_c = max(
            (cosine(query_embedding, a) for a in agent._anchor_cache["casual"]),
            default=0.0,
        )
        logger.debug("[RAG] 语义意图: sim_k=%.3f sim_c=%.3f", sim_k, sim_c)
        return sim_k > sim_c + _INTENT_MARGIN and sim_k > _INTENT_FLOOR
    # 兜底（无 embedding）：仅抑制明显闲聊，其余放行由检索分数把关
    return not any(w in q for w in _CASUAL_FALLBACK)


def extract_relevant_snippets(
    content: str,
    query: str,
    max_snippets: int = 2,
    max_chars_per_snippet: int = 2000,
) -> list[str]:
    """从大文档中提取与 query 最相关的段落，用于 RAG 注入。

    注意：不要对段落做硬性短截断（旧实现 content[:200] 会把分块后段的关键
    信息如 IP 地址丢弃，导致 LLM 看不到答案而回复「未找到」）。仅当段落
    异常庞大（超过 max_chars_per_snippet）时才做保护性截断，并以完整分块
    作为兜底，确保关键信息不丢失。
    """
    query_tokens = set(t.strip() for t in query.split() if len(t.strip()) >= 2)
    if not query_tokens:
        return [content]

    paragraphs = [p.strip() for p in content.split('\n') if p.strip()]
    if not paragraphs:
        return [content]

    scored = []
    for para in paragraphs:
        score = sum(1 for token in query_tokens if token in para)
        if score > 0:
            scored.append((-score, len(scored), para))

    scored.sort()
    snippets = []
    seen = set()
    for _, idx, para in scored[:max_snippets]:
        if idx in seen:
            continue
        seen.add(idx)
        # 不再硬性截断到 200 字，避免关键信息（如分块后段的 IP）被丢弃；
        # 仅在内容异常庞大时做保护性截断，并保留完整段落上下文。
        if len(para) > max_chars_per_snippet:
            snippets.append(para[:max_chars_per_snippet] + "…")
        else:
            snippets.append(para)

    if not snippets:
        # 未匹配到关键词时返回完整分块，保证关键信息不丢失
        snippets = [content]

    # v6 覆盖率兜底：若提取内容不足原始内容的 20%，说明关键词匹配几乎全部丢失了
    # 有用信息（如配置步骤、IP 地址）不含查询词 → 退回使用完整内容。
    # 典型触发场景：查询「VPN 怎么配置」命中 782 字文档但只提取 28 字标题（3.6%）。
    _extracted_total = sum(len(s) for s in snippets)
    if _extracted_total > 0 and _extracted_total < len(content) * 0.2:
        snippets = [content]

    return snippets


def retrieve_relevant_knowledge(
    agent: Any,
    query: str,
    query_embedding=None,
) -> tuple[str, float | None]:
    """自动从知识库检索相关知识，注入到 system prompt。

    三层防护（防 AI 把弱相关文档当事实复述）：
    1. 门槛由 config 控制（rag_min_similarity / rag_max_results）
    2. 仅返回最相关的一条，避免列表轰炸
    3. 调用方（_build_user_message）按意图门控决定是否注入
    """
    min_sim = agent._rag_min_similarity
    max_res = agent._rag_max_results

    try:
        # 找到 kb_search 工具
        kb_tool = agent.tool_router._tools.get("kb_search")
        if not kb_tool:
            return "", None

        # 调用知识库搜索（收紧门槛）；复用调用方已算好的 query 向量，避免重复 embed
        result = kb_tool.search(
            query=query, query_embedding=query_embedding,
            top_k=max_res, min_similarity=min_sim,
        )
        if not result.get("success") or not result.get("results"):
            return "", None

        # P2-6：BGE 本地离线重排（默认关；开启且模型可用时仅调整候选顺序，
        # 不改原相似度 score，best_score 阈值与引文页脚语义保持不变）。
        # 任意失败/未开启 → 原始顺序（rerank 内部已兜底降级）。
        if getattr(agent, "_rerank_enabled", False) and _rerank is not None:
            try:
                result["results"] = _rerank(
                    query,
                    result["results"],
                    model=getattr(agent, "_rerank_model", "BAAI/bge-reranker-base"),
                    offline=getattr(agent, "_rerank_offline", False),
                    top_k=getattr(agent, "_rerank_top_k", None) or None,
                    timeout=getattr(agent, "_rerank_timeout", 2.0),
                )
            except Exception as e:  # noqa: BLE001 - 重排异常绝不阻断主链路
                logger.debug("[rerank] 重排调用异常，沿用原始顺序: %s", e)

        # 取最高相似度（用于置信阈值双保险）
        best_score = max((r.get("score", 0) for r in result["results"]), default=None)

        # 格式化相关知识：按 agent._rag_max_results 控制展示条数（原硬编码 1 条，
        # 导致多段相关文档只注入最一条，信息量严重不足）；需超过展示阈值；同源去重。
        knowledge_parts = ["【相关知识】"]
        citations: list[Citation] = []
        seen_sources: set[str] = set()
        displayed = 0
        _MAX_DISPLAY = min(getattr(agent, "_rag_max_results", 4) or 4, 4)  # 上限 4 防轰炸
        for r in result["results"]:
            if displayed >= _MAX_DISPLAY:
                break
            source = r.get("source", "未知文档")
            score = r.get("score", 0)

            # 跳过低相关度（低于展示阈值）
            if score < _RAG_DISPLAY_SIMILARITY:
                continue
            # 同源去重
            if source in seen_sources:
                continue
            seen_sources.add(source)
            displayed += 1

            content = r.get("content", "")
            # v6：高置信 RAG 命中时跳过关键词 snippet 提取，直接使用原始 chunk 内容。
            # 原因：extract_relevant_snippets 按查询词做段落级关键词匹配——
            #   「服务器地址、客户端安装、连接步骤」等真正有用的内容不含查询词
            #   （如「公司/VPN/怎么/配置」），全部得 0 分被丢弃。
            #   从 782 字完整文档只提取 28 字（3.6%），导致模型说「只有标题没有步骤」。
            # 向量检索已确认 best_score >= 接地阈值时，整块 chunk 都是相关的，
            # 无需再做关键词过滤（那是给低置信召回用的降噪手段）。
            _use_raw = (best_score is not None
                        and float(best_score) >= float(_RAG_GROUND_SIMILARITY))
            if _use_raw:
                _max_c = agent._rag_max_content_chars or 800
                raw = content[:_max_c] + ("…" if len(content) > _max_c else "")
                snippet_text = f"  - {raw}"
                snippets_for_cite = [raw]
            else:
                snippets = extract_relevant_snippets(
                    content, query,
                    max_chars_per_snippet=agent._rag_max_content_chars,
                )
                snippet_text = "\n".join(f"  - {s}" for s in snippets)
                snippets_for_cite = snippets

            knowledge_parts.append(f"1. {source}（{score:.0%}）\n{snippet_text}")
            citations.append(Citation(
                source=source,
                score=float(score or 0),
                snippet=(snippets_for_cite[0] if snippets_for_cite else "")[:120],
                doc_id=(str(r.get("id")) if r.get("id") is not None else None),
            ))

        # 侧信道透传结构化引文：保持 2 元组返回契约不变（避免破坏既有调用/测试），
        # 由 prompt_builder 在注入判定通过后读取并写入 agent._last_kb_citations。
        try:
            agent._last_kb_citations_raw = citations
        except Exception:
            logger.debug("[resilience] 无法在 agent 上暂存引文（best-effort，忽略）", exc_info=True)

        # 空块防编造：全部结果低于展示阈值时 knowledge_parts 仅剩「【相关知识】」头，
        # 若照旧返回非空串会被 rag_inject 误判为已接地，强令模型"直接基于它回答"——
        # 面对空知识块只能靠参数记忆编造。此处返回空串，让链路走空-RAG 三级兜底（诚实追问）。
        if displayed == 0:
            logger.debug("[RAG] 检索结果均低于展示阈值(%.2f)，按未命中处理（防空块诱导编造）",
                         _RAG_DISPLAY_SIMILARITY)
            return "", best_score

        return "\n".join(knowledge_parts), best_score
    except Exception as e:
        logger.warning("[RAG] 自动注入失败: %s", e)
        return "", None


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

