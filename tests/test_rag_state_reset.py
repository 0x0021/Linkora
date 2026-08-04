#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：锁定两处已修复的潜在缺陷（不依赖真实凭据 / 网络）

  #1  `src/llm/agent.py` —— RAG 命中遥测状态 `_last_kb_*` 每轮泄漏
      ---------------------------------------------------------------
      `_last_kb_hit / _last_kb_best_score / _last_kb_query_intent` 走
      threading.local，原本只在“消息够长且是文档意图”的检索分支内赋值。
      短消息 / 闲聊跳过检索时，上一轮的命中状态会残留，被 Feature A
      （低置信转人工）的 `reply.confidence / evidence_source` 错误沿用。
      修复：在 `_build_user_message` 入口重置为干净值。
      本测试：先文档查询（命中）→ 再闲聊（不手动重置）→ 闲聊后状态必须为
      干净值；若修复被回退，闲聊会带着上一轮 True 残留，断言 FAIL。

  #2  `src/memory/sqlite_store.py` —— `init_db` 迁移非幂等
      -----------------------------------------------------
      老表迁移每次启动都对 `blocked_conversations` 跑两条
      `ALTER ... ADD COLUMN`，全新库（建表 DDL 已含这两列）必抛
      `duplicate column name`。修复：先 `PRAGMA table_info` 查已存在列再 ALTER。
      本测试：对同一 store 多次调用 `init_db()` 不得抛异常。

确定性离线 embedding（FakeEmb）复用 test_rag_gating.py / validate_runtime_injection.py
的同思路，保证可复现。
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.llm.agent import LLMAgent                 # noqa: E402
from src.memory.sqlite_store import SQLiteStore     # noqa: E402
from src.models import Message                       # noqa: E402
from src.tools.kb_search import KBSearchTool         # noqa: E402


# ============================================================================
# 确定性离线 embedding（关键词特征向量）
# ============================================================================
class FakeEmb:
    """与真实 EmbeddingClient 接口兼容的确定性 embedding（enabled=True 走语义路径）。"""

    DIM = 8
    _HOWTO = ["怎么", "如何", "配置", "设置", "申请", "流程", "安装", "使用",
              "操作", "步骤", "规范", "手册", "vpn", "打印机", "账号", "权限",
              "开通", "教程", "指南"]
    _CASUAL = ["你好", "在吗", "天气", "吃什么", "谢谢", "早", "晚安", "哈哈",
               "收到", "忙", "早上好", "晚上好"]

    def __init__(self, enabled: bool = True):
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def embed(self, text: str) -> list[float]:
        if not self._enabled:
            return []
        t = (text or "").lower()
        howto = any(k in t for k in self._HOWTO)
        v = [0.0] * self.DIM
        v[0] = 1.0 if howto else 0.0
        v[3] = 1.0 if ("vpn" in t or "配置" in t) else 0.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


# ============================================================================
# 最小可用配置 + 共用常量
# ============================================================================
def _build_config():
    advanced = SimpleNamespace(
        max_chars_daily_chat=50,
        max_chars_tech_issue=120,
        hard_truncation_chars=300,
        rag_min_similarity=0.6,
        rag_max_results=3,
        rag_auto_inject=True,
        rag_intent_only=True,          # 走真实意图门控（FakeEmb 让文档查询通过）
        rag_max_content_chars=1200,
    )
    return SimpleNamespace(
        system_prompt="你是 {user_name} 的数字分身，平台 {platform}。",
        advanced=advanced,
        few_shot_examples=None,
        persona_style_prompt="",
        persona_style_prompts={},
    )


PERSONA = ("主人沟通风格：干脆直接、不绕弯子；技术答复先给解法再补备选；"
           "闲聊一两句收住，绝不列编号列表。")
FEWSHOT = [
    {"user": "打印机连不上咋办", "assistant": "先看网线，再重启打印机，还不行重装驱动。"},
    {"user": "这次团建去哪", "assistant": "听安排的，到地儿干活就行。"},
]
KB_TITLE = "VPN 配置规范"
KB_CONTENT = ("VPN 配置步骤：1) 打开设置-网络-VPN；2) 填入网关地址与账号；"
             "3) 选择 L2TP 协议并保存；4) 连接后验证内网可达。")
QUERY_DOC = "VPN怎么配置"
QUERY_CHAT = "周末有空去爬山吗"  # 纯闲聊，不含 _HOWTO 关键词（避免 FakeEmb 虚高相似度）


def _message(content: str) -> Message:
    return Message(
        msg_id="m1", chat_id="c1", chat_type="single", chat_name=None,
        sender_id="u_zhang", sender_name="张三", content=content,
        msg_type="text", timestamp=datetime.now(),
    )


@pytest.fixture
def agent():
    """构造真实 LLMAgent（绑定真实 store / KBSearchTool / FakeEmb），停在 LLM 调用前。"""
    tmp = tempfile.mkdtemp(prefix="rag_state_")
    db_path = os.path.join(tmp, "test.db")
    emb = FakeEmb(enabled=True)

    store = SQLiteStore(db_path=db_path)
    store._memory_ops_repo.save_style_profile({"prompt": PERSONA, "confidence": "high"}, "auto")

    doc_id = store._kb_repo.add_kb_document(title=KB_TITLE, doc_type="doc", source="回归测试")
    store._kb_repo.add_kb_chunks(doc_id, [KB_CONTENT])
    chunk_id = store._kb_repo.list_kb_chunks(doc_id)[0]["id"]
    # 关键：把查询文本向量作为该分块向量，保证 faiss 检索相似度≈1.0
    store._kb_repo.update_chunk_embedding(chunk_id, emb.embed(QUERY_DOC))

    kb_tool = KBSearchTool(store, {"enabled": True})
    kb_tool.embedding_client = emb   # 复用查询向量，跳过模型
    tool_router = SimpleNamespace(_tools={"kb_search": kb_tool})

    return LLMAgent(
        config=_build_config(),
        client=SimpleNamespace(),     # 不触发任何 LLM 调用
        tool_router=tool_router,
        user_name="OWNER",
        user_dept="研发中心",
        org_name="某科技公司",
        store=store,
        platform_id="dingtalk",
        few_shot_examples=FEWSHOT,
    )


# ============================================================================
# #1 核心回归：RAG 命中状态每轮自动重置（防修复被回退）
# ============================================================================
def test_rag_state_reset_between_turns(agent):
    """文档查询命中后，紧接着的闲聊必须回到干净状态（不手动重置！）。"""
    # 第一轮：文档查询 → 应命中
    agent._build_user_message(_message(QUERY_DOC), history=[])
    assert agent._last_kb_hit is True
    assert agent._last_kb_best_score is not None
    assert agent._last_kb_best_score > 0.9
    assert agent._last_kb_query_intent is True

    # 第二轮：闲聊（门控应拦截 RAG）—— 不手动重置，依赖 _build_user_message 入口重置。
    # 若 #1 修复被回退，此处会残留上一轮的 True，断言失败。
    agent._build_user_message(_message(QUERY_CHAT), history=[])
    assert agent._last_kb_hit is False
    assert agent._last_kb_best_score is None
    assert agent._last_kb_query_intent is False


def test_rag_state_clean_on_first_turn(agent):
    """首条消息即闲聊，状态也必须是干净初始值（不依赖任何前置命中）。"""
    agent._build_user_message(_message(QUERY_CHAT), history=[])
    assert agent._last_kb_hit is False
    assert agent._last_kb_best_score is None
    assert agent._last_kb_query_intent is False


# ============================================================================
# #2 回归：init_db 迁移幂等（防修复被回退）
# ============================================================================
def test_init_db_idempotent():
    """对同一 store 多次调用 init_db() 不得抛异常（PRAGMA 跳过已存在列）。"""
    tmp = tempfile.mkdtemp(prefix="initdb_")
    db_path = os.path.join(tmp, "idempotent.db")
    store = SQLiteStore(db_path=db_path)   # 构造时即触发第一次 init_db（建表）
    store.init_db()                         # 第二次：全新库已含新列，应被跳过
    store.init_db()                         # 第三次：同样幂等


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
