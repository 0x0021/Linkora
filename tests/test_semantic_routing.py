"""Phase 2 语义路由：复用本地 embedding 做语义相似度兜底。

用确定性 fake embedding（字符哈希向量，无需加载模型）验证：
- match_tools：语义相似度 >= 阈值命中相关工具，相关度低的排除；
- score_skill：技能评分 = max(关键词命中, 语义相似度×weight)；
- 降级：embedding 不可用（enabled=False）时语义层退场，行为回退纯子串匹配。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src import semantic as semantic_index
from src.skills.manager import SkillManager


def _patch_skill_dirs(monkeypatch, root: str):
    """临时覆盖模块级 _SKILL_DIRS 仅指向测试目录。"""
    import src.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "_SKILL_DIRS", [root + "/data/skills"])
from src.skills.router import SkillRouter
from src.config import SkillsConfig, ToolsConfig


class FakeEmbedding:
    """确定性字符哈希向量器：共享字符越多余弦相似度越高，无需真实模型。"""

    def __init__(self, enabled: bool = True, dim: int = 128):
        self.enabled = enabled
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        if not self.enabled:
            return []
        vec = [0.0] * self.dim
        for ch in str(text):
            # 仅对 CJK 字符哈希，忽略 ASCII/标点，使相似度更贴近语义重叠
            if not ("\u4e00" <= ch <= "\u9fff"):
                continue
            idx = (hash(ch) % self.dim + self.dim) % self.dim
            vec[idx] += 1.0
        norm = (sum(v * v for v in vec) ** 0.5) or 1.0
        return [v / norm for v in vec]


@pytest.fixture
def fake_client():
    client = FakeEmbedding(enabled=True)
    semantic_index.set_embedding_client(client)
    yield client
    # 清理，避免污染其他测试模块
    semantic_index.set_embedding_client(None)
    semantic_index.invalidate_all()


def test_match_tools_semantic_hit_and_exclude(fake_client):
    """语义相似度 >= 阈值的工具命中，相关度低的排除。"""
    tools = [
        ("get_weather", "get_weather 天气查询 气温 下雨 带伞 温度"),
        ("get_stock", "get_stock 股票 行情 股价 大盘 涨停"),
    ]
    msg_vec = fake_client.embed("今天天气下雨带伞")
    hits = semantic_index.match_tools(msg_vec, tools, threshold=0.42)
    assert "get_weather" in hits
    assert "get_stock" not in hits


def test_match_tools_disabled_embedding_returns_empty(fake_client):
    """embedding 不可用时 match_tools 返回空（上层回退子串匹配）。"""
    fake_client.enabled = False
    tools = [("get_weather", "get_weather 天气查询 气温 下雨")]
    msg_vec = fake_client.embed("今天天气怎么样")
    assert semantic_index.match_tools(msg_vec, tools) == {}


def test_score_skill_semantic_vs_keyword(fake_client):
    """score_skill 返回语义相似度；embedding 禁用时返回 None（回退关键词）。"""
    sim = semantic_index.score_skill(
        fake_client.embed("行程安排规划"),
        "planner",
        "planner 行程规划与日程安排助手",
    )
    assert isinstance(sim, float) and 0.0 <= sim <= 1.0
    # 禁用后返回 None
    fake_client.enabled = False
    assert semantic_index.score_skill(
        fake_client.embed("x"), "planner", "planner 行程规划") is None


def _write_skill(root, name, frontmatter, body="# Body\n"):
    d = root / "data" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return d


def test_skill_router_semantic_only_activation(fake_client, monkeypatch):
    """语义相近但无关键词子串命中时，技能仍能经语义激活（覆盖口语/同义改写）。"""
    with tempfile.TemporaryDirectory() as td:
        _patch_skill_dirs(monkeypatch, td)
        # intent_keywords 与消息刻意不重叠（无子串命中），语义文本相近
        fm = (
            "name: planner\n"
            "intent_keywords:\n- 排期表\n- itinerary\n"
            "weight: 1.0\n"
            "description: 行程规划与日程安排助手\n"
        )
        _write_skill(Path(td), "planner", fm)
        mgr = SkillManager(td)
        mgr.reload()
        router = SkillRouter(mgr, skills_config=SkillsConfig(semantic_routing=True))

        msg = "帮我规划一下这周的行程安排"
        msg_vec = fake_client.embed(msg)
        # 短语无关键词子串命中（排期表/itinerary 均不在消息中）
        name, _ = router.route(msg, query_embedding=msg_vec)
        assert name == "planner"


def test_skill_router_fallback_when_embedding_disabled(fake_client, monkeypatch):
    """embedding 禁用时，语义层退场，评分回退为纯关键词（与 Phase 1 行为一致）。"""
    with tempfile.TemporaryDirectory() as td:
        # 锁定技能发现目录到临时目录，隔离真实仓库 data/skills（否则 CI 上发现为空）
        _patch_skill_dirs(monkeypatch, td)
        fm = (
            "name: weather\n"
            "intent_categories:\n- domain.weather\n"
            "weight: 1.0\n"
            "description: 天气查询\n"
        )
        _write_skill(Path(td), "weather", fm)
        mgr = SkillManager(td)
        mgr.reload()
        router = SkillRouter(mgr, skills_config=SkillsConfig(semantic_routing=True))

        # 禁用 embedding
        fake_client.enabled = False
        # 关键词命中（"天气"在消息中）→ 仍应激活
        name, _ = router.route("今天天气怎么样")
        assert name == "weather"


def test_agent_tool_semantic_fallback(monkeypatch, fake_client):
    """LLMAgent._keyword_match_tool_names 在子串未命中时用语义兜底补充工具。"""
    from src.llm.agent import LLMAgent
    from src.config import LlmConfig
    from src.tools.base import ToolRouter, BaseTool

    class _Weather(BaseTool):
        def __init__(self):
            self.name = "get_weather"
            self.intent_categories = ["domain.weather"]
            self.intent_keywords = []
            self.description = "天气查询"
            self.parameters = {"type": "object", "properties": {}}

        def execute(self, args):
            return "ok"

    cfg = ToolsConfig(available=["send_message", "get_weather"], tool_routing_mode="smart")
    router = ToolRouter(cfg)
    router.register(_Weather())
    router.register(type("_B", (BaseTool,), {
        "name": "send_message",
        "intent_keywords": [],
        "intent_categories": [],
        "description": "发送消息",
        "parameters": {"type": "object", "properties": {}},
        "execute": lambda self, args: "ok",
    })())
    agent = LLMAgent(config=LlmConfig(), client=None, tool_router=router)

    # agent 复用 kb_search 的 embedding 客户端；强制用 fake
    monkeypatch.setattr(agent, "_get_embedding_client", lambda: fake_client)

    msg = "外面是不是要下雨了今天出门带伞吗"  # 含 雨/带伞（关键词），同时验证语义通道也通
    msg_vec = fake_client.embed(msg)
    names = agent._keyword_match_tool_names(msg, query_embedding=msg_vec)
    assert "get_weather" in names


# ── 向量计算边缘情况 ────────────────────────────────────────

def test_cosine_empty_or_none_vectors():
    """余弦相似度：空向量/None 返回 0.0。"""
    assert semantic_index.cosine(None, [1.0]) == 0.0
    assert semantic_index.cosine([1.0], None) == 0.0
    assert semantic_index.cosine([], []) == 0.0


def test_cosine_zero_vector():
    """零向量（范数为零）返回 0.0，避免除零错误。"""
    assert semantic_index.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_exception_fallback(monkeypatch):
    """numpy 计算异常时安全返回 0.0。"""
    import numpy as np
    def _raise(*a, **kw):
        raise ValueError("mock error")
    monkeypatch.setattr(np, "dot", _raise)
    assert semantic_index.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_embed_client_none():
    """_embed() 客户端未绑定时返回 None。"""
    old = semantic_index._client
    semantic_index._client = None
    try:
        assert semantic_index._embed("测试文本") is None
    finally:
        semantic_index._client = old


def test_embed_client_disabled():
    """_embed() 客户端 enabled=False 时返回 None（不尝试编码）。"""
    old = semantic_index._client
    semantic_index._client = type("D", (), {"enabled": False})()
    try:
        assert semantic_index._embed("测试") is None
    finally:
        semantic_index._client = old


def test_embed_client_raises():
    """_embed() 编码抛异常时安全返回 None。"""
    old = semantic_index._client
    class BadClient:
        enabled = True
        def embed(self, text):
            raise RuntimeError("mock encoding failure")
    semantic_index._client = BadClient()
    try:
        assert semantic_index._embed("测试") is None
    finally:
        semantic_index._client = old


def test_match_tools_none_message_vec():
    """message_vec 为 None 时 match_tools 直接返回空。"""
    tools = [("get_weather", "天气 气温 查询")]
    assert semantic_index.match_tools(None, tools) == {}


def test_match_tools_client_none():
    """_client 为 None 时 match_tools 返回空。"""
    old = semantic_index._client
    semantic_index._client = None
    try:
        tools = [("get_weather", "天气 查询")]
        assert semantic_index.match_tools([1.0], tools) == {}
    finally:
        semantic_index._client = old


def test_match_tools_client_disabled():
    """_client enabled=False 时 match_tools 返回空。"""
    old = semantic_index._client
    semantic_index._client = type("D", (), {"enabled": False})()
    try:
        tools = [("get_weather", "天气 查询")]
        assert semantic_index.match_tools([1.0], tools) == {}
    finally:
        semantic_index._client = old


def test_score_skill_none_vec():
    """message_vec 为 None 时 score_skill 返回 None。"""
    assert semantic_index.score_skill(None, "weather", "天气查询") is None


def test_warmup_tools_client_none():
    """warmup_tools 客户端未绑定时直接返回（不抛异常）。"""
    old = semantic_index._client
    semantic_index._client = None
    try:
        semantic_index.warmup_tools([("t", "text")])
        assert len(semantic_index._tool_cache) == 0
    finally:
        semantic_index._client = old


def test_warmup_tools_client_disabled():
    """warmup_tools 客户端禁用时直接返回，不尝试编码。"""
    old = semantic_index._client
    semantic_index._client = type("D", (), {"enabled": False})()
    try:
        semantic_index.warmup_tools([("t", "text")])
        assert len(semantic_index._tool_cache) == 0
    finally:
        semantic_index._client = old


def test_invalidate_tools():
    """invalidate_tools 清空工具向量缓存。"""
    semantic_index._tool_cache["fake"] = ("sig", [1.0])
    semantic_index.invalidate_tools()
    assert len(semantic_index._tool_cache) == 0


def test_invalidate_skills():
    """invalidate_skills 清空技能向量缓存。"""
    semantic_index._skill_cache["fake"] = ("sig", [1.0])
    semantic_index.invalidate_skills()
    assert len(semantic_index._skill_cache) == 0


def test_invalidate_all():
    """invalidate_all 同时清空工具和技能缓存。"""
    semantic_index._tool_cache["a"] = ("s1", [1.0])
    semantic_index._skill_cache["b"] = ("s2", [2.0])
    semantic_index.invalidate_all()
    assert len(semantic_index._tool_cache) == 0
    assert len(semantic_index._skill_cache) == 0


def test_get_embedding_client():
    """get_embedding_client 返回已绑定的客户端。"""
    from src.semantic import get_embedding_client, set_embedding_client
    old = get_embedding_client()
    dummy = object()
    set_embedding_client(dummy)
    try:
        assert get_embedding_client() is dummy
    finally:
        set_embedding_client(old)
