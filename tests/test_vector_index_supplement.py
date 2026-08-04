"""VectorIndex 补充测试：零向量归一化、remove、save/load 持久化。"""
from __future__ import annotations

import os
import tempfile

import numpy as np

from src.memory.vector_index import VectorIndex


class TestVectorIndexEdge:
    def test_normalize_zero_vector(self):
        """零向量归一化不会被 NaN 污染。"""
        idx = VectorIndex(dim=4)
        vec = np.array([[0.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        normalized = idx._normalize(vec)
        # 归一化后仍为零向量（除以 1 而非除以 0）
        assert np.all(normalized == 0.0)

    def test_add_and_search(self):
        idx = VectorIndex(dim=4)
        idx.add(1, [1.0, 0.0, 0.0, 0.0])
        idx.add(2, [0.0, 1.0, 0.0, 0.0])
        results = idx.search([1.0, 0.0, 0.0, 0.0], top_k=5)
        assert len(results) >= 1
        # chunk_id=1 应该最相似
        assert results[0][0] == 1

    def test_search_empty_index(self):
        idx = VectorIndex(dim=4)
        results = idx.search([1.0, 0.0, 0.0, 0.0])
        assert results == []

    def test_remove_existing(self):
        idx = VectorIndex(dim=4)
        idx.add(1, [1.0, 0.0, 0.0, 0.0])
        assert idx.remove(1) is True
        # count 用 id_map 而非 ntotal
        assert idx.count == 0

    def test_remove_nonexistent(self):
        idx = VectorIndex(dim=4)
        assert idx.remove(999) is False

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            idx = VectorIndex(dim=4, index_path=os.path.join(td, "test.faiss"))
            idx.add(1, [1.0, 0.0, 0.0, 0.0])
            idx.add(2, [0.0, 1.0, 0.0, 0.0])
            idx.save()

            # 验证文件存在
            assert os.path.exists(os.path.join(td, "test.faiss"))
            assert os.path.exists(os.path.join(td, "test.faiss.map.json"))

            # 加载到新实例
            idx2 = VectorIndex(dim=4, index_path=os.path.join(td, "test.faiss"))
            success = idx2.load()
            assert success
            assert idx2.count == 2

            # 搜索验证
            results = idx2.search([1.0, 0.0, 0.0, 0.0], top_k=1)
            assert len(results) == 1
            assert results[0][0] == 1

    def test_load_nonexistent(self):
        idx = VectorIndex(dim=4, index_path="/nonexistent/path.faiss")
        assert idx.load() is False

    def test_save_no_index_path(self):
        idx = VectorIndex(dim=4)
        idx.add(1, [1.0, 0.0, 0.0, 0.0])
        # 无 index_path 时不保存，不崩溃
        idx.save()

    def test_rebuild(self):
        idx = VectorIndex(dim=4)
        idx.add(1, [1.0, 0.0, 0.0, 0.0])
        idx.rebuild([(10, [0.0, 1.0, 0.0, 0.0]), (20, [0.0, 0.0, 1.0, 0.0])])
        assert idx.count == 2
        results = idx.search([0.0, 1.0, 0.0, 0.0])
        assert results[0][0] == 10

    def test_add_batch(self):
        idx = VectorIndex(dim=4)
        idx.add_batch([
            (1, [1.0, 0.0, 0.0, 0.0]),
            (2, [0.0, 1.0, 0.0, 0.0]),
            (3, [0.0, 0.0, 1.0, 0.0]),
        ])
        assert idx.count == 3
        assert idx.raw_count == 3

    def test_add_batch_empty(self):
        idx = VectorIndex(dim=4)
        idx.add_batch([])
        assert idx.count == 0

    def test_search_after_remove_uses_id_map(self):
        """remove 后 count 反映了已删除向量。"""
        idx = VectorIndex(dim=4)
        idx.add(1, [1.0, 0.0, 0.0, 0.0])
        idx.add(2, [0.0, 1.0, 0.0, 0.0])
        idx.remove(1)
        assert idx.count == 1
        # raw_count 仍为 2（faiss 底层不缩）
        assert idx.raw_count == 2
