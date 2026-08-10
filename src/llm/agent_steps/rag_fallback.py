"""三级递进 RAG 空结果处理。

从 src.llm.agent._apply_rag_empty_fallback 拆出。
"""
from __future__ import annotations

import logging

from src.models import Message

logger = logging.getLogger(__name__)


def apply_rag_empty_fallback(
    agent,
    message: Message,
    messages: list[dict],
    query_vec: list[float] | None,
) -> None:
    """三级递进 RAG 空结果处理（就地修改 messages 与 thread-local 递进状态）。"""
    # 三级递进 RAG 空结果处理：
    # 第1级：降阈值重搜 → 第2级：引导追问 → 第3级：强制兜底
    # 非知识类查询时，重置状态
    if not getattr(agent, "_last_kb_query_intent", False):
        agent._tl.rag_empty_fallback_level = 0

    agent._last_rag_empty = (
        getattr(agent, "_last_kb_query_intent", False)
        and not getattr(agent, "_last_kb_hit", False)
    )

    # RAG 命中（正常或降级重搜）→ 重置递进状态；否则进入递进处理
    if not agent._last_rag_empty:
        agent._tl.rag_empty_fallback_level = 0
    elif agent._rag_empty_fallback_enabled:
        fallback_level = getattr(agent._tl, "rag_empty_fallback_level", 0)
        if fallback_level > agent._rag_max_retry_rounds:
            # 第 3 级：最终兜底（_done 中强制替换 LLM 回复）
            logger.info("[RAG三级递进] 已达最大引导追问次数(%d)，第3级强制兜底",
                        agent._rag_max_retry_rounds)
        elif fallback_level == agent._rag_max_retry_rounds:
            # 上轮已引导追问，本轮仍未命中 → 升级到第 3 级强制兜底
            agent._tl.rag_empty_fallback_level = agent._rag_max_retry_rounds + 1
            logger.info("[RAG三级递进] 用户补充信息后仍未命中，进入第3级强制兜底")
        elif fallback_level == 0:
            # 第 1 级：降阈值重搜
            fallback_sim = max(agent._rag_fallback_min_similarity,
                                agent._rag_min_similarity * 0.6)
            logger.info("[RAG三级递进] 第1级降阈值重搜，阈值 %.3f → %.3f，上限 %d → %d",
                        agent._rag_min_similarity, fallback_sim,
                        agent._rag_max_results, agent._rag_fallback_max_results)
            from src.llm.rag_inject import inject_rag_knowledge
            _new_sys, _result = inject_rag_knowledge(
                query=message.content,
                system_content="",
                agent=agent,
                rag_auto_inject=True,
                rag_intent_only=agent._rag_intent_only,
                query_embedding=query_vec,
                override_min_similarity=fallback_sim,
                override_max_results=agent._rag_fallback_max_results,
            )
            if _result.relevant_knowledge:
                # 降级找到结果：以独立 system 消息注入
                fallback_block = (
                    "【降级重搜结果】以下知识通过降低相似度二次检索获得，请优先参考：\n\n"
                    + _result.relevant_knowledge
                )
                messages.insert(1, {"role": "system", "content": fallback_block})
                agent._last_rag_empty = False
                agent._tl.rag_empty_fallback_level = 0
                logger.info("[RAG三级递进] 第1级降阈值重搜命中，已注入 %d 字符",
                            len(_result.relevant_knowledge))
            else:
                # 降级无结果 -> 进入第 2 级引导追问。
                # 不写硬性正则拦截：对话是否完结交给 LLM 结合上下文自行判断
                # （上下文已暴露发言人，system_prompt 也有收尾指令）。
                agent._tl.rag_empty_fallback_level = 1
                guidance_block = (
                    "【知识库引导追问指令】知识库中未直接匹配到相关信息。"
                    "请根据以下规则引导用户补充细节：\n"
                    "0. 【风格保持】即使知识库没有答案，也要以主人惯用的口吻和节奏回复——"
                    "像真人那样自然地回应（简短、口语化、不拘形式），不要变成机械客服。"
                    "如果不知道具体答案，可以用真人方式表达「我不太清楚」「这个得确认一下」等，"
                    "而不是输出模板句。\n"
                    "1. 合理追问，如「请问您能提供更多细节吗？"
                    "比如具体是哪个系统的U9、用途是什么？」\n"
                    "2. 绝对禁止：编造URL/域名/IP/内网地址；编造文档名称或《》形式引用；"
                    "编造审批流程/操作路径/申请入口；编造「相关度XX%」等置信度数字；"
                    "暗示用户去某个具体系统/页面操作；提供「参考/供参考」形式的虚假来源。\n"
                    "3. 语气专业友好，一次追问一个问题即可。"
                    "4. 若对话者已明确表示任务完成或不再需要"
                    "（如「改完了」「不用了」「先不用」「用不上了」），"
                    "该话题视为闭环：不要追问、不要索要信息（工号/手机号/账号等），"
                    "只做一句简短确认或直接结束。"
                )
                messages.insert(1, {"role": "system", "content": guidance_block})
                logger.info(
                    "[RAG三级递进] 第1级降阈值无结果，进入第2级引导追问"
                )
        # else: fallback_level == 1（引导追问模式），本轮已是引导追问后的新一轮，
        # 由上面的 fallback_level == max_retry_rounds 分支处理升级
