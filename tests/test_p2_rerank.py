"""Phase 2 · P2-6：BGE 本地离线重排 测试。

覆盖：
- rerank 模块直接单测（mock CrossEncoder，不联网下载）：
  - reorder-only：仅调整顺序，原 score 字段不变
  - top_k 截断与候选窗口拼接
  - 空输入 / reranker 加载失败 → 返回原始顺序
  - 重排预测异常 → 降级原始顺序
- style.retrieve_relevant_knowledge 接线：
  - 关闭（默认）→ 不重排，顺序/score 不变
  - 开启且模型可用 → 顺序被重排，原相似度 score 保留
  - 重排异常 → 沿用原始顺序（不阻断主链路）
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import src.llm.rerank as rerank_mod
from src.llm import style


@pytest.fixture(autouse=True)
def _reset_rerank_cache():
    """每个用例前清空模块级 lazy 缓存，防止用例间串扰。"""
    rerank_mod._clear_cache()
    yield
    rerank_mod._clear_cache()


# ----------------------------- 测试夹具 -----------------------------

class _FakeKbTool:
    def __init__(self, results):
        self._results = results

    def search(self, **kwargs):
        return {"success": True, "results": self._results}


class _FakeAgent:
    """驱动 style.retrieve_relevant_knowledge 的最小 agent。"""

    _rag_min_similarity = 0.5
    _rag_max_results = 5
    _rag_max_content_chars = 800

    def __init__(self, results, rerank_enabled=False):
        self.tool_router = SimpleNamespace(_tools={"kb_search": _FakeKbTool(results)})
        self._rerank_enabled = rerank_enabled
        self._rerank_model = "BAAI/bge-reranker-base"
        self._rerank_offline = False
        self._rerank_top_k = 10
        self._rerank_timeout = 2.0


class _FakeReranker:
    """mock CrossEncoder：predict 返回外部注入的打分序列。"""

    def __init__(self, scores):
        self._scores = scores

    def predict(self, pairs, show_progress_bar=False):
        assert len(pairs) == len(self._scores), "pairs/scores 长度不一致"
        return list(self._scores)


# --------------------- rerank 模块直接单测 ---------------------

def test_rerank_empty_returns_empty():
    assert rerank_mod.rerank("q", []) == []


def test_rerank_reorder_only_keeps_scores():
    """reorder-only：顺序按重排分降序，但原 content/score 字段完整保留。"""
    results = [
        {"content": "A 内容", "source": "docA", "score": 0.9},
        {"content": "B 内容", "source": "docB", "score": 0.6},
        {"content": "C 内容", "source": "docC", "score": 0.8},
    ]
    # 重排打分：让 C(0.9) > A(0.1) > B(0.05) → 期望顺序 [C, A, B]
    fake = _FakeReranker([0.1, 0.05, 0.9])
    with patch.object(rerank_mod, "get_reranker", return_value=fake):
        out = rerank_mod.rerank("query", results)
    assert [r["source"] for r in out] == ["docC", "docA", "docB"]
    # 原相似度 score 必须保留（用于阈值/引文页脚语义一致）
    assert [r["score"] for r in out] == [0.8, 0.9, 0.6]
    assert [r["content"] for r in out] == ["C 内容", "A 内容", "B 内容"]


def test_rerank_top_k_truncates_and_concat():
    """top_k < 总数：候选窗口重排后截断，尾部未参与重排者原序拼接。"""
    results = [
        {"source": "d0", "score": 0.1, "content": "c0"},
        {"source": "d1", "score": 0.2, "content": "c1"},
        {"source": "d2", "score": 0.3, "content": "c2"},
        {"source": "d3", "score": 0.4, "content": "c3"},
    ]
    # 候选窗口 top_k=2 → 仅 d0,d1 参与重排；重排打分 [0.5, 0.2] 顺序不变
    fake = _FakeReranker([0.5, 0.2])
    with patch.object(rerank_mod, "get_reranker", return_value=fake):
        out = rerank_mod.rerank("q", results, top_k=2)
    assert [r["source"] for r in out] == ["d0", "d1", "d2", "d3"]


def test_rerank_load_failure_returns_original():
    """reranker 加载失败（返回 None）→ 原始顺序。"""
    results = [
        {"source": "d0", "score": 0.1, "content": "c0"},
        {"source": "d1", "score": 0.9, "content": "c1"},
    ]
    with patch.object(rerank_mod, "get_reranker", return_value=None):
        out = rerank_mod.rerank("q", results)
    assert [r["source"] for r in out] == ["d0", "d1"]


def test_rerank_predict_exception_degrades():
    """reranker.predict 抛异常 → 降级原始顺序，不向上抛。"""
    results = [
        {"source": "d0", "score": 0.1, "content": "c0"},
        {"source": "d1", "score": 0.9, "content": "c1"},
    ]
    fake = MagicMock()
    fake.predict.side_effect = RuntimeError("boom")
    with patch.object(rerank_mod, "get_reranker", return_value=fake):
        out = rerank_mod.rerank("q", results)
    assert [r["source"] for r in out] == ["d0", "d1"]


def test_rerank_get_reranker_loads_and_caches():
    """get_reranker 成功加载后缓存，重复调用不复建。"""
    fake_cls = MagicMock(return_value=_FakeReranker([0.1]))
    with patch("sentence_transformers.CrossEncoder", fake_cls):
        r1 = rerank_mod.get_reranker("fake/model", offline=False)
        r2 = rerank_mod.get_reranker("fake/model", offline=False)
        assert r1 is r2  # 缓存复用
        assert fake_cls.call_count == 1  # 仅加载一次


def test_rerank_get_reranker_failure_returns_none():
    """get_reranker 在 load 抛异常时返回 None（不抛）。"""
    fake_cls = MagicMock(side_effect=ImportError("no torch"))
    with patch("sentence_transformers.CrossEncoder", fake_cls):
        assert rerank_mod.get_reranker("fake/model") is None


# --------------------- style 接线单测 ---------------------

def _sample_results():
    return [
        {"content": "VPN 配置步骤：第一步...", "source": "VPN手册", "score": 0.9, "id": 7},
        {"content": "补充说明文档内容", "source": "补充文档", "score": 0.6, "id": 8},
        {"content": "无关背景知识", "source": "背景文档", "score": 0.55, "id": 9},
    ]


def test_style_rerank_disabled_keeps_order():
    """默认关闭：结果顺序与原始一致，best_score 不变（零行为变更）。"""
    agent = _FakeAgent(_sample_results(), rerank_enabled=False)
    text, best = style.retrieve_relevant_knowledge(agent, "VPN 配置")
    assert best == 0.9
    # _MAX_DISPLAY 读配置（上限 4）：保留通过展示阈值的所有来源，顺序仍为原始最优
    cites = agent._last_kb_citations_raw
    assert len(cites) >= 1  # 不再硬编码 1 条
    assert cites[0].source == "VPN手册"
    assert "【相关知识】" in text


def test_style_rerank_enabled_reorders_preserves_score():
    """开启且模型可用：顺序重排，但原相似度 score 保留。"""
    # 让「背景文档」重排分最高排到最前（原本相似度最低）
    agent = _FakeAgent(_sample_results(), rerank_enabled=True)
    fake = _FakeReranker([0.1, 0.2, 0.95])
    with patch.object(rerank_mod, "get_reranker", return_value=fake):
        text, best = style.retrieve_relevant_knowledge(agent, "VPN 配置")
    cites = agent._last_kb_citations_raw
    # 顺序被重排：背景文档（原 score 0.55）排到第一
    assert cites[0].source == "背景文档"
    # 但 best_score 仍取原相似度最大值（0.9），语义不变
    assert best == 0.9
    # 重排后注入文本含被重排到前面的文档
    assert "背景文档" in text


def test_style_rerank_exception_degrades_to_original():
    """重排异常：沿用原始顺序，不阻断返回。"""
    agent = _FakeAgent(_sample_results(), rerank_enabled=True)
    fake = MagicMock()
    fake.predict.side_effect = RuntimeError("boom")
    with patch.object(rerank_mod, "get_reranker", return_value=fake):
        text, best = style.retrieve_relevant_knowledge(agent, "VPN 配置")
    # 异常降级：顺序保持原始，best 仍是 0.9
    assert best == 0.9
    assert agent._last_kb_citations_raw[0].source == "VPN手册"
    assert "【相关知识】" in text
