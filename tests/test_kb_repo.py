"""KbRepo 仓储层单测。

覆盖此前测试盲区（原覆盖率 69%）：文档查重四级策略、字段白名单更新、
分页筛选、文档/分块删除的级联与索引同步、embedding 写入的维度漂移防护、
统计聚合与关键词兜底检索。

统一用真实临时 SQLite 库，确保 SQL 与事务语义本身被验证。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.memory.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_db_path):
    s = SQLiteStore(db_path=str(tmp_db_path))
    yield s
    try:
        s.close()
    except Exception:
        pass


@pytest.fixture
def repo(store):
    return store._kb_repo


def _add_doc(repo, title="文档A", doc_type="note", source="manual", **kw):
    return repo.add_kb_document(title=title, doc_type=doc_type, source=source, **kw)


# ── 查重：四级策略 ──

class TestCheckDuplicate:
    def test_no_duplicate_on_empty_kb(self, repo):
        assert repo.check_duplicate_document("新文档") == {"duplicate": False}

    def test_title_exact_match(self, repo):
        _add_doc(repo, title="季度报告")
        r = repo.check_duplicate_document("季度报告")
        assert r["duplicate"] is True
        assert r["reason"] == "标题完全匹配"
        assert r["doc"]["title"] == "季度报告"

    def test_source_id_match(self, repo):
        _add_doc(repo, title="原标题", source_id="doc-123")
        r = repo.check_duplicate_document("不同标题", source_id="doc-123")
        assert r["duplicate"] is True and r["reason"] == "来源ID重复"

    def test_url_match(self, repo):
        _add_doc(repo, title="原标题", url="https://example.com/a")
        r = repo.check_duplicate_document("不同标题", url="https://example.com/a")
        assert r["duplicate"] is True and r["reason"] == "URL重复"

    def test_content_hash_match(self, repo):
        """相同正文即便标题不同也应判重（靠 metadata 里的 content_hash）。"""
        _add_doc(repo, title="原标题", content="这是一段正文内容")
        r = repo.check_duplicate_document("完全不同的标题", content="这是一段正文内容")
        assert r["duplicate"] is True and r["reason"] == "内容哈希匹配"

    def test_content_hash_differs_no_duplicate(self, repo):
        _add_doc(repo, title="原标题", content="正文甲")
        assert repo.check_duplicate_document("新标题", content="正文乙") == {"duplicate": False}

    def test_vector_similarity_match(self, repo):
        doc_id = _add_doc(repo, title="向量文档")
        repo.add_kb_chunks(doc_id, ["片段"])
        chunk_id = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.update_chunk_embedding(chunk_id, [1.0, 0.0, 0.0])
        r = repo.check_duplicate_document("全新标题", embedding=[1.0, 0.0, 0.0])
        assert r["duplicate"] is True
        assert "向量相似度" in r["reason"]

    def test_vector_below_threshold_not_duplicate(self, repo):
        doc_id = _add_doc(repo, title="向量文档")
        repo.add_kb_chunks(doc_id, ["片段"])
        chunk_id = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.update_chunk_embedding(chunk_id, [1.0, 0.0, 0.0])
        r = repo.check_duplicate_document("全新标题", embedding=[0.0, 1.0, 0.0])
        assert r == {"duplicate": False}

    def test_malformed_embedding_skipped_not_raised(self, repo, store):
        """库里存在损坏的 embedding 时应跳过该行，而不是让整次查重炸掉。"""
        doc_id = _add_doc(repo, title="坏向量文档")
        repo.add_kb_chunks(doc_id, ["片段"])
        store.conn.execute("UPDATE kb_chunks SET embedding = '{bad json'")
        store.conn.commit()
        assert repo.check_duplicate_document("新标题", embedding=[1.0, 0.0]) == {"duplicate": False}


# ── 文档增改查 ──

class TestDocumentCrud:
    def test_add_returns_id_and_status_pending(self, repo):
        doc_id = _add_doc(repo)
        doc = repo.get_kb_document(doc_id)
        assert doc["status"] == "pending"
        assert doc["title"] == "文档A"

    def test_add_with_content_writes_content_hash(self, repo):
        doc_id = _add_doc(repo, content="正文")
        meta = json.loads(repo.get_kb_document(doc_id)["metadata"])
        assert len(meta["content_hash"]) == 32

    def test_add_preserves_caller_metadata(self, repo):
        doc_id = _add_doc(repo, metadata={"author": "张三"}, content="正文")
        meta = json.loads(repo.get_kb_document(doc_id)["metadata"])
        assert meta["author"] == "张三" and "content_hash" in meta

    def test_get_missing_doc_returns_none(self, repo):
        assert repo.get_kb_document(99999) is None

    def test_update_no_kwargs_is_noop(self, repo):
        doc_id = _add_doc(repo)
        before = repo.get_kb_document(doc_id)["updated_at"]
        repo.update_kb_document(doc_id)
        assert repo.get_kb_document(doc_id)["updated_at"] == before

    def test_update_applies_allowed_fields(self, repo):
        doc_id = _add_doc(repo)
        repo.update_kb_document(doc_id, title="新标题", status="indexed")
        doc = repo.get_kb_document(doc_id)
        assert doc["title"] == "新标题" and doc["status"] == "indexed"

    def test_update_rejects_unknown_field(self, repo):
        """白名单外的键必须被丢弃——kwargs 直接拼进 SQL 就是注入面。"""
        doc_id = _add_doc(repo)
        repo.update_kb_document(doc_id, evil="x")  # 全部被过滤 → 直接返回
        assert repo.get_kb_document(doc_id)["title"] == "文档A"

    def test_update_mixed_fields_keeps_only_allowed(self, repo):
        doc_id = _add_doc(repo)
        repo.update_kb_document(doc_id, title="改了", nonexistent_col="boom")
        assert repo.get_kb_document(doc_id)["title"] == "改了"

    def test_update_refreshes_updated_at(self, repo):
        doc_id = _add_doc(repo)
        repo.update_kb_document(doc_id, created_at="2000-01-01T00:00:00")
        doc = repo.get_kb_document(doc_id)
        assert doc["created_at"] == "2000-01-01T00:00:00"
        assert doc["updated_at"] != "2000-01-01T00:00:00"  # 由仓储强制刷新


class TestListDocuments:
    def test_returns_items_and_filtered_total(self, repo):
        for i in range(5):
            _add_doc(repo, title=f"文档{i}")
        items, total = repo.list_kb_documents(limit=2)
        assert total == 5 and len(items) == 2

    def test_offset_paginates(self, repo):
        for i in range(3):
            _add_doc(repo, title=f"文档{i}")
        items, total = repo.list_kb_documents(limit=2, offset=2)
        assert total == 3 and len(items) == 1

    def test_filter_by_status(self, repo):
        d1 = _add_doc(repo, title="A")
        _add_doc(repo, title="B")
        repo.update_kb_document(d1, status="indexed")
        items, total = repo.list_kb_documents(status="indexed")
        assert total == 1 and items[0]["title"] == "A"

    def test_filter_by_doc_type(self, repo):
        _add_doc(repo, title="A", doc_type="note")
        _add_doc(repo, title="B", doc_type="faq")
        items, total = repo.list_kb_documents(doc_type="faq")
        assert total == 1 and items[0]["title"] == "B"

    def test_total_is_filtered_not_global(self, repo):
        """total 必须是过滤后的数量，否则前端翻页会出现空白尾页。"""
        _add_doc(repo, title="A", doc_type="note")
        for i in range(4):
            _add_doc(repo, title=f"F{i}", doc_type="faq")
        _, total = repo.list_kb_documents(doc_type="note", limit=1)
        assert total == 1


# ── 分块生命周期 ──

class TestChunks:
    def test_add_chunks_sets_count_and_status(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a", "b", "c"])
        doc = repo.get_kb_document(doc_id)
        assert doc["chunk_count"] == 3 and doc["status"] == "indexed"

    def test_chunks_ordered_by_index(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["第一", "第二", "第三"])
        chunks = repo.list_kb_chunks(doc_id)
        assert [c["content"] for c in chunks] == ["第一", "第二", "第三"]
        assert [c["chunk_index"] for c in chunks] == [0, 1, 2]

    def test_add_chunks_bumps_kb_revision(self, repo, store):
        before = store.get_kb_revision()
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        assert store.get_kb_revision() > before

    def test_mark_chunk_retry_pending(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        cid = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.mark_chunk_retry_pending(cid)
        row = store.conn.execute(
            "SELECT retry_pending FROM kb_chunks WHERE id = ?", (cid,)).fetchone()
        assert row["retry_pending"] == 1

    def test_update_chunk_embedding_persists_json(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        cid = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.update_chunk_embedding(cid, [0.1, 0.2, 0.3])
        assert json.loads(repo.list_kb_chunks(doc_id)[0]["embedding"]) == [0.1, 0.2, 0.3]

    def test_dimension_drift_skips_index_update(self, repo, store, caplog):
        """维度不一致时只能跳过索引写入——强行 add 会抛错并破坏 DB/索引一致性。"""
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a", "b"])
        chunks = repo.list_kb_chunks(doc_id)
        repo.update_chunk_embedding(chunks[0]["id"], [1.0, 0.0, 0.0])
        assert store._index_dim == 3
        fake_index = MagicMock()
        store._vector_index = fake_index
        with caplog.at_level("WARNING"):
            repo.update_chunk_embedding(chunks[1]["id"], [1.0, 0.0])  # 2 维
        fake_index.add.assert_not_called()
        assert "维度" in caplog.text
        # DB 仍然写入了（跳过的只是索引）
        assert json.loads(repo.list_kb_chunks(doc_id)[1]["embedding"]) == [1.0, 0.0]

    def test_index_add_failure_does_not_raise(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        cid = repo.list_kb_chunks(doc_id)[0]["id"]
        fake_index = MagicMock()
        fake_index.add.side_effect = RuntimeError("faiss boom")
        store._vector_index = fake_index
        store._index_dim = 3
        repo.update_chunk_embedding(cid, [1.0, 0.0, 0.0])  # 不应抛出


class TestDelete:
    def test_delete_document_cascades_chunks(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a", "b"])
        repo.delete_kb_document(doc_id)
        assert repo.get_kb_document(doc_id) is None
        assert repo.list_kb_chunks(doc_id) == []

    def test_delete_document_removes_vectors_from_index(self, repo, store):
        """SQLite 删了但 faiss 没删 = 幽灵向量，检索会命中已删内容。"""
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a", "b"])
        chunk_ids = [c["id"] for c in repo.list_kb_chunks(doc_id)]
        fake_index = MagicMock()
        fake_index.remove.return_value = True
        fake_index.index_path = None
        store._vector_index = fake_index
        repo.delete_kb_document(doc_id)
        removed = {c.args[0] for c in fake_index.remove.call_args_list}
        assert removed == set(chunk_ids)

    def test_delete_chunk_decrements_count(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a", "b", "c"])
        cid = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.delete_kb_chunk(cid)
        assert repo.get_kb_document(doc_id)["chunk_count"] == 2
        assert len(repo.list_kb_chunks(doc_id)) == 2

    def test_delete_missing_chunk_keeps_count(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        repo.delete_kb_chunk(99999)
        assert repo.get_kb_document(doc_id)["chunk_count"] == 1

    def test_chunk_count_never_negative(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        store.conn.execute("UPDATE kb_documents SET chunk_count = 0 WHERE id = ?", (doc_id,))
        store.conn.commit()
        cid = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.delete_kb_chunk(cid)
        assert repo.get_kb_document(doc_id)["chunk_count"] == 0

    def test_index_remove_failure_does_not_block_delete(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a"])
        fake_index = MagicMock()
        fake_index.remove.side_effect = RuntimeError("faiss boom")
        store._vector_index = fake_index
        repo.delete_kb_document(doc_id)  # 不应抛出
        assert repo.get_kb_document(doc_id) is None

    def test_no_index_is_noop(self, repo, store):
        store._vector_index = None
        repo._remove_chunks_from_index([1, 2, 3])  # 不应抛出


# ── 统计 ──

class TestStats:
    def test_kb_stats_shape(self, repo):
        d1 = _add_doc(repo, title="A", doc_type="note", source="manual")
        _add_doc(repo, title="B", doc_type="faq", source="web")
        repo.add_kb_chunks(d1, ["x", "y"])
        cid = repo.list_kb_chunks(d1)[0]["id"]
        repo.update_chunk_embedding(cid, [1.0, 0.0])
        s = repo.kb_stats()
        assert s["total_documents"] == 2
        assert s["total_chunks"] == 2
        assert s["indexed_chunks"] == 1
        assert s["indexed_docs"] == 1
        assert {r["doc_type"] for r in s["by_type"]} == {"note", "faq"}
        assert {r["source"] for r in s["by_source"]} == {"manual", "web"}

    def test_empty_stats(self, repo):
        s = repo.kb_stats()
        assert s["total_documents"] == 0 and s["by_type"] == []

    def test_count_helpers(self, repo):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["a", "b"])
        assert repo.count_kb_documents() == 1
        assert repo.count_embedded_chunks() == 0
        cid = repo.list_kb_chunks(doc_id)[0]["id"]
        repo.update_chunk_embedding(cid, [1.0])
        assert repo.count_embedded_chunks() == 1


# ── 关键词兜底检索 ──

class TestKeywordSearch:
    def test_blank_query_returns_empty(self, repo):
        assert repo.search_kb_by_keyword("") == []
        assert repo.search_kb_by_keyword("   ") == []

    def test_punctuation_only_query_returns_empty(self, repo):
        assert repo.search_kb_by_keyword("!!!???") == []

    def test_chinese_bigram_matches_partial(self, repo):
        """中文按 2-gram 拆分：查"钉钉文档管理"应能命中只含"钉钉"的文档。"""
        doc_id = _add_doc(repo, title="钉钉指南")
        repo.add_kb_chunks(doc_id, ["钉钉是一款协作工具"])
        assert len(repo.search_kb_by_keyword("钉钉文档管理")) >= 1

    def test_english_word_match(self, repo):
        doc_id = _add_doc(repo, title="Python Guide")
        repo.add_kb_chunks(doc_id, ["Python is a language"])
        assert len(repo.search_kb_by_keyword("python")) >= 1

    def test_score_normalized_to_unit_interval(self, repo):
        """分数必须归一到 [0,1]，否则会漏成用户可见的「相关度 300%」。"""
        doc_id = _add_doc(repo, title="钉钉")
        repo.add_kb_chunks(doc_id, ["钉钉钉钉钉钉"])
        for r in repo.search_kb_by_keyword("钉钉"):
            assert 0.0 <= r["score"] <= 1.0

    def test_results_sorted_desc_and_capped(self, repo):
        for i in range(6):
            doc_id = _add_doc(repo, title=f"钉钉文档{i}")
            repo.add_kb_chunks(doc_id, ["钉钉内容"])
        results = repo.search_kb_by_keyword("钉钉", top_k=3)
        assert len(results) == 3
        assert results == sorted(results, key=lambda r: r["score"], reverse=True)

    def test_swallows_db_error(self, repo, store):
        store.conn.close()
        assert repo.search_kb_by_keyword("钉钉") == []


# ── 向量检索 ──

class TestVectorSearch:
    def test_empty_embedding_returns_empty(self, repo):
        assert repo.search_kb([]) == []

    def test_brute_force_fallback_ranks_by_similarity(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["近", "远"])
        chunks = repo.list_kb_chunks(doc_id)
        repo.update_chunk_embedding(chunks[0]["id"], [1.0, 0.0])
        repo.update_chunk_embedding(chunks[1]["id"], [0.0, 1.0])
        store._vector_index = None
        results = repo.search_kb([1.0, 0.0], top_k=2)
        assert results[0]["content"] == "近"
        assert results[0]["similarity"] > results[1]["similarity"]

    def test_doc_type_filter_in_fallback(self, repo, store):
        d1 = _add_doc(repo, title="A", doc_type="note")
        d2 = _add_doc(repo, title="B", doc_type="faq")
        for d in (d1, d2):
            repo.add_kb_chunks(d, ["内容"])
            repo.update_chunk_embedding(repo.list_kb_chunks(d)[0]["id"], [1.0, 0.0])
        store._vector_index = None
        results = repo.search_kb([1.0, 0.0], doc_type="faq")
        assert len(results) == 1 and results[0]["doc_type"] == "faq"

    def test_min_similarity_filters_low_scores(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["正交"])
        repo.update_chunk_embedding(repo.list_kb_chunks(doc_id)[0]["id"], [0.0, 1.0])
        store._vector_index = None
        assert repo.search_kb([1.0, 0.0], min_similarity=0.5) == []

    def test_malformed_embedding_row_skipped(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["好", "坏"])
        chunks = repo.list_kb_chunks(doc_id)
        repo.update_chunk_embedding(chunks[0]["id"], [1.0, 0.0])
        store.conn.execute("UPDATE kb_chunks SET embedding = '{bad' WHERE id = ?",
                           (chunks[1]["id"],))
        store.conn.commit()
        store._vector_index = None
        results = repo.search_kb([1.0, 0.0], top_k=5)
        assert [r["content"] for r in results] == ["好"]

    def test_faiss_failure_falls_back_to_brute_force(self, repo, store):
        doc_id = _add_doc(repo)
        repo.add_kb_chunks(doc_id, ["内容"])
        repo.update_chunk_embedding(repo.list_kb_chunks(doc_id)[0]["id"], [1.0, 0.0])
        fake_index = MagicMock()
        fake_index.count = 1
        fake_index.search.side_effect = RuntimeError("faiss boom")
        store._vector_index = fake_index
        results = repo.search_kb([1.0, 0.0])
        assert len(results) == 1 and results[0]["content"] == "内容"
