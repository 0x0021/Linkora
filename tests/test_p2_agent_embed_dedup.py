"""P2-4 回归：同一 query 不应被重复向量化。

背景：process_message 在 _build_user_message（RAG 语义意图）与后续语义路由
（技能评分 / 工具兜底）两处原本各 embed 一次同一条消息文本，浪费本地 embedding
算力。修复后 process_message 只 embed 一次，并把向量传给 _build_user_message 复用。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from src.llm.agent import LLMAgent
from src.models import Message


def _make_agent(client):
    config = MagicMock()
    config.system_prompt = ""
    # 补 advanced 真实数值：否则 agent 构造时 _max_input_tokens 等会变成 MagicMock，
    # 触发 token 截断比较 `len > MagicMock` 崩溃（与去重逻辑本身无关）。
    adv = config.advanced
    adv.max_input_tokens = 12000
    adv.rag_auto_inject = True
    adv.rag_intent_only = True
    adv.rag_min_similarity = 0.6
    adv.rag_max_results = 2
    return LLMAgent(
        config=config,
        client=client,
        tool_router=None,
        user_name="",
        user_dept="",
        org_name="",
        store=None,
    )


def _msg(content, role="user", sender="u", chat_id="c1"):
    return Message(
        msg_id=f"m_{role}_{content[:10]}",
        chat_id=chat_id,
        chat_type="group",
        chat_name="g",
        sender_id=sender,
        sender_name=sender,
        content=content,
        msg_type="text",
        timestamp=datetime.now(),
        role=role,
    )


class TestBuildUserMessageEmbedDedup:
    def test_passed_embedding_reused_no_extra_embed(self):
        agent = _make_agent(MagicMock())
        agent._build_system_prompt = lambda **kw: ""
        counter = {"n": 0}

        class FakeEmb:
            enabled = True

            def embed(self, t):
                counter["n"] += 1
                return [0.1, 0.2]

        agent._get_embedding_client = lambda: FakeEmb()
        agent._is_document_query = lambda *a, **k: True
        agent._retrieve_relevant_knowledge = lambda *a, **k: ("", None)

        msg = _msg("帮我查一下审批流程", role="user")

        # 不传 query_embedding → 应 embed 一次
        agent._build_user_message(msg, [])
        assert counter["n"] == 1

        # 传入已算好的向量 → 不应再 embed（P2-4 去重核心）
        agent._build_user_message(msg, [], query_embedding=[0.1, 0.2])
        assert counter["n"] == 1, "传入 query_embedding 后不应重复 embed"
