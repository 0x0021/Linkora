"""RAG 注入层：从 _build_user_message 抽出的「检索→门控→注入」独立模块。

设计：纯函数式 + 注入式接口。
- 不读取 self._last_kb_*；调用方传入 mutable 容器接收结果。
- 不直接修改 agent 状态，保证零副作用只发生在调用方传入的容器。

为什么抽出来：
- 原 47 行混合在 _build_user_message 里，单测只能 monkey-patch 全 agent。
- 抽离后可用纯单测验证「无 RAG / 关闭 RAG / 短消息跳过 / 意图未命 / 高置信短路」
  五个分支，无需启动 main 全套设施。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.llm.style import _RAG_GROUND_SIMILARITY, _RE_HAS_TEXT, Citation

logger = logging.getLogger(__name__)


@dataclass
class RagInjectResult:
    """本次 RAG 注入的结果摘要。

    - injected: 是否真正把检索结果追加到 system_content
    - relevant_knowledge: 已拼好的「【相关知识】...」文本，未注入则为 ""
    - best_score: KB 命中最高相似度；kb_grounded=False 时为 None
    - intent_ok: 意图分类是否命中（用于 Feature A 透传）
    - skipped_reason: 仅 debug 用，便于日志/单测观察分支选择
    - citations: 结构化引文列表（仅注入时非空，供回复溯源/置信度呈现）
    - rag_block: v5 完整 RAG 注入块（前置指令+知识内容+降级兜底），供 prompt_builder 提取为独立消息
    """

    injected: bool
    relevant_knowledge: str
    best_score: float | None
    intent_ok: bool
    skipped_reason: str  # "disabled" | "short" | "intent-miss" | "no-embed" | ""
    citations: list[Citation] = field(default_factory=list)
    rag_block: str = ""  # v5：完整 RAG 块，供 prompt_builder 提取为独立消息定位


def inject_rag_knowledge(
    *,
    query: str,
    system_content: str,
    agent,  # LLMAgent；只用于反向调用 _get_embedding_client / _is_document_query / _retrieve_relevant_knowledge
    rag_auto_inject: bool,
    rag_intent_only: bool,
    query_embedding=None,
    override_min_similarity: float | None = None,
    override_max_results: int | None = None,
) -> tuple[str, RagInjectResult]:
    """「自动注入 RAG 知识」主逻辑（从 _build_user_message 抽离）。

    返回 (新的_system_content, RagInjectResult)。
    行为与拆分前保持一致：
    1. rag_auto_inject=False → 直接返回，不动 system_content，result.skipped_reason="disabled"
    2. query 短于 5 字符或无中文/英文 → result.skipped_reason="short"
    3. 三层门控（意图 + 召回相似度 + 高置信短路）决策是否注入
    4. 透传 _last_kb_best_score / _last_kb_hit / _last_kb_query_intent 已在调用方处理

    三级递进支持：
    - override_min_similarity: 临时覆盖 agent._rag_min_similarity（降级重搜用）
    - override_max_results: 临时覆盖 agent._rag_max_results（降级重搜用）
    """
    skipped_reason = ""
    if not rag_auto_inject:
        logger.debug("[RAG] 自动注入已关闭（rag_auto_inject=false），由 LLM 主动调 kb_search")
        skipped_reason = "disabled"
        return system_content, RagInjectResult(
            injected=False,
            relevant_knowledge="",
            best_score=None,
            intent_ok=False,
            skipped_reason=skipped_reason,
        )

    # 过滤：太短的消息、纯表情、纯标点不走 RAG
    has_text = _RE_HAS_TEXT.search(query)
    has_meaningful_text = (
        len(query) >= 5  # 至少5个字符
        and has_text is not None  # 包含至少3个连续中文字母
    )

    if not has_meaningful_text:
        logger.debug("[RAG] 跳过注入（无意义消息）: %s", query[:20])
        skipped_reason = "short"
        return system_content, RagInjectResult(
            injected=False,
            relevant_knowledge="",
            best_score=None,
            intent_ok=False,
            skipped_reason=skipped_reason,
        )

    # 一次性向量化 query：优先复用 process_message 已算好的向量（避免重复 embed），
    # 仅当调用方未传入（如语义路由关闭导致 _embed_message 返回 None）时本地补算。
    emb = agent._get_embedding_client()
    if query_embedding is not None:
        q_emb = query_embedding
    elif emb:
        try:
            q_emb = emb.embed(query)
        except Exception as e:
            logger.warning("[RAG] query 向量化失败，跳过语义意图: %s", e)
            q_emb = None
    else:
        q_emb = None

    # 门控2：语义判定是否需查知识库（闲聊/天气/问候等不注入；业务操作自动命中）
    intent_ok = (not rag_intent_only) or agent._is_document_query(query, q_emb)

    # 三级递进支持：降级重搜时临时覆盖相似度阈值和结果上限
    _saved_min_sim = None
    _saved_max_res = None
    if override_min_similarity is not None:
        _saved_min_sim = getattr(agent, "_rag_min_similarity", None)
        agent._rag_min_similarity = override_min_similarity
        logger.debug("[RAG] 降级重搜：临时覆盖 min_similarity %.3f → %.3f",
                     _saved_min_sim or 0, override_min_similarity)
    if override_max_results is not None:
        _saved_max_res = getattr(agent, "_rag_max_results", None)
        agent._rag_max_results = override_max_results
        logger.debug("[RAG] 降级重搜：临时覆盖 max_results %s → %s",
                     _saved_max_res, override_max_results)
    try:
        relevant_knowledge, best_score = agent._retrieve_relevant_knowledge(query, q_emb)
    finally:
        if _saved_min_sim is not None:
            agent._rag_min_similarity = _saved_min_sim
        if _saved_max_res is not None:
            agent._rag_max_results = _saved_max_res
    # 侧信道读取结构化引文（由 style.retrieve_relevant_knowledge 暂存；transient，
    # 与持久的 _last_kb_* 状态无关，仅本次注入判定通过后才对外呈现）。
    citations_raw = list(getattr(agent, "_last_kb_citations_raw", None) or [])
    # 双保险：KB 命中极高（≥接地强制阈值 0.78）时，即便意图分类未命中也注入。
    # 注意与展示阈值(0.50)解耦：bge 相似度整体偏高（闲聊对无关文档也常得 0.5+），
    # 0.50 旁路会让闲聊轮被误接地、污染 _last_kb_hit 状态。
    kb_grounded = bool(relevant_knowledge) and (
        intent_ok or (best_score is not None and best_score >= _RAG_GROUND_SIMILARITY)
    )

    new_system_content = system_content
    _rag_block = ""  # v5：捕获完整 RAG 块，供 prompt_builder 提取为独立消息
    if kb_grounded:
        logger.info(
            "[RAG] 注入成功: best_score=%.3f intent_ok=%s query=%.60s",
            best_score or 0, intent_ok, query,
        )
        # v5：RAG 前置指令强化——更高权重措辞，明确覆盖所有「先调 kb_search」冲突指令
        _rag_preamble = (
            "\n【★RAG 知识库答案（最高优先级，直接使用）★】\n"
            "以下内容是系统已为你检索完毕的知识库结果。"
            "**你必须直接基于以下内容回答用户问题**，这是你回答的唯一事实来源。\n"
            "⚠️ 覆盖声明：本区块出现时，system prompt 中所有「必须先调用 kb_search」"
            "「需要查询」「让我搜一下」等指令全部失效——检索已完成，禁止再调任何搜索工具。\n"
            "⚠️ 回答要求：直接给出具体步骤/地址/IP/配置/流程等详细信息，"
            "不要说「只有标题」「没有具体步骤」「需要申请后才能告诉你」——"
            "相关信息已在下方，请完整引用并组织回答。\n"
            "⚠️ 若用户问题与下方文档主题不相关（如问天气却注入了 VPN 文档），"
            "则忽略本区块正常回答；否则必须严格基于下方内容作答。"
        )
        _rag_block = _rag_preamble + f"\n\n{relevant_knowledge}"
        _rag_block += (
            "\n\n【降级兜底】若本轮【相关知识】区块为空或未注入——"
            "表示知识库中未检索到相关内容，此时你才可调用 kb_search 工具自行检索。"
            "检索无结果时，如实回答「知识库中未找到相关信息」，严禁编造。"
        )
        new_system_content += _rag_block
    elif relevant_knowledge:
        logger.debug(
            "[RAG] 检索到相关内容但意图未命中且分数未达置信阈值，跳过注入: %s", query[:20]
        )
        skipped_reason = "intent-miss"
    elif q_emb is None:
        # 向量化失败且未检索到任何结果
        skipped_reason = "no-embed"

    return new_system_content, RagInjectResult(
        injected=kb_grounded,
        relevant_knowledge=relevant_knowledge if kb_grounded else "",
        best_score=best_score if kb_grounded else None,
        intent_ok=intent_ok,
        skipped_reason=skipped_reason,
        citations=citations_raw if kb_grounded else [],
        rag_block=_rag_block,  # v5：完整 RAG 块
    )
