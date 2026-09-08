"""RAG「严格问答模式」（智能问答）单测。

覆盖三件事：
1. 模式判定（resolve_strict_mode）：默认关 / 显式开 / 配置缺失或脏数据一律关
2. 检索与生成装配（inject_rag_knowledge + prompt_builder）：
   开启时强制检索、跳过意图门控与短消息过滤、注入严格约束块、跳过公共记忆
3. 未命中短路（process_message）：知识库无命中时不调用 LLM，直接返回固定文案

关闭态的回归同样重要：本文件专门验证「关闭时行为不变」，防止开关反过来
污染原有问答链路。
"""

from __future__ import annotations

from datetime import datetime

from src.llm.agent_steps.reply import process_message
from src.llm.prompt_builder import PromptBuilder
from src.llm.rag_inject import inject_rag_knowledge
from src.llm.rag_strict import (
    STRICT_BLOCK_MARK,
    StrictModeConfig,
    build_strict_block,
    resolve_strict_mode,
)
from src.models import Message


# --------------------------------------------------------------------------
# 桩对象
# --------------------------------------------------------------------------
class _Advanced:
    def __init__(self, **kw):
        # enforce_brevity 等回复后处理会读这些高级参数
        self.hard_truncation_chars = 800
        self.max_chars_daily_chat = 50
        self.max_chars_tech_issue = 100
        self.rag_strict_mode = kw.get("rag_strict_mode", False)
        self.rag_strict_min_similarity = kw.get("rag_strict_min_similarity", 0.50)
        self.rag_strict_max_results = kw.get("rag_strict_max_results", 3)
        self.rag_strict_no_hit_reply = kw.get(
            "rag_strict_no_hit_reply", "知识库中暂未收录相关内容，我无法凭已有信息作答。"
        )


class _Cfg:
    """对齐真实 LLMAgent：agent.config 就是 LlmConfig（advanced 直接在上面）。"""

    def __init__(self, advanced=None):
        self.advanced = advanced
        self.model = "test-model"
        self.max_tool_rounds = 3
        self.converge_after_tool_rounds = 3


class _Agent:
    """inject_rag_knowledge 关心的最小桩。"""

    def __init__(self, *, intent_returns: bool = True, kb_returns=("", None), config=None):
        self._intent_returns = intent_returns
        self._kb_returns = kb_returns
        self.config = config
        self._rag_min_similarity = 0.6
        self._rag_max_results = 1

    def _get_embedding_client(self):
        return None

    def _is_document_query(self, query, query_embedding=None):
        return self._intent_returns

    def _retrieve_relevant_knowledge(self, query, query_embedding=None):
        return self._kb_returns


def _msg(content: str = "VPN 怎么配置") -> Message:
    return Message(
        msg_id="m1", chat_id="c1", chat_type="single", chat_name="",
        sender_id="u1", sender_name="张三", content=content,
        msg_type="text", timestamp=datetime(2026, 9, 8, 10, 0, 0),
    )


# --------------------------------------------------------------------------
# 1. 模式判定
# --------------------------------------------------------------------------
class TestResolveStrictMode:
    def test_no_config_means_off(self):
        class _Bare:
            config = None

        assert resolve_strict_mode(_Bare()).enabled is False

    def test_default_off(self):
        agent = _Agent(config=_Cfg(advanced=_Advanced()))
        assert resolve_strict_mode(agent).enabled is False

    def test_enabled_returns_params(self):
        adv = _Advanced(
            rag_strict_mode=True,
            rag_strict_min_similarity=0.66,
            rag_strict_max_results=5,
            rag_strict_no_hit_reply="无可奉告",
        )
        got = resolve_strict_mode(_Agent(config=_Cfg(advanced=adv)))
        assert got.enabled is True
        assert got.min_similarity == 0.66
        assert got.max_results == 5
        assert got.no_hit_reply == "无可奉告"

    def test_dirty_config_falls_back_to_off(self):
        """配置类型异常/缺失时解析失败即关闭，绝不让严格模式意外接管。"""
        class _Dirty:
            advanced = object()  # 无 rag_strict_mode 字段

        class _Holder:
            config = _Dirty()

        assert resolve_strict_mode(_Holder()).enabled is False

    def test_bad_numbers_fall_back_to_defaults(self):
        adv = _Advanced(rag_strict_mode=True, rag_strict_min_similarity="abc",
                        rag_strict_max_results=None)
        got = resolve_strict_mode(_Agent(config=_Cfg(advanced=adv)))
        assert got.enabled is True
        assert got.min_similarity == 0.50
        assert got.max_results == 3

    def test_empty_no_hit_reply_falls_back(self):
        adv = _Advanced(rag_strict_mode=True, rag_strict_no_hit_reply="   ")
        got = resolve_strict_mode(_Agent(config=_Cfg(advanced=adv)))
        assert got.no_hit_reply == "知识库中暂未收录相关内容，我无法凭已有信息作答。"


# --------------------------------------------------------------------------
# 2. 提示块
# --------------------------------------------------------------------------
class TestBuildStrictBlock:
    def test_block_contains_mark_knowledge_and_reply(self):
        block = build_strict_block("【相关知识】VPN 步骤…", "未收录")
        assert block.startswith(STRICT_BLOCK_MARK) or STRICT_BLOCK_MARK in block
        assert "【知识库检索结果】" in block
        assert "VPN 步骤" in block
        assert "未收录" in block
        # 严格模式不给"降级兜底 / 自己去搜"这类许可
        assert "降级兜底" not in block
        assert "kb_search" not in block


# --------------------------------------------------------------------------
# 3. 检索与注入（inject_rag_knowledge）
# --------------------------------------------------------------------------
class TestStrictInjection:
    def test_off_behaviour_unchanged_short_message_skipped(self):
        """关闭时：短消息仍按原逻辑跳过（回归保护）。"""
        agent = _Agent(kb_returns=("【相关知识】x", 0.9))
        cfg = StrictModeConfig(enabled=False)
        _, result = inject_rag_knowledge(
            query="你好", system_content="", agent=agent,
            rag_auto_inject=True, rag_intent_only=True, strict=cfg,
        )
        assert result.skipped_reason == "short"
        assert result.injected is False

    def test_on_forces_retrieval_for_short_message(self):
        """开启时：短消息也进入检索（检索不到即未命中，不会退化成通用知识硬答）。"""
        agent = _Agent(kb_returns=("", None))
        cfg = StrictModeConfig(enabled=True)
        _, result = inject_rag_knowledge(
            query="你好", system_content="", agent=agent,
            rag_auto_inject=True, rag_intent_only=False, strict=cfg,
        )
        assert result.skipped_reason != "short"
        assert result.injected is False

    def test_on_hit_builds_strict_block(self):
        kb = "【相关知识】\n1. VPN手册\n  - 步骤…"
        agent = _Agent(kb_returns=(kb, 0.72))
        cfg = StrictModeConfig(enabled=True, no_hit_reply="未收录")
        new_sys, result = inject_rag_knowledge(
            query="VPN 怎么配置", system_content="BASE", agent=agent,
            rag_auto_inject=True, rag_intent_only=False, strict=cfg,
        )
        assert result.injected is True
        assert result.best_score == 0.72
        assert result.rag_block
        assert STRICT_BLOCK_MARK in result.rag_block
        assert "VPN手册" in result.rag_block
        # 严格块取代普通 RAG 前置指令
        assert "★RAG 知识库答案" not in new_sys
        assert "降级兜底" not in new_sys

    def test_on_uses_strict_threshold_override(self):
        """严格模式应把严格阈值临时压到 agent 上（否则召回门槛沿用旧值）。"""
        seen = {}

        class _SpyAgent(_Agent):
            def _retrieve_relevant_knowledge(self, query, query_embedding=None):
                seen["min_sim"] = self._rag_min_similarity
                seen["max_res"] = self._rag_max_results
                return ("【相关知识】x", 0.8)

        agent = _SpyAgent()
        inject_rag_knowledge(
            query="VPN 怎么配置", system_content="", agent=agent,
            rag_auto_inject=True, rag_intent_only=False,
            override_min_similarity=0.42, override_max_results=4,
            strict=StrictModeConfig(enabled=True),
        )
        assert seen["min_sim"] == 0.42
        assert seen["max_res"] == 4
        # 调用结束后必须还原，避免污染后续轮次
        assert agent._rag_min_similarity == 0.6
        assert agent._rag_max_results == 1


# --------------------------------------------------------------------------
# 4. prompt_builder 接线
# --------------------------------------------------------------------------
class _PbResult:
    def __init__(self, injected, block=""):
        self.injected = injected
        self.rag_block = block
        self.relevant_knowledge = block if injected else ""
        self.best_score = 0.8 if injected else None
        self.intent_ok = True
        self.skipped_reason = ""
        self.citations = []


class _PbAgent:
    user_name = "OWNER"
    _max_input_tokens = 100000
    _rag_auto_inject = False
    _rag_intent_only = True

    def __init__(self, config=None):
        self.config = config
        self._last_kb_best_score = None
        self._last_kb_hit = False
        self._last_kb_query_intent = False
        self._last_kb_citations = []
        self._last_kb_citations_raw = []
        self._last_kb_relevant_knowledge = ""

    def _build_system_prompt(self, **kw):
        return "system prompt body"

    def _apply_history_tiering(self, history):
        return history

    def _truncate_long_message(self, c):
        return c


def _patch_pb(monkeypatch, captured: dict, injected: bool, block: str = ""):
    import src.llm.prompt_builder as pbmod

    def _fake_inject(**kwargs):
        captured.update(kwargs)
        result = _PbResult(injected, block)
        return kwargs.get("system_content", "") + block, result

    def _fake_memory(**kwargs):
        captured["memory_called"] = True
        from src.llm.memory_inject import PublicMemoryInjectResult
        return kwargs.get("system_content", ""), PublicMemoryInjectResult(injected=False)

    monkeypatch.setattr(pbmod, "inject_rag_knowledge", _fake_inject)
    monkeypatch.setattr(pbmod, "inject_public_memories", _fake_memory)
    monkeypatch.setattr(
        pbmod, "wrap_incoming_message",
        lambda message, truncate_fn=None: message.content,
    )


class TestPromptBuilderStrictWiring:
    def test_off_keeps_original_flags(self, monkeypatch):
        captured: dict = {}
        _patch_pb(monkeypatch, captured, injected=False)
        agent = _PbAgent(config=_Cfg(advanced=_Advanced()))
        PromptBuilder(agent).build_user_message(_msg(), [])

        assert captured["rag_auto_inject"] is False   # 沿用 agent 原值
        assert captured["rag_intent_only"] is True    # 沿用 agent 原值
        assert captured["override_min_similarity"] is None
        assert captured["strict"].enabled is False
        assert captured["memory_called"] is True      # 公共记忆照常注入

    def test_on_forces_injection_and_skips_public_memory(self, monkeypatch):
        captured: dict = {}
        _patch_pb(monkeypatch, captured, injected=True, block=STRICT_BLOCK_MARK + " 块")
        adv = _Advanced(rag_strict_mode=True, rag_strict_min_similarity=0.44,
                        rag_strict_max_results=2)
        agent = _PbAgent(config=_Cfg(advanced=adv))
        messages = PromptBuilder(agent).build_user_message(_msg(), [])

        assert captured["rag_auto_inject"] is True    # 强制检索
        assert captured["rag_intent_only"] is False   # 跳过意图门控
        assert captured["override_min_similarity"] == 0.44
        assert captured["override_max_results"] == 2
        assert captured["strict"].enabled is True
        assert "memory_called" not in captured        # 公共记忆被跳过

        # 严格块被抽成独立 system 消息，紧贴 user 之前（近因位）
        contents = [m["content"] for m in messages]
        assert any(STRICT_BLOCK_MARK in (c or "") for c in contents)
        idx_block = next(i for i, c in enumerate(contents) if STRICT_BLOCK_MARK in (c or ""))
        idx_user = next(i for i, m in enumerate(messages) if m["role"] == "user")
        assert idx_block < idx_user
        # 命中状态透传（供 process_message 判断是否短路）
        assert agent._last_kb_hit is True


# --------------------------------------------------------------------------
# 5. process_message 未命中短路
# --------------------------------------------------------------------------
class _TL:
    rag_empty_fallback_level = 0


class _ProcAgent:
    """process_message 关心的最小桩。"""

    def __init__(self, config, *, kb_hit: bool):
        self.config = config
        self._tl = _TL()
        self.store = None
        self.skill_router = None
        self.user_name = "OWNER"
        self.user_title = ""
        self._rq_id = None
        # process_message 每轮会先重置命中状态，再由 prompt_builder 写回；
        # 这里在 _build_user_message 里模拟真实写回。
        self._kb_hit = kb_hit
        self._last_kb_hit = kb_hit
        self._auto_complete_enabled = False  # 关闭自动续写，避免桩 client 二次调用
        self._last_kb_best_score = 0.8 if kb_hit else None
        self._last_kb_citations = []
        self.chat_calls = 0
        # 关闭态分支需要读到的三级递进字段
        self._rag_empty_fallback_enabled = True
        self._rag_max_retry_rounds = 1
        self._last_rag_empty = False
        self._last_kb_query_intent = False

    def _embed_message(self, text):
        return None

    def _build_user_message(self, message, history, query_embedding=None):
        # 模拟真实 prompt_builder：把本轮检索命中状态写回 agent
        self._last_kb_hit = self._kb_hit
        return [{"role": "system", "content": "s"}, {"role": "user", "content": message.content}]

    def _select_tools(self, text, query_embedding=None):
        return [
            {"function": {"name": "kb_search"}},
            {"function": {"name": "web_search"}},
        ]

    def _resolve_routing_mode(self):
        return "llm"

    def _apply_rag_empty_fallback(self, message, messages, query_vec):
        self.fallback_called = True

    def _check_stale_tool_results(self, messages):
        return None


class _Client:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, stream=False):
        self.calls += 1
        raise AssertionError("严格模式未命中时不应调用 LLM")


class TestProcessMessageStrictShortCircuit:
    def test_no_hit_returns_fixed_reply_without_llm(self):
        adv = _Advanced(rag_strict_mode=True, rag_strict_no_hit_reply="未收录，别问我")
        agent = _ProcAgent(_Cfg(advanced=adv), kb_hit=False)
        agent.client = _Client()

        reply = process_message(agent, _msg("VPN 怎么配置"))

        assert reply.text == "未收录，别问我"
        assert reply.already_sent is False
        assert agent.client.calls == 0
        # 严格模式下不进三级递进兜底（其引导追问话术不是知识库内容）
        assert getattr(agent, "fallback_called", False) is False

    def test_hit_proceeds_to_llm(self):
        adv = _Advanced(rag_strict_mode=True)
        agent = _ProcAgent(_Cfg(advanced=adv), kb_hit=True)

        class _HitClient:
            def __init__(self):
                self.calls = 0
                self.tool_names = None

            def chat(self, messages, tools=None, stream=False):
                self.calls += 1
                self.tool_names = [
                    t.get("function", {}).get("name") for t in (tools or [])
                ]
                from src.llm.client import LLMResponse
                return LLMResponse(content="依据资料回答", tool_calls=[], finish_reason="stop", usage=None)

        agent.client = _HitClient()
        reply = process_message(agent, _msg("VPN 怎么配置"))
        assert reply.text == "依据资料回答"
        assert agent.client.calls == 1
        # 严格模式只保留 kb_search（web_search 等外部信息源必须被剔除）
        assert agent.client.tool_names == ["kb_search"]

    def test_off_short_circuit_disabled(self):
        """关闭时：未命中也照常调用 LLM（原逻辑不变）。"""
        agent = _ProcAgent(_Cfg(advanced=_Advanced()), kb_hit=False)

        class _OkClient:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools=None, stream=False):
                self.calls += 1
                from src.llm.client import LLMResponse
                return LLMResponse(content="通用知识回答", tool_calls=[], finish_reason="stop", usage=None)

        agent.client = _OkClient()
        reply = process_message(agent, _msg("今天天气如何"))
        assert reply.text == "通用知识回答"
        assert agent.client.calls == 1
