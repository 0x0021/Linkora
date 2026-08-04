"""FAISS 向量索引单元测试。

覆盖 VectorIndex 全部操作：add / add_batch / search / remove / rebuild / save / load / count。
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from src.memory.vector_index import VectorIndex


def _rand_vec(dim=128):
    """随机单位向量（L2 归一化后可直接用于内积=余弦）。"""
    v = np.random.randn(dim).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


class TestInit:
    def test_creates_index(self):
        vi = VectorIndex(dim=128)
        assert vi.dim == 128
        assert vi._index is not None
        assert vi._index.ntotal == 0

    def test_with_index_path(self):
        vi = VectorIndex(dim=64, index_path="/tmp/test_vi.faiss")
        assert vi.index_path == "/tmp/test_vi.faiss"

    def test_dim_preserved(self):
        vi = VectorIndex(dim=256)
        assert vi.dim == 256


class TestAdd:
    def test_single_add(self):
        vi = VectorIndex(dim=128)
        vec = _rand_vec()
        vi.add(chunk_id=1, embedding=vec)
        assert vi.count == 1
        assert vi.raw_count == 1

    def test_add_multiple(self):
        vi = VectorIndex(dim=128)
        for i in range(5):
            vi.add(chunk_id=i, embedding=_rand_vec())
        assert vi.count == 5


class TestAddBatch:
    def test_batch_add(self):
        vi = VectorIndex(dim=128)
        items = [(i, _rand_vec()) for i in range(10)]
        vi.add_batch(items)
        assert vi.count == 10

    def test_empty_batch(self):
        vi = VectorIndex(dim=128)
        vi.add_batch([])
        assert vi.count == 0


class TestSearch:
    def test_search_returns_similar(self):
        vi = VectorIndex(dim=128)
        v0 = _rand_vec()
        vi.add(chunk_id=0, embedding=v0)
        vi.add(chunk_id=1, embedding=_rand_vec())

        results = vi.search(v0, top_k=5)
        assert len(results) >= 1
        # 自身相似度应接近 1.0
        assert results[0][0] == 0
        assert results[0][1] > 0.99

    def test_search_empty_index(self):
        vi = VectorIndex(dim=128)
        assert vi.search(_rand_vec()) == []

    def test_search_top_k_clamped(self):
        vi = VectorIndex(dim=128)
        for i in range(3):
            vi.add(chunk_id=i, embedding=_rand_vec())
        results = vi.search(_rand_vec(), top_k=10)
        assert len(results) <= 3


class TestRemove:
    def test_remove_existing(self):
        vi = VectorIndex(dim=128)
        vi.add(chunk_id=42, embedding=_rand_vec())
        assert vi.count == 1
        assert vi.remove(42)
        # remove 仅摘除映射，count 减 1，raw_count 不变
        assert vi.count == 0
        assert vi.raw_count == 1

    def test_remove_nonexistent(self):
        vi = VectorIndex(dim=128)
        assert not vi.remove(999)


class TestRebuild:
    def test_rebuild_clears_and_re_adds(self):
        vi = VectorIndex(dim=128)
        vi.add(chunk_id=1, embedding=_rand_vec())
        vi.add(chunk_id=2, embedding=_rand_vec())
        vi.remove(1)  # mark deleted

        new_items = [(10, _rand_vec()), (11, _rand_vec())]
        vi.rebuild(new_items)
        assert vi.count == 2
        # 旧 id 被清理
        assert not vi.remove(1)
        assert vi.remove(10)


class TestSaveLoad:
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.faiss")
            vi = VectorIndex(dim=128, index_path=path)
            items = [(i, _rand_vec()) for i in range(5)]
            vi.add_batch(items)
            vi.save()

            # 验证文件存在
            assert os.path.exists(path)
            assert os.path.exists(path + ".map.json")

            # 加载到新实例
            vi2 = VectorIndex(dim=128, index_path=path)
            assert vi2.load()

            assert vi2.count == 5
            assert vi2.dim == 128

            # 搜索验证向量有效
            results = vi2.search(items[0][1])
            assert len(results) >= 1
            assert results[0][1] > 0.99

    def test_load_nonexistent_path(self):
        vi = VectorIndex(dim=128, index_path="/nonexistent/path.faiss")
        assert not vi.load()

    def test_load_no_path(self):
        vi = VectorIndex(dim=128)
        assert not vi.load()

    def test_save_no_path_skips(self):
        vi = VectorIndex(dim=128)
        vi.save()  # 不应抛异常
        # index_path 为 None 时 save 直接返回

    def test_load_without_map_file(self):
        """索引文件存在但缺少 .map.json 也应能正常加载（无映射）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.faiss")
            vi = VectorIndex(dim=128, index_path=path)
            vi.add(chunk_id=1, embedding=_rand_vec())
            vi.save()
            # 删除 map 文件
            os.remove(path + ".map.json")

            vi2 = VectorIndex(dim=128, index_path=path)
            assert vi2.load()
            # 无映射时 count=0，但底层索引向量还在
            assert vi2.count == 0
            assert vi2.raw_count == 1


class TestCountProperties:
    def test_count_and_raw_count(self):
        vi = VectorIndex(dim=128)
        assert vi.count == 0
        assert vi.raw_count == 0

        vi.add(chunk_id=1, embedding=_rand_vec())
        assert vi.count == 1
        assert vi.raw_count == 1

        vi.remove(1)
        assert vi.count == 0
        assert vi.raw_count == 1  # faiss 底层不减


class TestNormalize:
    def test_zero_vector_normalized(self):
        """零向量归一化后不会导致 NaN。"""
        vi = VectorIndex(dim=4)
        vi.add(chunk_id=0, embedding=[0.0, 0.0, 0.0, 0.0])
        # 不抛异常即通过
        assert vi.count == 1
