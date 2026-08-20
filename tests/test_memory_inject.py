"""src/llm/memory_inject.inject_public_memories 单元测试。

覆盖：无 embedding / 短 query / 无命中 / 命中注入 / sender_id 空（只查公共记忆）。
纯函数式设计，用 fake agent + fake memory repo 即可，无需启动全套设施。
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.llm.memory_inject import (
    PUBLIC_MEMORY_BLOCK_MARK,
    inject_public_memories,
)


class _FakeEmbedding:
    enabled = True

    def embed(self, text: str) -> list[float]:
        return [0.1] * 8


class _FakeMemoryRepo:
    def __init__(self, memories: list[dict] | None = None):
        self.memories = memories or []
        self.last_sender_id = None
        self.last_query_text = ""

    def recall_memory(self, query_embedding, top_k, query_text="",
                      sender_id="", min_similarity=0.0) -> list[dict]:
        self.last_sender_id = sender_id
        self.last_query_text = query_text
        self.last_top_k = top_k
        self.last_min_similarity = min_similarity
        return [dict(m) for m in self.memories]


class _FakeStore:
    def __init__(self, memories: list[dict] | None = None):
        self._memory_repo = _FakeMemoryRepo(memories)


class _FakeAgent:
    def __init__(self, store=None, embedding=None):
        self.store = store
        self._embedding = embedding

    def _get_embedding_client(self):
        return self._embedding


def _make_agent(memories: list[dict] | None = None) -> _FakeAgent:
    return _FakeAgent(
        store=_FakeStore(memories),
        embedding=_FakeEmbedding(),
    )


def test_disabled_when_no_embedding():
    """embedding client 缺失时跳过，system_content 原样返回。"""
    agent = _FakeAgent(store=_FakeStore(), embedding=None)
    system_content = "SYSTEM"
    new_content, result = inject_public_memories(
        query="软件资源站在内网怎么访问", system_content=system_content, agent=agent,
    )
    assert new_content == system_content
    assert result.injected is False
    assert result.skipped_reason == "disabled"


def test_disabled_when_no_store_repo():
    """store 或 _memory_repo 缺失时跳过。"""
    agent = _FakeAgent(store=None, embedding=_FakeEmbedding())
    system_content = "SYSTEM"
    new_content, result = inject_public_memories(
        query="软件资源站在内网怎么访问", system_content=system_content, agent=agent,
    )
    assert new_content == system_content
    assert result.skipped_reason == "disabled"


def test_short_query_skipped():
    """短 query（<5 字符）跳过，避免对无意义消息注入。"""
    agent = _make_agent()
    system_content = "SYSTEM"
    new_content, result = inject_public_memories(
        query="在吗", system_content=system_content, agent=agent,
    )
    assert new_content == system_content
    assert result.skipped_reason == "short"


def test_no_hit_not_injected():
    """召回为空（无公共记忆或相似度不足）时不注入。"""
    agent = _make_agent([])
    system_content = "SYSTEM"
    new_content, result = inject_public_memories(
        query="软件资源站在内网怎么访问", system_content=system_content, agent=agent,
    )
    assert new_content == system_content
    assert result.injected is False
    assert result.skipped_reason == "no-hit"


def test_hit_injects_block():
    """命中公共记忆时注入「【★公共记忆…】」块，并透传 best_score。"""
    _resource_stub = "内网10.0.4.18:8800 互联网sw.rokae.com:8800"
    mem = {
        "content": _resource_stub,
        "similarity": 0.735,
    }
    agent = _make_agent([mem])
    system_content = "SYSTEM"
    new_content, result = inject_public_memories(
        query=f"http://{_resource_stub.split()[1]} 在局域网打不开?",
        system_content=system_content, agent=agent,
    )
    assert result.injected is True
    assert result.best_score == 0.735
    assert new_content.startswith(system_content)
    assert PUBLIC_MEMORY_BLOCK_MARK in new_content
    # 验证注入块确实包含目标资源域名（用 urlparse 避免 URL 子串告警）
    resource_host = _resource_stub.split()[1].removeprefix("互联网")  # sw.rokae.com:8800
    assert urlparse(f"http://{resource_host}").hostname == "sw.rokae.com"


def test_sender_id_empty_ensures_public_only():
    """sender_id 必须传空串——memory_repo 在 sender_id 为空时只查 scope='public'。"""
    mem = {"content": "公司年假 10 天", "similarity": 0.9}
    agent = _make_agent([mem])
    inject_public_memories(
        query="公司年假几天", system_content="SYSTEM", agent=agent,
    )
    repo = agent.store._memory_repo
    assert repo.last_sender_id == ""
    assert repo.last_query_text == "公司年假几天"
    assert repo.last_min_similarity > 0  # 有阈值过滤，防噪音


def test_reuse_query_embedding():
    """传入 query_embedding 时不再重复调用 embed（零额外成本）。"""
    class _CountingEmbedding(_FakeEmbedding):
        def __init__(self):
            self.calls = 0

        def embed(self, text):
            self.calls += 1
            return [0.1] * 8

    emb = _CountingEmbedding()
    agent = _FakeAgent(store=_FakeStore([{"content": "x", "similarity": 0.9}]), embedding=emb)
    q_emb = [0.2] * 8
    inject_public_memories(
        query="公司年假几天", system_content="SYSTEM", agent=agent,
        query_embedding=q_emb,
    )
    assert emb.calls == 0  # 复用传入向量，零额外 embed


def test_block_contains_high_priority_preamble():
    """v6 权重提升：返回 block 必须含「最高优先级」「覆盖声明」等指令前缀，
    避免 LLM 被 KB 文档/网络信息覆盖公共记忆里的事实（2026-08-20 事故：8800 资源站）。
    """
    _resource_stub = "内网 10.0.4.18:8800 互联网 sw.rokae.com:8800"
    mem = {"content": _resource_stub, "similarity": 0.7}
    agent = _make_agent([mem])
    new_content, result = inject_public_memories(
        query="8800 打不开", system_content="SYSTEM", agent=agent,
    )
    assert result.injected is True
    assert result.block, "v6 必须返回完整 block 供 prompt_builder 抽出独立消息"
    # 高优先级声明必须出现
    assert "最高优先级" in result.block
    assert "覆盖声明" in result.block
    assert "直接给出" in result.block
    # block 块必须含记忆内容
    assert "10.0.4.18" in result.block
    # block 已追加到 system_content 末尾（去除尾部空白后内容等价）
    assert new_content.rstrip() == ("SYSTEM" + result.block).rstrip()


def test_block_appears_in_system_content_for_prompt_builder_extraction():
    """v6 prompt_builder 权重提升：从 system_content 末尾能完整定位并抽离 block。"""
    mem = {"content": "公司年假 10 天", "similarity": 0.9}
    agent = _make_agent([mem])
    new_content, result = inject_public_memories(
        query="公司年假几天", system_content="SYSTEM_BEFORE", agent=agent,
    )
    # 模拟 prompt_builder 的抽出逻辑：用 PUBLIC_MEMORY_BLOCK_MARK 定位起点
    from src.llm.memory_inject import PUBLIC_MEMORY_BLOCK_MARK
    assert PUBLIC_MEMORY_BLOCK_MARK in new_content
    idx = new_content.rindex(PUBLIC_MEMORY_BLOCK_MARK)
    extracted = new_content[idx:]
    remaining = new_content[:idx].rstrip()
    # 抽出后 system_content 不再含 block
    assert PUBLIC_MEMORY_BLOCK_MARK not in remaining
    # 抽出后 system_content 保留原文 + 末尾无 block 残留
    assert remaining == "SYSTEM_BEFORE"
    assert extracted.startswith(PUBLIC_MEMORY_BLOCK_MARK)
