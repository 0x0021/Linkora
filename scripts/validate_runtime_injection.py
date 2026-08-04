#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实战闭环验证：agent 运行时是否真正把 画像(persona) + few-shot + RAG 三者
注入到送给 LLM 的最终 system prompt。

设计（不依赖任何真实凭据 / 网络）：
- 用确定性「关键词 embedding」FakeEmb 替代真实向量模型（与 test_rag_gating.py
  的 MockEmbeddingClient 思路一致），离线可跑、可复现。
- 真实 SQLiteStore（临时 DB）：种入主人沟通风格画像 + 一条 KB 文档分块，
  分块向量 = 查询文本的向量，保证 faiss 检索命中（相似度=1.0）。
- 真实 KBSearchTool：绑定上述 store 与 FakeEmb。
- 构造真实 LLMAgent，调用运行时真正使用的 _build_user_message（LLM 调用前的
  最终边界），捕获 messages[0]（system prompt）。
- 断言 system prompt 同时含：
    · 【主人沟通风格】        —— 画像注入
    · 【本人语气样例】        —— few-shot 注入
    · 【相关知识（自动检索）】 —— RAG 注入
- 同时校验 RAG 命中状态透传（_last_kb_hit / _last_kb_best_score）。

运行：.venv/bin/python scripts/validate_runtime_injection.py
"""
from __future__ import annotations

import math
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace

# ---- 让脚本可从项目根目录运行 ----
import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.memory.sqlite_store import SQLiteStore          # noqa: E402
from src.tools.kb_search import KBSearchTool             # noqa: E402
from src.llm.agent import LLMAgent                       # noqa: E402
from src.models import Message                           # noqa: E402


# ============================================================================
# 1) 确定性离线 embedding（关键词特征向量）
# ============================================================================
class FakeEmb:
    """与真实 EmbeddingClient 接口兼容的确定性 embedding。

    - enabled=True（让语义路径可走）
    - embed(text) 返回固定维度向量：含 how-to 关键词 → 靠近「知识锚点」；
      含闲聊关键词 → 靠近「闲聊锚点」。同文本 → 同向量（余弦=1.0）。
    """

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
        casual = any(k in t for k in self._CASUAL)
        v = [0.0] * self.DIM
        v[0] = 1.0 if howto else 0.0
        v[1] = 1.0 if casual else 0.0
        v[2] = min(len(t) / 100.0, 1.0)
        v[3] = 1.0 if ("vpn" in t or "配置" in t) else 0.0
        v[4] = (hash(t) % 1000) / 1000.0   # 微小扰动，保证同文本同向量
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


# ============================================================================
# 2) 配置（最小可用，仅含运行时注入路径需要的字段）
# ============================================================================
def build_config():
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
    cfg = SimpleNamespace(
        system_prompt="你是 {user_name} 的数字分身，平台 {platform}。",
        advanced=advanced,
        few_shot_examples=None,        # 改由 Agent 构造参数传入（平台级样例路径）
        persona_style_prompt="",       # 留空 → 走 store 自动画像
        persona_style_prompts={},
    )
    return cfg


# ============================================================================
# 3) 主验证流程
# ============================================================================
def main() -> int:
    tmp = tempfile.mkdtemp(prefix="agent_cl闭环_")
    db_path = os.path.join(tmp, "validate.db")

    emb = FakeEmb(enabled=True)
    QUERY = "VPN怎么配置"
    PERSONA = ("主人沟通风格：干脆直接、不绕弯子；技术答复先给解法再补备选；"
               "闲聊一两句收住，绝不列编号列表。")
    FEWSHOT = [
        {"user": "打印机连不上咋办", "assistant": "先看网线，再重启打印机，还不行重装驱动。"},
        {"user": "这次团建去哪", "assistant": "听安排的，到地儿干活就行。"},
    ]
    KB_TITLE = "VPN 配置规范"
    KB_CONTENT = ("VPN 配置步骤：1) 打开设置-网络-VPN；2) 填入网关地址与账号；"
                  "3) 选择 L2TP 协议并保存；4) 连接后验证内网可达。")

    print("=" * 72)
    print("实战闭环验证：画像 + few-shot + RAG 注入最终 system prompt")
    print("=" * 72)

    # --- 3.1 真实 store：种画像 + 种 KB ---
    store = SQLiteStore(db_path=db_path)
    store._memory_ops_repo.save_style_profile({"prompt": PERSONA, "confidence": "high"}, "auto")

    doc_id = store._kb_repo.add_kb_document(title=KB_TITLE, doc_type="doc", source="闭环验证")
    store._kb_repo.add_kb_chunks(doc_id, [KB_CONTENT])
    chunk_id = store._kb_repo.list_kb_chunks(doc_id)[0]["id"]
    # 关键：把查询文本向量作为该分块向量，保证 faiss 检索相似度=1.0
    store._kb_repo.update_chunk_embedding(chunk_id, emb.embed(QUERY))

    # --- 3.2 真实 KBSearchTool，绑定 FakeEmb ---
    kb_tool = KBSearchTool(store, {"enabled": True})
    kb_tool.embedding_client = emb   # 复用查询向量，跳过模型

    tool_router = SimpleNamespace(_tools={"kb_search": kb_tool})

    # --- 3.3 构造真实 Agent ---
    agent = LLMAgent(
        config=build_config(),
        client=SimpleNamespace(),                 # 本验证不触发 LLM 调用
        tool_router=tool_router,
        user_name="OWNER",
        user_dept="研发中心",
        org_name="某科技公司",
        store=store,
        platform_id="dingtalk",
        few_shot_examples=FEWSHOT,
    )

    # --- 3.4 跑运行时最终边界：_build_user_message（LLM 调用前） ---
    # 真实注入块标记 vs base prompt 里的“规则说明文字”要区分开：
    #   注入块: 【相关知识（自动检索，仅供参考）】
    #   说明文字: …若出现【相关知识（自动检索）】区块… （误报源）
    RAG_INJECT_MARKER = "【相关知识（自动检索，仅供参考）】"

    def run_scenario(content: str, expect_rag: bool, label: str) -> tuple[bool, list[str]]:
        # 每轮前重置 RAG 遥测状态（production 中该状态仅在检索时更新，
        # 短消息/闲聊跳过检索会残留上一轮值——此处重置以隔离本场景）
        agent._last_kb_hit = False
        agent._last_kb_best_score = None
        m = Message(
            msg_id="m_" + label, chat_id="c1", chat_type="single", chat_name=None,
            sender_id="u_zhang", sender_name="张三", content=content,
            msg_type="text", timestamp=datetime.now(),
        )
        sc = agent._build_user_message(m, history=[])[0]["content"]
        persona_hit = "【主人沟通风格" in sc
        fewshot_hit = "【本人语气样例" in sc
        rag_hit = RAG_INJECT_MARKER in sc
        rag_state = bool(getattr(agent, "_last_kb_hit", False))
        rag_score = getattr(agent, "_last_kb_best_score", None)
        # 闲聊：画像/few-shot 必在；RAG 应被意图门控拦截（expect_rag=False）
        expected = [
            ("画像(persona)注入", persona_hit, True),
            ("few-shot 注入", fewshot_hit, True),
            ("RAG 知识注入", rag_hit, expect_rag),
            ("RAG 命中状态透传(_last_kb_hit)", rag_state, expect_rag),
        ]
        print(f"\n场景[{label}] 查询={content!r}  RAG相似度={rag_score}")
        print("-" * 72)
        fails = []
        for name, ok, want in expected:
            status = "PASS" if (ok == want) else "FAIL"
            if status == "FAIL":
                fails.append(name)
            print(f"  [{status}] {name}  (期望={'注入' if want else '不注入'}, 实际={'注入' if ok else '未注入'})")
        # 打印实际注入片段
        if persona_hit:
            seg = sc.split("【主人沟通风格", 1)[1].split("【", 1)[0]
            print("  · 画像片段:", seg.strip()[:80], "…")
        if fewshot_hit:
            seg = sc.split("【本人语气样例", 1)[1].split("【", 1)[0]
            print("  · few-shot片段:", seg.strip()[:100], "…")
        if rag_hit:
            seg = sc.split("【相关知识", 1)[1].split("】", 1)[1]
            print("  · RAG片段: …《%s》…" % KB_TITLE, seg.strip()[:90], "…")
        return (len(fails) == 0), fails

    all_pass = True
    ok1, f1 = run_scenario(QUERY, expect_rag=True, label="文档查询")
    ok2, f2 = run_scenario("今天天气怎么样呀", expect_rag=False, label="闲聊(门控应拦截RAG)")
    all_pass = ok1 and ok2

    print("\n" + "=" * 72)
    if all_pass:
        print("结论: ✅ 画像+few-shot 始终注入；RAG 仅在文档意图命中时注入（门控双向正确）")
    else:
        print("结论: ❌ 未通过项:", f1 + f2)
    print("=" * 72)
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
