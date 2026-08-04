"""路由质量追踪与工具收敛。

从 src.llm.agent._record_routing_trace_pre / _finalize_trace /
_maybe_converge_tools / _build_tool_call_assistant_message /
_handle_discarded_tool_calls 拆出。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from src.llm.client import LLMResponse

logger = logging.getLogger(__name__)

# 工具收敛护栏中应撤下的"继续检索"工具（保留 send_message/save_memory 等动作类工具）。
_RETRIEVAL_TOOLS = {"web_search", "kb_search", "search_doc"}


def record_routing_trace_pre(
    agent,
    message,
    disposition: str,
    intent_action: str,
    routing_mode: str,
    routed_tools: list,
    activated: list,
    t_start: float,
) -> list:
    """记录路由质量数据（Phase 4 收敛 + 组合 + 语义路由可观测 + 全链路瀑布）。"""
    agent._rq_id = None
    stages_pre: list = []
    if agent.store and hasattr(agent.store, "_routing_quality_repo"):
        try:
            rd = {}
            if agent.skill_router:
                rd = getattr(agent.skill_router, "_last_routing_detail", {})
            primary = activated[0] if activated else None
            sub_skills = [m.name for m in activated[1:]] if activated and len(activated) > 1 else []
            t_routing = time.perf_counter()
            stages_pre = [
                {"stage": "message_in", "ms": round((t_routing - t_start) * 1000, 1),
                 "status": "ok", "detail": {"type": getattr(message, "msg_type", "") or "text",
                                            "len": len(message.content or ""),
                                            "sender": message.sender_name or ""}},
                {"stage": "intent", "ms": 0.0, "status": "ok",
                 "detail": {"disposition": disposition or "unknown",
                            "action": intent_action,
                            "routing_mode": routing_mode or ""}},
                {"stage": "skill_routing", "ms": 0.0, "status": "ok" if primary else "skip",
                 "detail": {"primary": primary.name if primary else None,
                            "score": round(primary.score, 3) if primary else 0.0,
                            "source": primary.source if primary else "",
                            "combo_count": len(sub_skills),
                            "candidates": rd.get("candidates_count", 0),
                            "convergence_applied": rd.get("convergence_applied", 0)}},
                {"stage": "tool_exposure", "ms": 0.0, "status": "ok",
                 "detail": {"count": len(routed_tools), "tools": sorted(routed_tools)}},
            ]
            agent._rq_id = agent.store._routing_quality_repo.record_routing_quality(
                sender_id=message.sender_id or "",
                sender_name=message.sender_name or "",
                conversation_id=message.chat_id or "",
                content_preview=message.content[:200] if message.content else "",
                primary_skill=(primary.name if primary else ""),
                primary_score=(primary.score if primary else 0.0),
                primary_source=(primary.source if primary else ""),
                combo_count=len(sub_skills),
                combo_skills=sub_skills,
                convergence_zone_size=rd.get("convergence_zone_size", 0),
                convergence_applied=rd.get("convergence_applied", 0),
                goal_fit_details=rd.get("goal_fit_details", {}),
                tools_exposed=sorted(routed_tools),
                routing_mode=routing_mode or "",
                candidates_count=rd.get("candidates_count", 0),
                intent_disposition=disposition or "",
                intent_action=intent_action,
                intent_actions=",".join(agent._last_action_intents),
                blocked_by_disabled_skill=json.dumps(agent._last_blocked_by_disabled_skill),
                message_type=getattr(message, "msg_type", "") or "text",
                stages_json=stages_pre,
            )
        except Exception:
            logger.warning("路由质量记录失败", exc_info=True)
    return stages_pre


def finalize_trace(
    agent,
    reply,
    t_start: float,
    stages_pre: list,
    llm_latency_ms: float,
    llm_rounds: int,
    last_usage: Any,
):
    """LLM 推理结束后补齐路由质量记录的耗时 / 轮次 / 完整瀑布。"""
    if agent._rq_id is None or not agent.store:
        return reply
    try:
        t_end = time.perf_counter()
        total_ms = round((t_end - t_start) * 1000, 1)
        llm_ms = round(llm_latency_ms, 1)
        stages = list(stages_pre)
        stages.append({
            "stage": "llm_inference", "ms": llm_ms,
            "status": "ok" if llm_rounds > 0 else "fail",
            "detail": {
                "model": agent.config.model,
                "rounds": llm_rounds,
                "usage": last_usage or {},
            },
        })
        stages.append({
            "stage": "reply", "ms": 0.0,
            "status": "ok" if reply.text else "empty",
            "detail": {"len": len(reply.text or ""),
                       "already_sent": bool(reply.already_sent)},
        })
        input_t = last_usage.get("prompt_tokens", 0) if isinstance(last_usage, dict) else 0
        output_t = last_usage.get("completion_tokens", 0) if isinstance(last_usage, dict) else 0
        total_t = input_t + output_t
        cost_est = agent._estimate_cost(input_t, output_t, agent.config.model)
        agent.store.update_routing_quality_trace(
            agent._rq_id,
            llm_latency_ms=llm_ms,
            llm_rounds=llm_rounds,
            llm_model=agent.config.model,
            total_latency_ms=total_ms,
            reply_len=len(reply.text or ""),
            reply_text=reply.text or "",
            stages_json=stages,
            input_tokens=input_t,
            output_tokens=output_t,
            total_tokens=total_t,
            cost_usd=cost_est,
        )
    except Exception:
        logger.warning("路由质量补全失败", exc_info=True)
    # === 指标采样（仪表盘用） ===
    try:
        from src.utils.metrics import LLMSample, MetricsAggregator
        sample = LLMSample(
            ts=0,
            platform_id=agent.platform_id or "",
            llm_calls=llm_rounds or 0,
            fallback_used=1 if last_usage is None and llm_rounds else 0,
            tool_calls=len(reply.routed_tools or []) if hasattr(reply, "routed_tools") else 0,
            total_latency_ms=int(total_ms),
            rate_limited=0,
            errors=0,
            input_tokens_est=last_usage.get("prompt_tokens", 0) if isinstance(last_usage, dict) else 0,
            output_tokens_est=last_usage.get("completion_tokens", 0) if isinstance(last_usage, dict) else 0,
            request_id=agent._tl.rq_id if hasattr(agent._tl, "rq_id") else "",
        )
        MetricsAggregator.instance().record(sample)
    except Exception:
        logger.debug("metrics 采样失败", exc_info=True)
    return reply


def maybe_converge_tools(
    response: LLMResponse,
    tools: list,
    messages: list[dict],
    round_num: int,
    converge_threshold: int,
    converged: bool,
) -> tuple[list, bool]:
    """收敛护栏：若已连续多轮都在调工具且尚未综合作答，则在本轮执行后强制收敛。"""
    if (not converged and converge_threshold > 0
            and round_num >= converge_threshold
            and any(tc["name"] in _RETRIEVAL_TOOLS for tc in response.tool_calls)):
        converged = True
        retained = [t for t in tools
                    if t.get("function", {}).get("name") not in _RETRIEVAL_TOOLS]
        if retained != tools:
            tools = retained
            logger.warning(
                "[工具收敛] 连续 %d 轮调用检索工具仍未作答，移除检索类工具(%s)并强制综合",
                round_num, sorted(_RETRIEVAL_TOOLS),
            )
            messages.append({
                "role": "system",
                "content": "你已进行多轮检索。现在停止继续调用搜索/检索类工具，"
                           "基于对话上下文中已有的工具结果，直接综合给出最准确、"
                           "最简明的回答。若已有结果不足以确定结论，明确说明还缺什么、"
                           "不要再次发起检索。",
            })
    return tools, converged


def build_tool_call_assistant_message(response: LLMResponse) -> dict:
    """把本轮 LLM 的 tool_calls 组装成合法的 assistant 历史消息。"""
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["args"], ensure_ascii=False),
                }
            }
            for tc in response.tool_calls
        ],
    }


def handle_discarded_tool_calls(
    agent,
    response: LLMResponse,
    messages: list[dict],
    round_num: int,
) -> bool:
    """工具收敛纠正：本轮 LLM 试图调用已被移除的工具且未给出有效内容。"""
    if not response.discarded_tool_names:
        return False
    if response.discarded_tool_results:
        assistant_msg = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": tr["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": tr["name"],
                        "arguments": "",
                    }
                }
                for tr in response.discarded_tool_results
            ],
        }
        messages.append(assistant_msg)
        messages.extend(response.discarded_tool_results)
    discarded_sorted = sorted(set(response.discarded_tool_names))
    logger.warning(
        "[工具收敛] 轮次 %d LLM 仍尝试已移除工具 %s 且无有效内容，注入纠正提示强制综合",
        round_num, discarded_sorted,
    )
    messages.append({
        "role": "system",
        "content": (
            f"你尝试调用的工具 {discarded_sorted} 当前不可用或已被移除。"
            "请停止尝试调用这些工具，基于对话上下文中已有的信息直接综合给出准确、"
            "简明的回答；若上下文仍不足以确定结论，明确说明还缺什么，不要再发起任何工具调用。"
        ),
    })
    return True
