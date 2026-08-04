"""知识库检索工具（kb_search）单元测试。

覆盖：
- top_k / min_similarity 参数健壮解析
- 意图关键词验证
- safe_float 单元
- _fallback_keywords
- embedding 初始化（enabled/disabled/失败）
- embedding 搜索路径（成功/空结果/异常）
- 全文搜索路径（成功/异常）
- health check
- 空查询拒绝
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from src.tools.kb_search import KBSearchTool, safe_float, safe_int


def _make_tool():
    store = MagicMock()
    store.conn.cursor.return_value.fetchall.return_value = []
    store._kb_repo.search_kb_by_keyword.return_value = []
    store._kb_repo.search_kb.return_value = []
    return KBSearchTool(store, {"enabled": False}), store


# ============================================================================
# 参数健壮性
# ============================================================================
class TestKBSearchParamSafety:
    def test_string_top_k_and_similarity_no_crash(self):
        tool, _ = _make_tool()
        r = tool.execute({"query": "打印机", "top_k": "5", "min_similarity": "0.3以上"})
        assert r.get("success") is True

    def test_chinese_number_and_none(self):
        tool, _ = _make_tool()
        r = tool.execute({"query": "VPN", "top_k": "五", "min_similarity": None})
        assert r.get("success") is True

    def test_empty_query_rejected(self):
        tool, _ = _make_tool()
        assert tool.execute({"query": "  "}).get("error")

    def test_top_k_clamped_upper(self):
        tool, store = _make_tool()
        assert max(1, min(safe_int("999", 3), 20)) == 20
        assert max(1, min(safe_int("-5", 3), 20)) == 1

    def test_search_with_keyword_args(self):
        """search 通过 kwargs 传参（程序化调用入口）。"""
        tool, _ = _make_tool()
        r = tool.search(query="VPN配置", top_k=5)
        assert r.get("success") is True

    def test_execute_search_exception(self):
        """execute 整体异常（非子方法内部拦截），fulltext 异常在子方法已 catch。"""
        tool, store = _make_tool()
        # _search_by_fulltext 内部 catch 后返回 []，最终 execute 返回 success:True
        store._kb_repo.search_kb_by_keyword.side_effect = RuntimeError("db crash")
        r = tool.execute({"query": "test"})
        assert r.get("success") is True
        assert r["search_method"] == "none"

    def test_execute_top_level_exception(self):
        """execute 顶层异常：绕过内层 catch，直接让 _search_by_fulltext 抛未捕获异常。"""
        tool, store = _make_tool()
        # 替换 _search_by_fulltext 为直接抛异常（绕过其内部 try/except）
        tool._search_by_fulltext = MagicMock(side_effect=RuntimeError("uncaught"))
        r = tool.execute({"query": "test"})
        assert "error" in r

    def test_fulltext_no_results(self):
        """全文检索无结果时返回 none search_method。"""
        tool, store = _make_tool()
        store._kb_repo.search_kb_by_keyword.return_value = []
        r = tool.execute({"query": "不存在的内容abcdefg"})
        assert r["search_method"] == "none"
        assert r["results"] == []


# ============================================================================
# Embedding 搜索路径
# ============================================================================
class TestEmbeddingSearch:
    def test_embedding_success(self):
        """embedding 启用且返回结果。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = [
            {"content": "VPN配置指南", "title": "VPN手册", "doc_type": "md",
             "similarity": 0.85, "chunk_id": "c1"},
        ]
        with patch("src.memory.embedding.EmbeddingClient") as MockEmb:
            mock_client = MagicMock()
            mock_client.embed.return_value = [0.1, 0.2, 0.3]
            MockEmb.return_value = mock_client

            tool = KBSearchTool(store, {"enabled": True})
            r = tool.execute({"query": "VPN配置"})

        assert r["search_method"] == "embedding"
        assert len(r["results"]) == 1
        assert r["results"][0]["content"] == "VPN配置指南"

    def test_embedding_no_results_fallback_to_fulltext(self):
        """向量检索无结果时降级为全文检索。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = []
        store._kb_repo.search_kb_by_keyword.return_value = [
            {"content": "fallback内容", "title": "文档", "doc_type": "",
             "score": 1, "chunk_id": "c2"},
        ]
        with patch("src.memory.embedding.EmbeddingClient") as MockEmb:
            mock_client = MagicMock()
            mock_client.embed.return_value = [0.1]
            MockEmb.return_value = mock_client

            tool = KBSearchTool(store, {"enabled": True})
            r = tool.execute({"query": "测试"})

        assert r["search_method"] == "fulltext"

    def test_embedding_client_not_created_when_disabled(self):
        """embedding 未启用时不创建客户端。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        with patch("src.memory.embedding.EmbeddingClient") as MockEmb:
            tool = KBSearchTool(store, {"enabled": False})
            MockEmb.assert_not_called()
        assert tool.embedding_client is None

    def test_embedding_config_as_object_enabled(self):
        """embedding_config 为非字典对象（如 dataclass），enabled 属性为 True。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = [
            {"content": "C", "title": "T", "doc_type": "", "similarity": 0.9, "chunk_id": "c"},
        ]
        cfg = MagicMock()
        cfg.enabled = True
        with patch("src.memory.embedding.EmbeddingClient") as MockEmb:
            mock_client = MagicMock()
            mock_client.embed.return_value = [0.1]
            MockEmb.return_value = mock_client
            with patch("src.config.EmbeddingConfig") as MockCfg:
                MockCfg.return_value = cfg
                tool = KBSearchTool(store, cfg)
        assert tool.embedding_client is not None

    def test_check_health_non_dict_config(self):
        """check_health 对非字典 config 的正确处理。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.kb_stats.return_value = {"total_documents": 5, "total_chunks": 20}
        cfg = MagicMock()
        cfg.enabled = True
        tool = KBSearchTool(store, cfg)
        h = tool.check_health()
        assert h["embedding_enabled"] is True

    def test_embedding_client_init_failure(self):
        """embedding 客户端初始化失败时降级。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        with patch("src.memory.embedding.EmbeddingClient", side_effect=RuntimeError("no GPU")):
            tool = KBSearchTool(store, {"enabled": True})
        assert tool.embedding_client is None
        assert tool.intent_keywords is not None  # 仍正常创建

    def test_search_by_embedding_exception(self):
        """向量检索内部异常时返回空列表。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.side_effect = RuntimeError("vector db down")
        with patch("src.memory.embedding.EmbeddingClient") as MockEmb:
            mock_client = MagicMock()
            mock_client.embed.return_value = [0.1]
            MockEmb.return_value = mock_client

            tool = KBSearchTool(store, {"enabled": True})
            r = tool.execute({"query": "测试"})

        # 向量失败 → 降级全文检索或返回错误
        assert r.get("success") or r.get("error")

    def test_embed_query_returns_none(self):
        """embed 返回 None 时跳过向量检索。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb_by_keyword.return_value = [
            {"content": "F", "title": "T", "doc_type": "", "score": 0, "chunk_id": "c"},
        ]
        with patch("src.memory.embedding.EmbeddingClient") as MockEmb:
            mock_client = MagicMock()
            mock_client.embed.return_value = None
            MockEmb.return_value = mock_client

            tool = KBSearchTool(store, {"enabled": True})
            r = tool.execute({"query": "测试"})

        assert r["search_method"] == "fulltext"


# ============================================================================
# 全文搜索
# ============================================================================
class TestFulltextSearch:
    def test_fulltext_success(self):
        tool, store = _make_tool()
        store._kb_repo.search_kb_by_keyword.return_value = [
            {"content": "打印机设置", "title": "IT手册", "doc_type": "md",
             "score": 2, "chunk_id": "c3"},
        ]
        r = tool.execute({"query": "打印机"})
        assert r["search_method"] == "fulltext"
        assert r["results"][0]["source"] == "IT手册"

    def test_fulltext_exception(self):
        tool, store = _make_tool()
        store._kb_repo.search_kb_by_keyword.side_effect = RuntimeError("fts error")
        r = tool._search_by_fulltext("test", 3)
        assert r == []


# ============================================================================
# Fallback keywords
# ============================================================================
class TestFallbackKeywords:
    def test_fallback_keywords_non_empty(self):
        tool, _ = _make_tool()
        fk = tool._fallback_keywords()
        assert len(fk) > 0
        assert "知识库" in fk
        assert "VPN" in fk


# ============================================================================
# Health check
# ============================================================================
class TestHealthCheck:
    def test_healthy(self):
        tool, store = _make_tool()
        store._kb_repo.kb_stats.return_value = {"total_documents": 10, "total_chunks": 50}
        h = tool.check_health()
        assert h["status"] == "healthy"
        assert h["kb_documents"] == 10
        assert h["embedding_available"] is False

    def test_embedding_enabled_dict(self):
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.kb_stats.return_value = {"total_documents": 0, "total_chunks": 0}
        tool = KBSearchTool(store, {"enabled": True})
        h = tool.check_health()
        assert h["embedding_enabled"] is True

    def test_unhealthy(self):
        tool, store = _make_tool()
        store._kb_repo.kb_stats.side_effect = RuntimeError("db error")
        h = tool.check_health()
        assert h["status"] == "unhealthy"


# ============================================================================
# 意图关键词
# ============================================================================
class TestKBSearchIntentKeywords:
    def test_intent_keywords_are_explicit_triggers(self):
        tool, _ = _make_tool()
        assert "知识库" in tool.intent_keywords
        assert "搜索知识库" in tool.intent_keywords
        assert len(tool.intent_keywords) == 14


# ============================================================================
# safe_float
# ============================================================================
class TestSafeFloat:
    def test_valid(self):
        assert safe_float("0.5", 0.3) == 0.5

    def test_with_unit_falls_back(self):
        assert safe_float("0.3以上", 0.3) == 0.3

    def test_none_and_empty(self):
        assert safe_float(None, 0.3) == 0.3
        assert safe_float("", 0.3) == 0.3

    def test_int_input(self):
        assert safe_float(1, 0.3) == 1.0


def test_search_no_client_falls_back_to_fulltext():
    """embedding_client 为 None 时 search 跳过向量检索直接走全文（不崩）。"""
    store = MagicMock()
    store.conn.cursor.return_value.fetchall.return_value = []
    store._kb_repo.search_kb_by_keyword.return_value = [
        {"content": "F", "title": "T", "doc_type": "", "score": 1, "chunk_id": "c"},
    ]
    tool = KBSearchTool(store, {"enabled": False})
    assert tool.embedding_client is None
    r = tool.search("测试", top_k=5, min_similarity=0.5)
    assert r["search_method"] == "fulltext"


# ============================================================================
# _search_kb_embedding（统一向量检索）
# ============================================================================
class TestSearchKbEmbedding:
    def test_success(self):
        """向量检索正常返回且过滤 low-similarity 结果。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = [
            {"content": "VPN配置", "title": "手册", "doc_type": "md",
             "similarity": 0.92, "chunk_id": "c1"},
            {"content": "打印机设置", "title": "IT指南", "doc_type": "md",
             "similarity": 0.25, "chunk_id": "c2"},
        ]
        tool = KBSearchTool(store, {"enabled": False})
        r = tool._search_kb_embedding([0.1, 0.2], top_k=5, min_similarity=0.5)
        assert len(r) == 1
        assert r[0]["content"] == "VPN配置"
        assert r[0]["score"] == 0.92

    def test_exception_returns_empty(self):
        """store.search_kb 异常时返回空列表。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.side_effect = RuntimeError("crash")
        tool = KBSearchTool(store, {"enabled": False})
        r = tool._search_kb_embedding([0.1], top_k=3, min_similarity=0.5)
        assert r == []

    def test_no_results_above_threshold(self):
        """所有结果相似度低于阈值时返回空。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = [
            {"content": "x", "title": "t", "doc_type": "", "similarity": 0.1, "chunk_id": "c"},
        ]
        tool = KBSearchTool(store, {"enabled": False})
        r = tool._search_kb_embedding([0.1], top_k=3, min_similarity=0.5)
        assert r == []

    def test_missing_fields_default(self):
        """结果缺少字段时使用默认值。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = [{"similarity": 0.8}]
        tool = KBSearchTool(store, {"enabled": False})
        r = tool._search_kb_embedding([0.1], top_k=3, min_similarity=0.5)
        assert len(r) == 1
        assert r[0]["content"] == ""
        assert r[0]["source"] == "未知文档"

    def test_via_search_with_precomputed_embedding(self):
        """通过 search 传入 query_embedding 复用已算好的向量（跳过内部 embed）。"""
        store = MagicMock()
        store.conn.cursor.return_value.fetchall.return_value = []
        store._kb_repo.search_kb.return_value = [
            {"content": "命中", "title": "文档", "doc_type": "md",
             "similarity": 0.95, "chunk_id": "c99"},
        ]
        tool = KBSearchTool(store, {"enabled": False})
        # 手动设置 embedding_client 以进入向量检索分支
        tool.embedding_client = MagicMock()
        r = tool.search(query="测试", query_embedding=[0.1, 0.2])
        assert r["success"] is True
        assert r["search_method"] == "embedding"
        assert r["results"][0]["content"] == "命中"
        # 复用了传入向量，不应再调用 embed
        tool.embedding_client.embed.assert_not_called()
