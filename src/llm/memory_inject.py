"""公共记忆自动注入层：每轮对话自动召回「团队公共记忆」并注入 system prompt。

背景（为什么需要）：
- 知识库(RAG)是自动注入的（src/llm/rag_inject.py）；但记忆召回完全依赖
  LLM 主动调用 ``recall_memory`` 工具——模型不调，记忆就永远不出现。
- 公共记忆是「团队/公司级共享知识，向所有对话人召回」，无隐私风险，
  却因依赖 LLM 主动调用而形同虚设（2026-08-20 用户反馈：公共记忆里的
  「软件资源站地址」没在对话中被使用）。
- 本模块让公共记忆像 RAG 一样每轮自动召回注入；个人记忆保持 LLM 主动
  调用（点对点隐私边界，绝不能自动注入他人个人记忆）。

设计：纯函数式 + 注入式接口（与 rag_inject 一致，便于单测）。
- 不读取 agent 的 _last_* 状态；结果通过返回值/调用方传入的容器透传。
- sender_id 固定传空串：memory_repo.recall_memory 在 sender_id 为空时
  走「仅 scope='public'」的安全分支，恰好符合公共记忆语义。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.llm.style import _RE_HAS_TEXT

logger = logging.getLogger(__name__)

# 注入参数（模块级常量，暂不做配置项——避免新增配置要同步 5 处）
PUBLIC_MEMORY_TOP_K: int = 3          # 每轮最多注入几条公共记忆
PUBLIC_MEMORY_MIN_SIMILARITY: float = 0.55  # 低于该相似度不注入（防噪音）

# 注入块的锚点标记（供 sanitize_reply / 测试定位）
PUBLIC_MEMORY_BLOCK_MARK = "【★公共记忆（团队/用户已录入的事实，最高优先级）★】"


@dataclass
class PublicMemoryInjectResult:
    """本次公共记忆注入的结果摘要。

    - injected: 是否真正把公共记忆追加到 system_content
    - memories: 注入的记忆条目列表（未注入为 []）
    - best_score: 命中最高相似度（未注入为 None）
    - skipped_reason: 仅 debug 用，便于日志/单测观察分支选择
    - block: 完整注入块文本（含高优先级指令前缀），供 prompt_builder 抽出为独立消息
    """

    injected: bool
    memories: list[dict] = field(default_factory=list)
    best_score: float | None = None
    skipped_reason: str = ""  # "disabled" | "short" | "no-embed" | "no-hit" | ""
    block: str = ""  # v6：完整块文本，供 prompt_builder 抽出为独立消息提升权重


def inject_public_memories(
    *,
    query: str,
    system_content: str,
    agent,  # LLMAgent；仅用于反向调用 _get_embedding_client / store._memory_repo
    query_embedding=None,
    top_k: int = PUBLIC_MEMORY_TOP_K,
    min_similarity: float = PUBLIC_MEMORY_MIN_SIMILARITY,
) -> tuple[str, PublicMemoryInjectResult]:
    """「自动注入公共记忆」主逻辑（对称 inject_rag_knowledge）。

    返回 (新的_system_content, PublicMemoryInjectResult)。
    1. 无 embedding client / store 不可用 → 跳过（"disabled"）
    2. query 无有效文本 → 跳过（"short"）
    3. 召回公共记忆（sender_id="" 仅 scope='public'），相似度 < min_similarity 不注入
    4. 命中则追加「【★公共记忆…】」块到 system_content 末尾
    """
    skipped_reason = ""

    # 1. 基础设施检查：embedding 与记忆仓库都不可用时直接跳过
    try:
        emb = agent._get_embedding_client()
    except Exception as e:  # 防御：embedding client 初始化失败不影响主回复
        logger.debug("[公共记忆] embedding client 不可用，跳过注入: %s", e)
        emb = None
    store = getattr(agent, "store", None)
    memory_repo = getattr(store, "_memory_repo", None) if store else None
    if not emb or not getattr(emb, "enabled", False) or memory_repo is None:
        skipped_reason = "disabled"
        return system_content, PublicMemoryInjectResult(
            injected=False, skipped_reason=skipped_reason,
        )

    # 2. 过滤：太短的消息、纯表情、纯标点不注入（与 RAG 同一口径）
    has_text = _RE_HAS_TEXT.search(query)
    has_meaningful_text = len(query) >= 5 and has_text is not None
    if not has_meaningful_text:
        skipped_reason = "short"
        return system_content, PublicMemoryInjectResult(
            injected=False, skipped_reason=skipped_reason,
        )

    # 3. 召回公共记忆：优先复用调用方已算好的向量（零额外 embedding）
    q_emb = query_embedding
    if q_emb is None:
        try:
            q_emb = emb.embed(query)
        except Exception as e:
            logger.warning("[公共记忆] query 向量化失败，跳过注入: %s", e)
            q_emb = None
    if not q_emb:
        skipped_reason = "no-embed"
        return system_content, PublicMemoryInjectResult(
            injected=False, skipped_reason=skipped_reason,
        )

    try:
        # sender_id="" → recall_memory 只查 scope='public'（安全分支，恰好是公共记忆）
        memories = memory_repo.recall_memory(
            q_emb, top_k, query_text=query, sender_id="",
            min_similarity=min_similarity,
        )
    except Exception as e:  # 防御：召回失败不影响主回复
        logger.warning("[公共记忆] 召回失败，跳过注入: %s", e)
        return system_content, PublicMemoryInjectResult(
            injected=False, skipped_reason="recall-error",
        )

    if not memories:
        skipped_reason = "no-hit"
        return system_content, PublicMemoryInjectResult(
            injected=False, skipped_reason=skipped_reason,
        )

    best_score = max(float(m.get("similarity", 0.0)) for m in memories)
    logger.info(
        "[公共记忆] 注入成功: best_score=%.3f top=%d query=%.60s",
        best_score, len(memories), query,
    )
    # v6：与 RAG 对齐——加高优先级指令前缀 + 降级兜底声明，提示 LLM 当
    # 公共记忆直接包含用户提到的地址/术语时优先采用，避免被 KB/网络信息
    # 覆盖（2026-08-20 真实事故：8800 资源站地址已在公共记忆，KB 文档却说
    # "不在内部系统列表"，LLM 据此回复忽略记忆里的事实）。
    _mem_block = (
        f"\n{PUBLIC_MEMORY_BLOCK_MARK}\n"
        "以下内容是系统已从团队公共记忆库检索到的事实（用户/团队明确录入）。\n"
        "**当下方内容直接包含用户提到的地址/术语/系统名/工号时，"
        "你必须直接基于本区块回答**，而不是引用其他来源（KB 文档/网络）。\n"
        "⚠️ 覆盖声明：若本区块与知识库(RAG)内容冲突，优先采用本区块——"
        "公共记忆是显式录入的事实，KB 可能过时。\n"
        "⚠️ 回答要求：直接给出具体地址/IP/配置/流程/工号等信息，"
        "不要说「不在系统列表」「需要查证」「信息不足」——"
        "若下方已明确写出该信息，视为已知事实直接回答。\n"
        "⚠️ 若用户问题与下方内容不相关（如问天气却注入了 VPN 文档），"
        "则忽略本区块正常回答。\n"
        "\n"
    )
    for m in memories:
        content = str(m.get("content", "")).strip()
        if content:
            _mem_block += f"- {content}\n"
    new_system_content = system_content + _mem_block

    return new_system_content, PublicMemoryInjectResult(
        injected=True,
        memories=memories,
        best_score=best_score,
        skipped_reason="",
        block=_mem_block,  # v6：完整块文本，供 prompt_builder 抽出为独立消息
    )
