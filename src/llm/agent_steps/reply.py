"""回复构建与主流程编排。

从 src.llm.agent._make_reply / _finish_reply / process_message /
extract_memories_from_conversation / summarize_conversation 拆出。
"""
from __future__ import annotations

import logging
import time

from src.llm.agent_reply import AgentReply
from src.llm.client import LLMResponse
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError
from src.llm.reply import enforce_brevity, gate_reply, strip_internal_artifacts
from src.llm.reply_helper import ensure_complete_reply
from src.models import Message
from src.utils.llm_json import extract_json
from src.utils.request_id import request_id_scope

logger = logging.getLogger(__name__)


def make_reply(
    agent,
    text: str,
    already_sent: bool,
    routing_mode: str,
    routed_tools: list,
) -> AgentReply:
    """产出回复并附带本轮工具路由信息（供决策追踪）。"""
    reply = AgentReply(text=text, already_sent=already_sent)
    reply.routing_mode = routing_mode
    reply.routed_tools = sorted(routed_tools)
    if agent.skill_router and agent.skill_router.last_match:
        reply.skill_name = agent.skill_router.last_match.name
        reply.skill_source = agent.skill_router.last_match.source
    reply.confidence = getattr(agent, "_last_kb_best_score", None)
    reply.evidence_source = "kb" if getattr(agent, "_last_kb_hit", False) else None
    reply.citations = list(getattr(agent, "_last_kb_citations", []) or [])
    reply.best_chunk = reply.citations[0].snippet if reply.citations else None
    return reply


def finish_reply(
    agent,
    text: str,
    self_sent_to_current_chat: bool,
    routing_mode: str,
    routed_tools: list,
) -> AgentReply:
    """产出最终回复：若已通过工具自回复当前会话，则标记 already_sent 跳过发送。
    三级递进兜底：第3级时忽略 LLM 生成内容，用配置的硬编码兜底回复替换。"""
    if self_sent_to_current_chat:
        logger.info("[防双重回复] 已通过 send_message 回复当前会话，跳过文本二次发送")
        return make_reply(agent, "", True, routing_mode, routed_tools)
    fallback_level = getattr(agent._tl, "rag_empty_fallback_level", 0)
    if (agent._last_rag_empty and agent._rag_empty_fallback_enabled
            and fallback_level > agent._rag_max_retry_rounds):
        logger.warning("[RAG三级递进] 第3级强制兜底，替换 LLM 回复")
        fallback_reply = getattr(agent.config, "llm", None)
        if fallback_reply:
            fallback_text = getattr(fallback_reply, "rag_empty_fallback_reply",
                                    "知识库中暂未收录相关信息。")
        else:
            fallback_text = "知识库中暂未收录相关信息。"
        return make_reply(agent, fallback_text, False, routing_mode, routed_tools)
    clean = enforce_brevity(agent, text.strip())
    clean = ensure_complete_reply(clean, agent.client,
                                  getattr(agent, "_auto_complete_enabled", True))
    clean, _gated = gate_reply(clean, agent.user_name, agent.user_title)
    if _gated:
        logger.info("[B闸门] 非流式回复命中末端闸门，已整句替换为安全模板")
    return make_reply(agent, clean, False, routing_mode, routed_tools)


@request_id_scope(prefix="llm")
def process_message(
    agent,
    message: Message,
    history: list[Message] = None,
    disposition: str = "",
    intent_action: str = "llm",
    enable_stream: bool = False,
) -> AgentReply:
    """Agent 主链路编排器。"""
    from src.llm.agent_steps.rag_fallback import apply_rag_empty_fallback
    from src.llm.agent_steps.skill import activate_skills, apply_skill_round_limit
    from src.llm.agent_steps.routing_trace import (
        build_tool_call_assistant_message,
        finalize_trace,
        handle_discarded_tool_calls,
        maybe_converge_tools,
        record_routing_trace_pre,
    )
    from src.llm.agent_steps.stream import detect_stream_support, handle_stream_response

    history = history or []
    t_start = time.perf_counter()
    agent._last_action_intents = []
    agent._last_blocked_by_disabled_skill = []
    # 每轮新消息重置 RAG 状态，避免上一轮空 RAG 的激进清洗污染后续非 RAG 回复
    #（如天气工具中的「降水概率 60%」被误当成相关度分数删除）。
    agent._last_kb_query_intent = False
    agent._last_kb_hit = False
    agent._last_rag_empty = False
    agent._last_kb_relevant_knowledge = ""

    stream_supported = detect_stream_support(agent, enable_stream)

    query_vec = agent._embed_message(message.content)
    messages = agent._build_user_message(message, history, query_embedding=query_vec)

    apply_rag_empty_fallback(agent, message, messages, query_vec)
    activated = activate_skills(agent, message, messages, query_vec)

    tools = agent._select_tools(message.content, query_embedding=query_vec)
    routing_mode = agent._resolve_routing_mode()
    routed_tools = [t.get("function", {}).get("name") for t in tools]

    stages_pre = record_routing_trace_pre(
        agent, message, disposition, intent_action, routing_mode, routed_tools,
        activated, t_start,
    )
    max_rounds = apply_skill_round_limit(agent, activated, agent.config.max_tool_rounds, messages)

    self_sent_to_current_chat = False
    llm_latency_ms = 0.0
    llm_rounds = 0
    last_usage = None
    t_llm_start = None

    def _mk_reply(text: str = "", already_sent: bool = False) -> AgentReply:
        return make_reply(agent, text, already_sent, routing_mode, routed_tools)

    def _done(text: str) -> AgentReply:
        return finish_reply(agent, text, self_sent_to_current_chat,
                            routing_mode, routed_tools)

    def _finalize(reply: AgentReply) -> AgentReply:
        return finalize_trace(agent, reply, t_start, stages_pre,
                              llm_latency_ms, llm_rounds, last_usage)

    logger.info("LLM 代理正在处理来自 %s 的消息（轮次限制: %d，工具数: %d）",
                message.sender_name, max_rounds, len(tools))

    converge_threshold = agent.config.converge_after_tool_rounds
    converged = False

    for round_num in range(1, max_rounds + 1):
        try:
            if round_num > 1:
                stale_msg = agent._check_stale_tool_results(messages)
                if stale_msg:
                    messages.append({"role": "system", "content": stale_msg})
                    logger.info("[工具过期] 检测到过期工具结果，已注入提醒")

            if t_llm_start is None:
                t_llm_start = time.perf_counter()
            _t0 = time.perf_counter()

            use_stream = stream_supported and round_num == 1 and not tools
            response = agent.client.chat(messages, tools=tools, stream=use_stream)
            llm_latency_ms += (time.perf_counter() - _t0) * 1000
            llm_rounds = round_num

            if use_stream and hasattr(response, "__iter__"):
                for _ in handle_stream_response(response, message, agent):
                    pass
                return _finalize(_mk_reply(already_sent=True))

            if isinstance(response, LLMResponse) and response.usage:
                last_usage = response.usage
        except Exception as e:
            logger.error("LLM 调用失败: %s", e, exc_info=True)
            if self_sent_to_current_chat:
                return _finalize(_mk_reply(already_sent=True))
            if isinstance(e, LLMRateLimitExhaustedError):
                raise
            raise LLMProcessingError(
                f"LLM 推理彻底失败: {e}", original=e, stage="llm_inference"
            ) from e

        if not isinstance(response, LLMResponse):
            logger.warning("LLM 返回非标准响应，跳过本轮")
            continue

        logger.info("LLM 轮次 %d: 结束原因=%s，内容=%s，工具调用数=%d，用量=%s",
                    round_num, response.finish_reason,
                    (response.content or "")[:100] if response.content else "无",
                    len(response.tool_calls), response.usage)

        if response.content and not response.tool_calls:
            reply = response.content.strip()
            return _finalize(_done(reply))

        if response.tool_calls:
            tools, converged = maybe_converge_tools(
                response, tools, messages, round_num, converge_threshold, converged)
            messages.append(build_tool_call_assistant_message(response))
            if response.discarded_tool_results:
                messages.extend(response.discarded_tool_results)
            tool_results, round_self_sent = agent._execute_tool_calls(response.tool_calls, message)
            self_sent_to_current_chat = self_sent_to_current_chat or round_self_sent
            messages.extend(tool_results)
            continue

        if response.content:
            return _finalize(_done(response.content))

        if handle_discarded_tool_calls(agent, response, messages, round_num):
            continue

        logger.warning("LLM 轮次 %d 返回空响应，注入纠正提示", round_num)
        messages.append({
            "role": "system",
            "content": "请基于对话上下文直接给出回复，不要再等待或发起工具调用。如果已有信息不足以回答，请说明情况。",
        })

    logger.warning("已达到最大工具调用轮次 (%d)，返回降级回复", max_rounds)
    if self_sent_to_current_chat:
        return _finalize(_mk_reply(already_sent=True))
    return _finalize(_mk_reply(text="我查询了一下但暂时没有找到确切答案，稍后可以再问我。"))


def extract_memories_from_conversation(
    agent,
    messages: list[Message],
) -> list[str]:
    """用 LLM 从对话中提取值得记住的信息。"""
    if not messages:
        return []

    conversation_text = []
    for msg in messages[-8:]:
        role = "对方" if msg.role != "assistant" else "我"
        conversation_text.append(f"{role}: {msg.content[:300]}")
    conversation_str = "\n".join(conversation_text)

    extraction_prompt = [
        {"role": "system", "content": """你是一个信息提取助手。分析以下对话，提取值得长期记住的信息。

重要原则：
- 只提取对方(user)说的事实，不要提取AI(我)回复中的内容
- 不要提取临时性问题解答（如"怎么重启服务"这类一次性问答）
- 每条记忆必须包含明确的主语和事实，能脱离对话独立理解
- 忽略日常寒暄、情绪表达、闲聊

值得记住的信息类型：
- 个人信息：姓名、职位、部门、联系方式、邮箱
- 偏好习惯：喜欢的工具、编程语言、工作方式
- 重要承诺：约定、截止日期、待办事项
- 项目信息：正在进行的项目、技术栈、团队组成
- 关系信息：同事关系、上下级、负责领域
- 明确的事实性陈述："我是XX"、"我负责XX"、"XX由我做"

不值得记住的：
- 日常寒暄、打招呼
- 临时性问题解答（如"怎么重启服务"）
- 情绪表达、闲聊
- 已经在系统提示词中的信息

输出格式：JSON 数组，每个元素是 {"text": "记忆内容", "confidence": "high/medium/low"}。
只保留 confidence 为 high 或 medium 的记忆。
如果没有值得记住的信息，返回空数组 []。
只输出 JSON，不要其他内容。"""},
        {"role": "user", "content": f"请从以下对话中提取值得记住的信息：\n\n{conversation_str}"},
    ]

    try:
        response = agent.client.chat(extraction_prompt, temperature=0.1)
        if not response.content:
            return []

        memories_raw = extract_json(response.content)
        if not isinstance(memories_raw, list):
            return []

        memories: list[str] = []
        for item in memories_raw:
            if isinstance(item, str):
                memories.append(item)
            elif isinstance(item, dict):
                conf = (item.get("confidence") or "").lower()
                if conf in ("high", "medium", ""):
                    text = item.get("text") or item.get("content") or ""
                    if isinstance(text, str) and text.strip():
                        memories.append(text)

        def _is_meaningful(s: str) -> bool:
            stripped = s.strip()
            if len(stripped) < 5:
                return False
            if len(stripped) > 200:
                return False
            if not any(ch.isalnum() for ch in stripped):
                return False
            greetings = ("你好", "您好", "谢谢", "感谢", "好的", "再见", "拜拜", "晚安", "嗯", "哦", "哈", "嘿")
            if stripped in greetings:
                return False
            compact = stripped.replace(" ", "").replace("！", "").replace("。", "").replace("？", "").replace("?", "")
            if len(set(compact)) <= 2 and len(compact) >= 4:
                return False
            return True

        return [m.strip() for m in memories if isinstance(m, str) and _is_meaningful(m)]
    except Exception as e:
        logger.debug("记忆提取失败: %s", e)
        return []


def summarize_conversation(
    agent,
    messages: list[Message],
    max_messages: int = 0,
) -> str:
    """用 LLM 生成对话摘要。"""
    if not messages:
        return ""

    if max_messages and max_messages > 0 and len(messages) > max_messages:
        messages = messages[-max_messages:]
    conversation_text = []
    for msg in messages:
        role = "对方" if msg.role != "assistant" else "我"
        conversation_text.append(f"{role}: {msg.content[:300]}")
    conversation_str = "\n".join(conversation_text)

    summary_prompt = [
        {"role": "system", "content": """你是一个对话摘要助手。请对以下对话进行简洁的摘要。

要求：
1. 用中文输出，不超过 200 字
2. 包含对话的核心主题、关键信息和结论
3. 使用自然语言描述，不要使用列表或编号
4. 格式：以「【对话摘要】」开头

只输出摘要内容，不要其他内容。"""},
        {"role": "user", "content": f"请对以下对话进行摘要：\n\n{conversation_str}"},
    ]

    try:
        response = agent.client.chat(summary_prompt, temperature=0.1)
        if not response.content:
            return ""
        summarized = response.content.strip()
        try:
            summarized = strip_internal_artifacts(summarized)
        except Exception:
            logger.debug("[resilience] 摘要落库前清洗失败，保留原文", exc_info=True)
        return summarized
    except Exception as e:
        logger.debug("[摘要] 生成失败: %s", e)
        return ""
