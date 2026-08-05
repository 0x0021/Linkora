from __future__ import annotations

import json
import logging
import threading
import time
import functools
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Iterator

from src.config import LlmConfig, SkillsConfig
from src.intent import default_registry
from src.llm import history as _history
from src.llm import router as _router
from src.llm import style as _style
from src.llm import system_prompt as _system_prompt
from src.llm.style import Citation
from src.llm.rag_inject import inject_rag_knowledge  # noqa: F401  # 保留以兼容测试 monkey-patch
from src.llm.message_wrap import wrap_incoming_message  # noqa: F401  # 保留以兼容测试 monkey-patch
from src.llm.client import LLMClient, LLMResponse
from src.llm.exceptions import LLMProcessingError, LLMRateLimitExhaustedError
from src.llm.prompt_builder import PromptBuilder
from src.llm.tool_orchestrator import ToolOrchestrator
from src.models import Message
from src.skills.manager import SkillManager
from src.skills.router import SkillRouter
from src.tools.base import ToolRouter
from src.utils.llm_json import extract_json
from src.utils.request_id import request_id_scope

# 子步骤已拆到 src/llm/agent_steps/；此处保留薄包装以兼容测试 monkey-patch
from src.llm.agent_steps import (
    apply_rag_empty_fallback as _apply_rag_empty_fallback,
    activate_skills as _activate_skills,
    apply_skill_round_limit as _apply_skill_round_limit,
    record_routing_trace_pre as _record_routing_trace_pre,
    finalize_trace as _finalize_trace,
    maybe_converge_tools as _maybe_converge_tools,
    build_tool_call_assistant_message as _build_tool_call_assistant_message,
    handle_discarded_tool_calls as _handle_discarded_tool_calls,
    detect_stream_support as _detect_stream_support,
    handle_stream_response as _handle_stream_response,
    make_reply as _make_reply_fn,
    finish_reply as _finish_reply_fn,
    process_message as _process_message_fn,
    extract_memories_from_conversation as _extract_memories_fn,
    summarize_conversation as _summarize_conversation_fn,
)

if TYPE_CHECKING:
    from src.llm.summary_scheduler import SummaryScheduler

logger = logging.getLogger(__name__)

# 工具收敛护栏中应撤下的“继续检索”工具（保留 send_message/save_memory 等动作类工具）。
_RETRIEVAL_TOOLS = {"web_search", "kb_search", "search_doc"}


# AgentReply 已移到 src.llm.agent_reply 以避免循环依赖，此处重导出保持向后兼容
from src.llm.agent_reply import AgentReply  # noqa: F401


class _AgentThreadState(threading.local):
    """LLMAgent 的每线程隔离状态容器（一次会话内的"最近一次"标记位）。

    为什么需要这个类：原写法是 ``self._tl.last_kb_hit: bool = False``——
    对成员表达式做带注解赋值虽然语法合法，但注解在运行时会被直接丢弃，
    静态检查器同样不接受（reportInvalidTypeForm），等于白写。
    这里把字段类型上移到 threading.local 子类做类级声明，类型才真正可见。

    刻意**不实现 __init__**：保持既有语义——只有构造 Agent 的那个线程会被
    赋初值，其余线程一律由读取侧的 ``getattr(self._tl, name, default)``
    兜底。若在此预置默认值，会改变 ``hasattr(agent._tl, "rq_id")``
    这类判定的结果（见 agent_steps/routing_trace.py）。
    """

    last_action_intents: list[str]
    last_blocked_by_disabled_skill: list[str]
    last_kb_best_score: float | None
    last_kb_hit: bool
    last_kb_query_intent: bool
    last_kb_citations: list[Citation]
    last_kb_relevant_knowledge: str
    last_rag_empty: bool
    rag_empty_fallback_level: int
    rq_id: int | None


class LLMAgent:
    def __init__(self, config: LlmConfig, client: LLMClient,
                 tool_router: ToolRouter, user_name: str = "",
                 user_dept: str = "", org_name: str = "",
                 user_title: str = "",
                 store=None,
                 skill_manager: SkillManager | None = None,
                 skills_config: SkillsConfig | None = None,
                 platform_id: str = "dingtalk",
                 fallback_store=None,
                 few_shot_examples: list[dict] | None = None,
                 im_adapter=None,
                 summary_scheduler: "SummaryScheduler | None" = None):
        self.config = config
        self.client = client
        self.tool_router = tool_router
        self.user_name = user_name
        self.user_dept = user_dept
        self.org_name = org_name
        self.user_title = user_title
        # 末尾自动续写补全开关（截断确定性修复）：弱模型早停导致回复以连接词/
        # 逗号/无结尾标点收尾时，自动用一次低成本 LLM 续写到正常句号。默认开。
        self._auto_complete_enabled = True
        self.store = store
        self.platform_id = platform_id
        self.few_shot_examples = few_shot_examples
        self.fallback_store = fallback_store
        self.skill_manager = skill_manager
        self.skills_config = skills_config or SkillsConfig()
        self.skill_router = SkillRouter(skill_manager, skills_config=self.skills_config, platform_id=platform_id) if skill_manager else None
        self.im_adapter = im_adapter
        # H2-A 后台异步摘要调度器（可为 None：不接线时降级为同步摘要旧行为）。
        # 注意：SummaryScheduler 与 LLMAgent 互相引用，存在循环依赖，
        # 故采用「先建 agent(传 None) → 再建 scheduler(agent) → 最后赋值」的两步接线，
        # main.py 中完成。此处仅持有引用，不创建。
        self._summary_scheduler = summary_scheduler
        # Prompt 构建已拆到 src/llm/prompt_builder.py
        self._prompt_builder = PromptBuilder(self)
        # 工具编排已拆到 src/llm/tool_orchestrator.py
        self._tool_orchestrator = ToolOrchestrator(self)
        self._tl = _AgentThreadState()
        self._tl.last_action_intents = []
        self._tl.last_blocked_by_disabled_skill = []
        self._tl.last_kb_best_score = None
        self._tl.last_kb_hit = False
        self._tl.last_kb_query_intent = False
        self._tl.last_kb_citations = []
        self._tl.last_kb_relevant_knowledge = ""
        self._tl.last_rag_empty = False
        self._tl.rag_empty_fallback_level = 0  # 三级递进：0=初始, 1=等待引导追问, 2=强制兜底
        self._tl.rq_id = None
        self._style_prompt_cache: str | None = None
        self._anchor_cache: dict = {"knowledge": None, "casual": None}
        self._cache_advanced_config()

    def _cache_advanced_config(self) -> None:
        adv = self.config.advanced if hasattr(self.config, "advanced") else None

        def _as_int(v, default: int) -> int:
            # 防御：单测可能用 MagicMock 整体替换 config，getattr 返回 Mock 而非数字；
            # 此时回退默认值，保证历史分级/摘要判定在异常配置下不崩溃（与旧硬编码 6 一致）。
            try:
                return int(v)
            except (TypeError, ValueError):
                return default

        def _as_float(v, default: float) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        self._rag_auto_inject = getattr(adv, "rag_auto_inject", True) if adv else True
        self._rag_intent_only = getattr(adv, "rag_intent_only", True) if adv else True
        self._rag_min_similarity = getattr(adv, "rag_min_similarity", 0.6) if adv else 0.6
        self._rag_max_results = getattr(adv, "rag_max_results", 1) if adv else 1
        # 三级递进 RAG 空结果处理配置
        self._rag_empty_fallback_enabled = bool(getattr(adv, "rag_empty_fallback_enabled", True) if adv else True)
        self._rag_fallback_min_similarity = _as_float(
            getattr(adv, "rag_fallback_min_similarity", 0.20) if adv else 0.20, 0.20,
        )
        self._rag_fallback_max_results = _as_int(
            getattr(adv, "rag_fallback_max_results", 2) if adv else 2, 2,
        )
        self._rag_max_retry_rounds = _as_int(
            getattr(adv, "rag_max_retry_rounds", 1) if adv else 1, 1,
        )
        # H8：打通此前"死配置" rag_max_content_chars，作为 RAG 片段逐段截断上限。
        # 此前该值从未被读取（_extract_relevant_snippets 一直用默认 2000）；prod 的
        # chunk_size=800 < 上限(1200)，截断分支永不触发 → 行为不变，仅把开关接活。
        self._rag_max_content_chars = getattr(adv, "rag_max_content_chars", 800) if adv else 800
        # P2-6：BGE 本地离线重排总开关（默认关，开启需本地模型权重）。
        self._rerank_enabled = bool(getattr(adv, "rerank_enabled", False) if adv else False)
        self._rerank_model = getattr(adv, "rerank_model", "BAAI/bge-reranker-base") if adv else "BAAI/bge-reranker-base"
        self._rerank_offline = bool(getattr(adv, "rerank_offline", False) if adv else False)
        self._rerank_top_k = _as_int(getattr(adv, "rerank_top_k", 10) if adv else 10, 10)
        self._rerank_timeout = _as_float(getattr(adv, "rerank_timeout", 2.0) if adv else 2.0, 2.0)
        self._max_input_tokens = getattr(adv, "max_input_tokens", 12000) if adv else 12000
        # H5/H6：注入 LLM 的近期完整条数（原硬编码 6；未配置时默认 6 = 向后兼容原行为）。
        self._history_tiering_recent = _as_int(
            getattr(adv, "history_tiering_recent", 6) if adv else 6, 6,
        )
        # H2-A：后台异步摘要阈值（详见 docs/system_design.md §3.1）。
        self._summary_async_enabled = bool(getattr(adv, "summary_async_enabled", True) if adv else True)
        self._summary_max_age_seconds = _as_int(
            getattr(adv, "summary_max_age_seconds", 600) if adv else 600, 600,
        )
        self._summary_min_coverage_ratio = _as_float(
            getattr(adv, "summary_min_coverage_ratio", 0.6) if adv else 0.6, 0.6,
        )
        self._summary_max_messages = _as_int(
            getattr(adv, "summary_max_messages", 0) if adv else 0, 0,
        )
        self._summary_min_older = _as_int(
            getattr(adv, "summary_min_older", 2) if adv else 2, 2,
        )

    @property
    def _last_action_intents(self) -> list[str]:
        return getattr(self._tl, "last_action_intents", [])

    @_last_action_intents.setter
    def _last_action_intents(self, val: list[str]) -> None:
        self._tl.last_action_intents = val

    @property
    def _last_blocked_by_disabled_skill(self) -> list[str]:
        return getattr(self._tl, "last_blocked_by_disabled_skill", [])

    @_last_blocked_by_disabled_skill.setter
    def _last_blocked_by_disabled_skill(self, val: list[str]) -> None:
        self._tl.last_blocked_by_disabled_skill = val

    @property
    def _last_kb_best_score(self) -> float | None:
        return getattr(self._tl, "last_kb_best_score", None)

    @_last_kb_best_score.setter
    def _last_kb_best_score(self, val: float | None) -> None:
        self._tl.last_kb_best_score = val

    @property
    def _last_kb_hit(self) -> bool:
        return getattr(self._tl, "last_kb_hit", False)

    @_last_kb_hit.setter
    def _last_kb_hit(self, val: bool) -> None:
        self._tl.last_kb_hit = val

    @property
    def _last_kb_query_intent(self) -> bool:
        return getattr(self._tl, "last_kb_query_intent", False)

    @_last_kb_query_intent.setter
    def _last_kb_query_intent(self, val: bool) -> None:
        self._tl.last_kb_query_intent = val

    @property
    def _last_kb_citations(self) -> list[Citation]:
        return getattr(self._tl, "last_kb_citations", [])

    @_last_kb_citations.setter
    def _last_kb_citations(self, val: list[Citation]) -> None:
        self._tl.last_kb_citations = val

    @property
    def _last_kb_relevant_knowledge(self) -> str:
        return getattr(self._tl, "last_kb_relevant_knowledge", "")

    @_last_kb_relevant_knowledge.setter
    def _last_kb_relevant_knowledge(self, val: str) -> None:
        self._tl.last_kb_relevant_knowledge = val

    @property
    def _last_rag_empty(self) -> bool:
        return getattr(self._tl, "last_rag_empty", False)

    @_last_rag_empty.setter
    def _last_rag_empty(self, val: bool) -> None:
        self._tl.last_rag_empty = val

    @property
    def _rq_id(self) -> int | None:
        return getattr(self._tl, "rq_id", None)

    @_rq_id.setter
    def _rq_id(self, val: int | None) -> None:
        self._tl.rq_id = val

    def _build_system_prompt_core(self, sender_name: str = None) -> str:
        # 实际逻辑已拆到 src/llm/system_prompt.py；此处保留 thin wrapper 以兼容测试
        # monkey-patch（如 ``agent._build_system_prompt = lambda **kw: ""``）。
        return _system_prompt.build_system_prompt_core(self, sender_name)

    def _build_system_prompt(self, sender_name: str = None,
                             include_tools: bool = False,
                             include_skills: bool = False,
                             include_few_shot: bool = True,
                             include_style: bool = True,
                             user_query: str | None = None,
                             query_embedding: list[float] | None = None,
                             exclude: list[dict] | None = None) -> str:
        # 实际逻辑已拆到 src/llm/system_prompt.py；此处保留 thin wrapper 以兼容测试 monkey-patch。
        return _system_prompt.build_system_prompt(
            self, sender_name, include_tools, include_skills,
            include_few_shot, include_style, user_query, query_embedding, exclude,
        )

    def _get_style_prompt(self) -> str:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper 以兼容测试 monkey-patch。
        return _style.get_style_prompt(self)

    def _load_style_profile(self) -> dict | None:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper 以兼容测试 monkey-patch。
        return _style.load_style_profile(self)

    def _enforce_brevity(self, reply: str) -> str:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper。
        return _style.enforce_brevity(self, reply)

    def _ensure_complete_reply(self, text: str) -> str:
        """末尾自动续写补全（截断确定性修复）。

        弱模型/早停可能导致回复以连接词（及/与/然后…）、逗号或无结尾标点收尾。
        检测到不完整时，用一次低成本 LLM 续写把句子补全到正常句号/问号；
        仅尝试一次，失败（限流/网络/解析）即降级返回原文，绝不阻塞主回复。
        补全后经 enforce_brevity 再次清洗（清泄漏/粘连标点/主人身份词）。
        可用 self._auto_complete_enabled 关闭。

        分段续写（2026-07-28 修复）：之前把整段文本丢给续写器，若中间句截断、
        后面跟了完整句（如"申请预算及。建议协助评估走正规采购渠道。"），续写器会被
        后面的完整句带偏不补。现改为**只把第一个断点句（含之前）喂给续写器**补全，
        再把原始后半段拼回，避免后面完整句干扰、也不丢内容。
        """
        if not getattr(self, "_auto_complete_enabled", True):
            return text
        if not text or not _style._is_reply_incomplete(text):
            return text
        try:
            # 找第一个不完整断点（以连接词/逗号收尾的句），只补全到该断点。
            segments = _style._split_sentences(text)
            break_idx = -1
            for i, seg in enumerate(segments):
                if _style._segment_is_incomplete(seg):
                    break_idx = i
                    break
            # 正常不会走到这里（_is_reply_incomplete 已为真），兜底用整段。
            partial = "".join(segments[:break_idx + 1]).rstrip() if break_idx >= 0 else text.rstrip()
            rest = "".join(segments[break_idx + 1:]) if break_idx >= 0 else ""
            messages = [
                {"role": "system", "content": (
                    "你是文本续写器。下面是一句话的未完部分，请只输出它缺失的结尾，"
                    "使其成为一个语法完整、以句号或问号正常结尾的句子。"
                    "严禁重复已给出的内容，不要添加任何解释、前缀、引号或换行。"
                )},
                {"role": "user", "content": partial},
            ]
            resp = self.client.chat(messages, stream=False, temperature=0.2)
            cont = getattr(resp, "content", "") or ""
            cont = cont.strip()
            if not cont:
                return text
            # 去重：若 partial 末尾（去掉句末标点）以连接词结尾、且续写以同词开头，
            # 削掉续写开头的连接词，避免"及。及"这类重复。
            tail = partial.rstrip().rstrip("。！？!?")
            for c in _style._REPLY_CONNECTORS:
                if tail.endswith(c) and cont.startswith(c):
                    cont = cont[len(c):].lstrip()
                    break
            # 拼接：补后的前半段 + 原始后半段（断点之后的所有句子，原样保留）。
            combined = partial.rstrip().rstrip("。！？!?") + cont + rest
            finalized = self._enforce_brevity(combined)
            if not _style._is_reply_incomplete(finalized):
                logger.info("[续写补全] 已补全截断回复: %d -> %d 字符 (断点句=%d/%d)",
                            len(text), len(finalized), break_idx + 1, len(segments))
                return finalized
            # 仍不完整（极少见）：返回最佳努力结果，不再二次调用 LLM。
            logger.warning("[续写补全] 续写后仍未完整，返回最佳努力结果")
            return finalized
        except Exception as e:  # 任意异常均降级，保证主回复不受影响
            logger.warning("[续写补全] 调用失败，降级返回原文: %s", e)
            return text

    def _get_embedding_client(self):
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper。
        return _style.get_embedding_client(self)

    def _embed_message(self, text: str) -> list[float] | None:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper。
        return _style.embed_message(self, text)

    @staticmethod
    def _cosine(a, b) -> float:
        # 实际逻辑已拆到 src/llm/style.py；保留 @staticmethod + thin wrapper 以兼容测试 monkey-patch。
        return _style.cosine(a, b)

    def _is_document_query(self, query: str, query_embedding=None) -> bool:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper。
        return _style.is_document_query(self, query, query_embedding)

    def _extract_relevant_snippets(self, content: str, query: str, max_snippets: int = 2, max_chars_per_snippet: int = 2000) -> list[str]:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper。
        return _style.extract_relevant_snippets(
            content, query, max_snippets, max_chars_per_snippet,
        )

    def _retrieve_relevant_knowledge(self, query: str, query_embedding=None) -> tuple[str, float | None]:
        # 实际逻辑已拆到 src/llm/style.py；此处保留 thin wrapper。
        return _style.retrieve_relevant_knowledge(self, query, query_embedding)

    @functools.lru_cache(maxsize=1024)
    def _estimate_tokens(self, text: str) -> int:
        # 实际逻辑已拆到 src/llm/prompt_builder.py；此处保留 thin wrapper 以兼容测试 monkey-patch。
        return self._prompt_builder.estimate_tokens(text)

    def _estimate_cost(self, input_tokens: int, output_tokens: int, model_name: str) -> float:
        # 实际逻辑已拆到 src/llm/prompt_builder.py；此处保留 thin wrapper。
        return self._prompt_builder.estimate_cost(input_tokens, output_tokens, model_name)

    def _truncate_long_message(self, content: str, max_chars: int = 500) -> str:
        # 实际逻辑已拆到 src/llm/history.py；此处保留 thin wrapper。
        return _history.truncate_long_message(content, max_chars)

    def _apply_history_tiering(self, history: list[Message],
                               max_recent: int | None = None) -> list[Message]:
        # 实际逻辑已拆到 src/llm/history.py；此处保留 thin wrapper。
        return _history.apply_history_tiering(self, history, max_recent)

    def _read_cached_summary(self, chat_id: str, older: list[Message]) -> "Message | None":
        # 实际逻辑已拆到 src/llm/history.py；此处保留 thin wrapper。
        return _history.read_cached_summary(self, chat_id, older)

    def _maybe_schedule_summary(self, chat_id: str, older: list[Message]) -> None:
        # 实际逻辑已拆到 src/llm/history.py；此处保留 thin wrapper。
        return _history.maybe_schedule_summary(self, chat_id, older)

    def _build_user_message(self, message: Message, history: list[Message], query_embedding: list | None = None) -> list[dict]:
        # 实际逻辑已拆到 src/llm/prompt_builder.py；此处保留 thin wrapper 以兼容测试 monkey-patch。
        return self._prompt_builder.build_user_message(message, history, query_embedding)

    def _execute_tool_calls(self, tool_calls: list[dict], message: Message) -> tuple[list[dict], bool]:
        # 实际逻辑已拆到 src/llm/tool_orchestrator.py；此处保留 thin wrapper 以兼容测试 monkey-patch。
        return self._tool_orchestrator.execute_tool_calls(tool_calls, message)

    # 类属性常量（_BASE_TOOL_NAMES / _FALLBACK_TOOL_NAMES / _TOOL_RESULT_TTL / _TOOL_RESULT_DEFAULT_TTL）
    # 已拆到 src/llm/router.py 模块级 BASE_TOOL_NAMES / FALLBACK_TOOL_NAMES / TOOL_RESULT_TTL / TOOL_RESULT_DEFAULT_TTL。

    def _check_stale_tool_results(self, messages: list[dict]) -> str | None:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.check_stale_tool_results(self, messages)

    def _is_tool_for_platform(self, tool_name: str) -> bool:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.is_tool_for_platform(self, tool_name)

    def _filter_schemas_by_platform(self, schemas: list[dict]) -> list[dict]:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.filter_schemas_by_platform(self, schemas)

    def _keyword_match_tool_names(self, message_text: str,
                                  query_embedding: list[float] | None = None) -> set[str]:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.keyword_match_tool_names(self, message_text, query_embedding)

    def _semantic_tool_matches(self, query_embedding: list[float]) -> dict[str, float]:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.semantic_tool_matches(self, query_embedding)

    def _filter_tools_by_intent(self, message_text: str,
                                query_embedding: list[float] | None = None) -> list[dict]:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.filter_tools_by_intent(self, message_text, query_embedding)

    def _resolve_routing_mode(self) -> str:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.resolve_routing_mode(self)

    def _merge_proactive_action_tools(self, base_names: set[str],
                                      message_text: str,
                                      skill_activated: bool,
                                      skill_tools: set[str]) -> set[str]:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.merge_proactive_action_tools(
            self, base_names, message_text, skill_activated, skill_tools,
        )

    def _select_tools(self, message_text: str,
                      query_embedding: list[float] | None = None) -> list[dict]:
        # 实际逻辑已拆到 src/llm/router.py；此处保留 thin wrapper。
        return _router.select_tools(self, message_text, query_embedding)

    # ------------------------------------------------------------------
    # process_message 的各阶段私有子步骤。
    # 抽取原则：只抽「输入 / 输出与副作用边界清晰」的阶段，让 process_message
    # 退化为编排器；工具循环内高度耦合的可变状态（tools / messages / usage 计数 /
    # self_sent 标记）仍留在编排器里逐轮推进，避免为拆而拆引入行为漂移。
    # 所有子步骤保持与原内联代码完全一致的执行顺序、日志与副作用。
    # ------------------------------------------------------------------

    def _detect_stream_support(self, enable_stream: bool):
        """判定本轮能否走流式：需启用流式且 IM 适配器支持增量更新。"""
        return _detect_stream_support(self, enable_stream)

    def _apply_rag_empty_fallback(self, message: Message, messages: list[dict],
                                  query_vec: list[float] | None) -> None:
        """三级递进 RAG 空结果处理（就地修改 messages 与 thread-local 递进状态）。"""
        return _apply_rag_empty_fallback(self, message, messages, query_vec)


    def _activate_skills(self, message: Message, messages: list[dict],
                         query_vec: list[float] | None) -> list:
        """技能激活检测（Phase 3 组合路由：可能同时激活多个 composable 技能）。"""
        return _activate_skills(self, message, messages, query_vec)


    def _record_routing_trace_pre(self, message: Message, disposition: str,
                                  intent_action: str, routing_mode: str,
                                  routed_tools: list, activated: list,
                                  t_start: float) -> list:
        """记录路由质量数据（Phase 4 收敛 + 组合 + 语义路由可观测 + 全链路瀑布）。"""
        return _record_routing_trace_pre(self, message, disposition, intent_action, routing_mode, routed_tools, activated, t_start)


    def _apply_skill_round_limit(self, activated: list, max_rounds: int,
                                 messages: list[dict]) -> int:
        """技能激活时降低工具轮次上限，并就地注入并行调用提示；返回生效的轮次上限。"""
        return _apply_skill_round_limit(self, activated, max_rounds, messages)


    def _make_reply(self, text: str, already_sent: bool,
                    routing_mode: str, routed_tools: list) -> AgentReply:
        """产出回复并附带本轮工具路由信息（供决策追踪）。"""
        return _make_reply_fn(self, text, already_sent, routing_mode, routed_tools)


    def _finish_reply(self, text: str, self_sent_to_current_chat: bool,
                      routing_mode: str, routed_tools: list) -> AgentReply:
        """产出最终回复：若已通过工具自回复当前会话，则标记 already_sent 跳过发送。
        三级递进兜底：第3级时忽略 LLM 生成内容，用配置的硬编码兜底回复替换。"""
        return _finish_reply_fn(self, text, self_sent_to_current_chat, routing_mode, routed_tools)


    def _finalize_trace(self, reply: AgentReply, t_start: float, stages_pre: list,
                        llm_latency_ms: float, llm_rounds: int,
                        last_usage) -> AgentReply:
        """LLM 推理结束后补齐路由质量记录的耗时 / 轮次 / 完整瀑布。"""
        return _finalize_trace(self, reply, t_start, stages_pre, llm_latency_ms, llm_rounds, last_usage)


    def _maybe_converge_tools(self, response: LLMResponse, tools: list,
                              messages: list[dict], round_num: int,
                              converge_threshold: int,
                              converged: bool) -> tuple[list, bool]:
        """收敛护栏：若已连续多轮都在调工具且尚未综合作答，则在本轮执行后强制收敛。"""
        return _maybe_converge_tools(response, tools, messages, round_num, converge_threshold, converged)


    @staticmethod
    def _build_tool_call_assistant_message(response: LLMResponse) -> dict:
        """把本轮 LLM 的 tool_calls 组装成合法的 assistant 历史消息。"""
        return _build_tool_call_assistant_message(response)


    def _handle_discarded_tool_calls(self, response: LLMResponse,
                                     messages: list[dict], round_num: int) -> bool:
        """工具收敛纠正：本轮 LLM 试图调用已被移除的工具且未给出有效内容。"""
        return _handle_discarded_tool_calls(self, response, messages, round_num)


    @request_id_scope(prefix="llm")
    def process_message(self, message: Message,
                        history: list[Message] = None,
                        disposition: str = "",
                        intent_action: str = "llm",
                        enable_stream: bool = False) -> AgentReply:
        """Agent 主链路编排器：上下文组装 → RAG 递进 → 技能/工具路由 → 工具轮次
        循环 → 回复收口 → 落库追踪。"""
        return _process_message_fn(self, message, history, disposition, intent_action, enable_stream)


    def _handle_stream_response(self, stream, message: Message) -> Iterator[str]:
        """处理流式 LLM 响应，发送占位消息并逐步更新。"""
        return _handle_stream_response(stream, message, self)


    def extract_memories_from_conversation(self, messages: list[Message]) -> list[str]:
        """用 LLM 从对话中提取值得记住的信息。"""
        return _extract_memories_fn(self, messages)


    def summarize_conversation(self, messages: list[Message], max_messages: int = 0) -> str:
        """用 LLM 生成对话摘要。"""
        return _summarize_conversation_fn(self, messages, max_messages)

# test-line

# test-line
