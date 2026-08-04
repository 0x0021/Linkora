"""inject_rag_knowledge 五个分支独立单测。

设计要点：
- 不构造完整 LLMAgent，只用 FakeAgent 桩对象替入 agent 依赖的三个方法
  （_get_embedding_client / _is_document_query / _retrieve_relevant_knowledge）
- 这让单测无需启动 main 即可跑，启动时间从 ~10s 降到 <0.1s
- 覆盖：disabled / short / intent-miss / no-embed / 正常注入（意图命中 + 高置信短路）
- 验证 RagInjectResult 的五个字段语义
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.rag_inject import inject_rag_knowledge, RagInjectResult


class FakeEmbedding:
    """确定性伪 embedding：让意图分类与向量短路分支可控。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def embed(self, text: str) -> list[float]:
        # 返回两维向量：[k_intent, c_intent]
        # _is_document_query 由 FakeAgent 拦截，不真正走 cosine；
        # 仅用于兼容「向量化失败」「query_embedding=None」两种分支。
        return [0.0, 0.0]


class FakeAgent:
    """minimal agent stub：只实现 inject_rag_knowledge 关心的三个方法。"""

    def __init__(
        self,
        *,
        intent_returns: bool = True,
        kb_returns: tuple[str, float | None] = ("", None),
        embed_enabled: bool = True,
    ):
        self._intent_returns = intent_returns
        self._kb_returns = kb_returns
        self._emb_client = FakeEmbedding(enabled=embed_enabled) if embed_enabled else None

    def _get_embedding_client(self):
        return self._emb_client

    def _is_document_query(self, query: str, query_embedding=None) -> bool:
        return self._intent_returns

    def _retrieve_relevant_knowledge(self, query: str, query_embedding=None):
        return self._kb_returns


def _run(query, *, auto=True, intent_only=True, agent=None, q_emb=None, sys=""):
    """helper：调用 inject_rag_knowledge，固定 system_content 起点为空字符串。"""
    if agent is None:
        agent = FakeAgent()
    return inject_rag_knowledge(
        query=query,
        system_content=sys,
        agent=agent,
        rag_auto_inject=auto,
        rag_intent_only=intent_only,
        query_embedding=q_emb,
    )


class TestRagInjectDisabled:
    """分支 1：rag_auto_inject=False → 完全跳过。"""

    def test_disabled_short_circuits(self):
        sys_content = "base"
        agent = FakeAgent(intent_returns=True, kb_returns=("【相关知识】xxx", 0.9))
        new_sys, result = _run(
            "VPN 怎么配置",
            auto=False,
            sys=sys_content,
            agent=agent,
        )
        assert new_sys == sys_content, "关闭注入时 system_content 不应被修改"
        assert result.injected is False
        assert result.skipped_reason == "disabled"
        assert result.best_score is None
        assert result.intent_ok is False

    def test_disabled_even_with_perfect_kb(self):
        """即使 KB 命中极高，关闭注入也不应触发。"""
        agent = FakeAgent(intent_returns=True, kb_returns=("【相关知识】VPN 配置步骤", 0.95))
        _, result = _run("VPN 怎么配置", auto=False, agent=agent)
        assert result.injected is False
        assert result.skipped_reason == "disabled"


class TestRagInjectShortMessage:
    """分支 2：query 太短 / 纯表情 / 无汉字英文 → skipped_reason='short'。"""

    def test_short_message_skipped(self):
        agent = FakeAgent(intent_returns=True)
        new_sys, result = _run("hi", agent=agent)
        assert new_sys == ""
        assert result.injected is False
        assert result.skipped_reason == "short"

    def test_emoji_only_skipped(self):
        """5 字符但无汉字英文（如纯 emoji/标点）也应跳过。"""
        agent = FakeAgent()
        _, result = _run("😀😁😂🤣😃", agent=agent)
        assert result.skipped_reason == "short"

    def test_punctuation_only_skipped(self):
        agent = FakeAgent()
        _, result = _run("？？？！！", agent=agent)
        assert result.skipped_reason == "short"

    def test_below_5_chars_skipped(self):
        """长度 <5 但有汉字也算 short（双重门：长度 + RE_HAS_TEXT）。"""
        agent = FakeAgent()
        _, result = _run("你好", agent=agent)
        assert result.skipped_reason == "short"


class TestRagInjectIntentMiss:
    """分支 3：意图未命中 + 分数 < 0.78 → skipped_reason='intent-miss'。"""

    def test_intent_false_low_score(self):
        """intent_returns=False + best_score=0.5 → 意图未命中 + 未达高置信阈值"""
        agent = FakeAgent(intent_returns=False, kb_returns=("【相关知识】手册片段", 0.5))
        new_sys, result = _run("VPN 配置步骤", agent=agent)
        assert result.injected is False
        assert result.skipped_reason == "intent-miss"
        assert result.intent_ok is False
        assert result.best_score is None  # 因 kb_grounded=False 而被清空
        assert "【相关知识】" not in new_sys

    def test_rag_intent_only_false_always_injects(self):
        """rag_intent_only=False 时意图未命中也注入（兜底放行）。"""
        agent = FakeAgent(intent_returns=False, kb_returns=("【相关知识】手册片段", 0.5))
        new_sys, result = _run("VPN 配置步骤", intent_only=False, agent=agent)
        assert result.injected is True
        assert "【相关知识】" in new_sys
        # intent_ok 是「是否注入」信号，不是「is_document_query」原始返回值。
        # rag_intent_only=False 时意图始终视为 True（by design），所以即便 FakeAgent
        # 的 _is_document_query 返回 False，注入仍发生。
        assert result.intent_ok is True


class TestRagInjectNoEmbed:
    """分支 4：embedding 不可用 + 无 KB 命中 → skipped_reason='no-embed'。"""

    def test_no_embed_no_kb(self):
        agent = FakeAgent(
            intent_returns=False,
            kb_returns=("", None),
            embed_enabled=False,  # emb_client=None
        )
        _, result = _run("VPN 配置步骤", agent=agent)
        assert result.injected is False
        assert result.best_score is None
        # intent-miss 还是 no-embed？看实现：intent-miss 优先（检索到内容时），
        # 只有 retrieve_relevant_knowledge 返回 ("", None) 才走到 no-embed 分支
        assert result.skipped_reason == "no-embed"

    def test_no_embed_with_high_intent(self):
        """无 embedding 但意图返回 True → 仍按意图检索 KB，结果为空 → no-embed"""
        agent = FakeAgent(
            intent_returns=True,
            kb_returns=("", None),
            embed_enabled=False,
        )
        _, result = _run("VPN 配置步骤", agent=agent)
        assert result.injected is False
        assert result.skipped_reason == "no-embed"


class TestRagInjectNormalInject:
    """分支 5：正常注入。"""

    def test_intent_hit_injects(self):
        """意图命中 + KB 有命中 → 注入并标记 injected=True。"""
        kb_text = "【相关知识】\n1. VPN手册（82%）\n  - 配置步骤…"
        agent = FakeAgent(intent_returns=True, kb_returns=(kb_text, 0.82))
        sys_base = "[主人风格画像] 用OWNER的口吻"
        new_sys, result = _run("VPN 怎么配置步骤", sys=sys_base, agent=agent)
        assert result.injected is True
        assert result.intent_ok is True
        assert result.best_score == 0.82
        assert result.skipped_reason == ""
        assert "【相关知识】" in new_sys
        assert "VPN手册" in new_sys
        # system_content 必须包含使用规则（防 AI 复述）—— v5 新措辞
        assert "★RAG 知识库答案" in new_sys
        # 原 system 必须保留
        assert "OWNER" in new_sys

    def test_high_confidence_short_circuit(self):
        """意图未命中但 best_score ≥ 置信阈值 → 仍注入（双保险）。"""
        kb_text = "【相关知识】\n1. 紧急操作指南（85%）\n  - 步骤…"
        agent = FakeAgent(intent_returns=False, kb_returns=(kb_text, 0.85))
        new_sys, result = _run("VPN 怎么配置", agent=agent)
        assert result.injected is True
        # intent_ok 透传真实意图（False）
        assert result.intent_ok is False
        assert result.best_score == 0.85
        assert result.skipped_reason == ""

    def test_intent_hit_injects_without_query_embedding(self):
        """query_embedding=None 也能跑（embedding 客户端降级到本地 embed）。"""
        kb_text = "【相关知识】\n1. 手册"
        agent = FakeAgent(intent_returns=True, kb_returns=(kb_text, 0.7))
        # 不传 query_embedding，由 inject_rag_knowledge 内部走 agent._get_embedding_client().embed(query)
        _, result = _run("VPN 怎么配置", agent=agent, q_emb=None)
        assert result.injected is True

    def test_embedding_call_failure_logged(self):
        """embed 抛异常时不崩溃，q_emb 走 None 分支。"""
        class _BadEmbedding:
            enabled = True
            def embed(self, text):
                raise RuntimeError("模拟 embedding 服务挂掉")
        agent = FakeAgent(intent_returns=True, kb_returns=("【相关知识】x", 0.7))
        agent._emb_client = _BadEmbedding()
        # query_embedding=None → 内部会调 embed(query) → raise → 捕获后 q_emb=None
        # 然后走 _is_document_query(query, None) → 由 FakeAgent 接管返回 True
        # 再走 _retrieve_relevant_knowledge(query, None) → 返回 ("【相关知识】x", 0.7)
        _, result = _run("VPN 怎么配置", agent=agent, q_emb=None)
        assert result.injected is True
        assert result.best_score == 0.7


class TestRagInjectEdgeCases:
    """边界场景：system_content 拼接方向、连续调用隔离。"""

    def test_empty_system_content_appends(self):
        """起始 system_content 为空时也能拼。"""
        agent = FakeAgent(intent_returns=True, kb_returns=("【相关知识】手册", 0.9))
        new_sys, _ = _run("VPN 配置", sys="", agent=agent)
        assert new_sys.startswith("\n")
        assert "【相关知识】" in new_sys

    def test_uses_provided_query_embedding(self):
        """传 query_embedding 时不应再调用 embed。"""
        class _SpyEmbed(FakeEmbedding):
            calls = []
            def embed(self, text):
                _SpyEmbed.calls.append(text)
                return [1.0, 0.0]
        agent = FakeAgent(intent_returns=True, kb_returns=("【相关知识】x", 0.8))
        agent._emb_client = _SpyEmbed()
        q_emb_provided = [1.0, 0.0]
        _run("VPN 配置", agent=agent, q_emb=q_emb_provided)
        assert _SpyEmbed.calls == [], "提供 query_embedding 时不应调用 embed()"

    def test_raginjectresult_dataclass_fields(self):
        """校验 RagInjectResult 字段类型。"""
        r = RagInjectResult(
            injected=True,
            relevant_knowledge="kb",
            best_score=0.8,
            intent_ok=True,
            skipped_reason="",
        )
        assert isinstance(r.injected, bool)
        assert isinstance(r.relevant_knowledge, str)
        assert isinstance(r.best_score, float)
        assert isinstance(r.intent_ok, bool)
        assert isinstance(r.skipped_reason, str)