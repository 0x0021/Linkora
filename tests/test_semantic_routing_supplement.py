"""语义路由补充测试：warmup_tools、match_tools 完整流程、score_skill。"""
from __future__ import annotations


from src import semantic


class DummyEmbeddingClient:
    """模拟 EmbeddingClient：返回简单向量。"""
    enabled = True

    def embed(self, text: str) -> list[float]:
        # 用 hash 生成固定维度的向量，确保相同输入得相同输出
        h = abs(hash(text))
        return [(h % 1000) / 1000.0 for _ in range(4)]


def test_warmup_tools_with_client():
    """有 embedding 客户端时，warmup_tools 预计算向量。"""
    client = DummyEmbeddingClient()
    semantic.set_embedding_client(client)
    texts = [("tool_a", "搜索工具描述"), ("tool_b", "分析工具描述")]
    try:
        semantic.warmup_tools(texts)
        # 缓存中应有数据
        assert "tool_a" in semantic._tool_cache
        assert "tool_b" in semantic._tool_cache
    finally:
        semantic.invalidate_all()

def test_warmup_tools_no_client():
    """无客户端时 warmup_tools 静默忽略。"""
    semantic.set_embedding_client(None)
    texts = [("tool_x", "whatever")]
    semantic.warmup_tools(texts)
    # 无异常即可


def test_match_tools_full_flow():
    """完整语义匹配流程：消息向量 → 比对工具向量 → 返回 >= threshold 的。"""
    client = DummyEmbeddingClient()
    semantic.set_embedding_client(client)
    msg_vec = client.embed("搜索资料")
    tools = [
        ("search_tool", "搜索 查找 检索资料"),
        ("unrelated", "不要匹配这个工具的无意义描述"),
    ]
    result = semantic.match_tools(msg_vec, tools, threshold=0.0)
    assert len(result) >= 1  # 至少能匹配上 search_tool
    semantic.invalidate_all()


def test_match_tools_no_client():
    """无 embedding 客户端时返回空 dict。"""
    semantic.set_embedding_client(None)
    result = semantic.match_tools([0.1, 0.2, 0.3, 0.4], [("t", "desc")])
    assert result == {}


def test_score_skill_with_client():
    """有客户端时 score_skill 返回相似度。"""
    client = DummyEmbeddingClient()
    semantic.set_embedding_client(client)
    msg_vec = client.embed("天气预报")
    score = semantic.score_skill(msg_vec, "weather_skill", "天气 预报 气象")
    assert score is not None
    assert 0.0 <= score <= 1.0
    semantic.invalidate_all()


def test_score_skill_no_client():
    """无客户端时 score_skill 返回 None。"""
    semantic.set_embedding_client(None)
    score = semantic.score_skill([0.1], "skill", "desc")
    assert score is None


def test_cosine_zero_vector():
    """任一向量为零时 cosine 返回 0.0。"""
    assert semantic.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert semantic.cosine([1.0, 1.0], [0.0, 0.0]) == 0.0


def test_cosine_none_input():
    assert semantic.cosine(None, [1.0, 2.0]) == 0.0
    assert semantic.cosine([1.0, 2.0], None) == 0.0


def test_cosine_empty_list():
    assert semantic.cosine([], [1.0, 2.0]) == 0.0


def test_invalidate_tools_and_skills():
    client = DummyEmbeddingClient()
    semantic.set_embedding_client(client)
    semantic.warmup_tools([("t1", "text")])
    assert len(semantic._tool_cache) > 0

    semantic.invalidate_tools()
    assert len(semantic._tool_cache) == 0

    semantic.warmup_tools([("t2", "text")])
    semantic.invalidate_skills()
    # tools 不受 skills invalidate 影响
    assert len(semantic._tool_cache) > 0

    semantic.invalidate_all()
    assert len(semantic._tool_cache) == 0
    assert len(semantic._skill_cache) == 0


def test_get_embedding_client():
    """get/set 客户端基本操作。"""
    client = DummyEmbeddingClient()
    semantic.set_embedding_client(client)
    assert semantic.get_embedding_client() is client
    semantic.set_embedding_client(None)
    assert semantic.get_embedding_client() is None


def test_match_tools_message_vec_none():
    """消息向量为 None 时直接返回 {}。"""
    client = DummyEmbeddingClient()
    semantic.set_embedding_client(client)
    result = semantic.match_tools(None, [("t", "desc")])
    assert result == {}
