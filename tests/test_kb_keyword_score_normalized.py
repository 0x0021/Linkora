"""全文检索（keyword 兜底）score 归一化回归测试。

背景：search_kb_by_keyword 原始评分是关键词命中计数（content+2/title+1），
无上界整数（3、5、8...），顺着 kb_search → citations → 引文页脚会渲染成
「相关度300%」等异常值。根源修复：repo 层按满分（3×关键词数）归一化到
[0,1]；工具层 _search_by_fulltext 再钳位兜底。
"""

import pytest

from src.memory.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_path):
    s = SQLiteStore(db_path=str(tmp_path / "kb_norm.db"))
    s.init_db()
    yield s
    s.close()


def _seed(store, title, chunk):
    did = store._kb_repo.add_kb_document(
        title=title, doc_type="doc", source="test", source_id=title)
    store._kb_repo.add_kb_chunks(did, [chunk])
    return did


class TestKeywordScoreNormalized:

    def test_score_bounded_0_1(self, store):
        """多关键词多次命中也不会超过 1.0（原实现会返回 3、5 等计数）。"""
        # 标题+内容都密集含关键词 → 原实现会累出大整数分
        _seed(store, "钉钉审批流程手册", "钉钉审批流程：钉钉上发起审批，审批通过后钉钉通知。")
        results = store._kb_repo.search_kb_by_keyword("钉钉审批流程", top_k=5)
        assert results, "应命中文档"
        for r in results:
            assert 0.0 <= r["score"] <= 1.0, f"score 越界: {r['score']}"

    def test_full_hit_close_to_1(self, store):
        """标题+内容全命中所有关键词 → 得满分 1.0。"""
        _seed(store, "报销流程", "报销流程说明")
        results = store._kb_repo.search_kb_by_keyword("报销流程", top_k=5)
        assert results
        assert results[0]["score"] == 1.0

    def test_ordering_preserved(self, store):
        """归一化是保序缩放：命中多的文档仍排在前面。"""
        _seed(store, "VPN配置指南", "VPN配置步骤：先安装VPN客户端，再配置VPN服务器地址。")
        _seed(store, "无关文档", "这里只提到一次VPN。")
        results = store._kb_repo.search_kb_by_keyword("VPN配置", top_k=5)
        assert len(results) == 2
        assert results[0]["title"] == "VPN配置指南"
        assert results[0]["score"] > results[1]["score"]

    def test_partial_hit_below_1(self, store):
        """仅部分关键词命中 → 分数在 (0,1) 开区间。"""
        _seed(store, "考勤制度", "考勤打卡说明")
        results = store._kb_repo.search_kb_by_keyword("考勤报销流程", top_k=5)
        assert results
        assert 0.0 < results[0]["score"] < 1.0


class TestToolLayerClamp:
    """工具层 _search_by_fulltext 的钳位兜底（防未来回归）。"""

    def test_clamps_legacy_unbounded_score(self):
        from unittest.mock import MagicMock
        from src.tools.kb_search import KBSearchTool

        st = MagicMock()
        st._kb_repo.search_kb_by_keyword.return_value = [
            {"content": "c", "title": "t", "doc_type": "", "score": 5, "chunk_id": "x"},
            {"content": "c2", "title": "t2", "doc_type": "", "score": -0.2, "chunk_id": "y"},
            {"content": "c3", "title": "t3", "doc_type": "", "score": None, "chunk_id": "z"},
        ]
        tool = KBSearchTool(st, {"enabled": False})
        out = tool._search_by_fulltext("q", 5)
        scores = [r["score"] for r in out]
        assert scores == [1.0, 0.0, 0.0]
