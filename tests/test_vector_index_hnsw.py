"""F18 RAG 检索规模化测试。

覆盖：HNSW 与 flat 检索一致性、幽灵向量自动重建、缓存开关、非法类型回落、
索引类型持久化（save/load 往返）、以及加载后缓存反推支撑自动重建。
"""

from __future__ import annotations

import os
import tempfile

import numpy as np

from src.memory.vector_index import VectorIndex


def _one_hot(dim: int, i: int) -> list[float]:
    """第 i 个标准基向量（L2 已归一化，内积=余弦）。"""
    v = [0.0] * dim
    v[i] = 1.0
    return v


def _build_orthogonal(dim: int, n: int) -> list[tuple[int, list[float]]]:
    """n 个互相正交的单位向量（chunk_id = i，向量 = e_i）。要求 n <= dim。"""
    return [(i, _one_hot(dim, i)) for i in range(n)]


class TestHnswParity:
    def test_hnsw_self_similarity_is_one(self):
        vi = VectorIndex(dim=8, index_type="hnsw", hnsw_ef=64)
        vi.add_batch(_build_orthogonal(8, 8))
        res = vi.search(_one_hot(8, 3), top_k=5)
        assert res[0][0] == 3
        assert res[0][1] > 0.99

    def test_hnsw_top1_matches_flat(self):
        items = _build_orthogonal(8, 8)
        flat = VectorIndex(dim=8, index_type="flat")
        hnsw = VectorIndex(dim=8, index_type="hnsw", hnsw_ef=64)
        flat.add_batch(items)
        hnsw.add_batch(items)
        for q in range(8):
            r_flat = flat.search(_one_hot(8, q), top_k=3)[0][0]
            r_hnsw = hnsw.search(_one_hot(8, q), top_k=3)[0][0]
            assert r_flat == r_hnsw == q

    def test_invalid_index_type_falls_back_to_flat(self):
        vi = VectorIndex(dim=4, index_type="bogus-type")
        assert vi.index_type == "flat"
        assert vi._index is not None
        vi.add(chunk_id=0, embedding=_one_hot(4, 0))
        assert vi.count == 1


class TestPhantomAutoRebuild:
    def test_maybe_rebuild_clears_phantoms(self):
        vi = VectorIndex(dim=16, cache_embeddings=True, phantom_rebuild_ratio=0.3)
        vi.add_batch(_build_orthogonal(16, 10))
        for i in range(4):  # 删 4/10 → live=6, phantom=4, ratio 0.67 > 0.3
            vi.remove(i)
        assert vi.count == 6
        assert vi.raw_count == 10  # remove 不立即重建

        rebuilt = vi.maybe_rebuild()
        assert rebuilt is True
        assert vi.count == 6
        assert vi.raw_count == 6  # 幽灵已回收
        # 已删 chunk 不再可检索（存活 chunk 仍可精确命中）
        assert 0 not in [cid for cid, _ in vi.search(_one_hot(16, 0), top_k=10)]
        res = vi.search(_one_hot(16, 4), top_k=10)
        assert res[0][0] == 4 and res[0][1] > 0.99

    def test_add_triggers_auto_rebuild_when_over_threshold(self):
        vi = VectorIndex(dim=16, cache_embeddings=True, phantom_rebuild_ratio=0.3)
        vi.add_batch(_build_orthogonal(16, 10))
        for i in range(4):
            vi.remove(i)  # live=6, phantom=4 → 超阈值
        # 下次 add 进入前自动重建
        vi.add(chunk_id=100, embedding=_one_hot(16, 0))
        assert vi.count == 7
        assert vi.raw_count == 7  # 自动重建回收了幽灵
        # 存活向量（chunk 4）仍可精确检索；新加 chunk 100（向量 e_0）亦命中
        res = vi.search(_one_hot(16, 4), top_k=10)
        assert res[0][0] == 4 and res[0][1] > 0.99
        assert vi.search(_one_hot(16, 0), top_k=10)[0][0] == 100

    def test_no_rebuild_below_threshold(self):
        vi = VectorIndex(dim=16, cache_embeddings=True, phantom_rebuild_ratio=0.6)
        vi.add_batch(_build_orthogonal(16, 10))
        vi.remove(0)  # live=9, phantom=1, ratio 0.11 < 0.6
        assert vi.maybe_rebuild() is False
        assert vi.raw_count == 10  # 未重建

    def test_cache_disabled_maybe_rebuild_is_noop(self):
        vi = VectorIndex(dim=16, cache_embeddings=False, phantom_rebuild_ratio=0.1)
        vi.add_batch(_build_orthogonal(16, 10))
        vi.remove(0)
        vi.remove(1)  # phantom=2，远超阈值
        assert vi.maybe_rebuild() is False  # 无缓存无法自重建
        assert vi.raw_count == 10

    def test_explicit_rebuild_from_cache(self):
        vi = VectorIndex(dim=16, cache_embeddings=True)
        vi.add_batch(_build_orthogonal(16, 10))
        vi.remove(0)
        vi.remove(1)
        vi.rebuild()  # 不传 items，从缓存重建
        assert vi.count == 8
        assert vi.raw_count == 8


class TestIndexTypePersistence:
    def test_hnsw_survives_save_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "idx.faiss")
            vi = VectorIndex(dim=8, index_type="hnsw", hnsw_ef=64, index_path=path)
            vi.add_batch(_build_orthogonal(8, 8))
            vi.save()

            vi2 = VectorIndex(dim=8, index_path=path)
            assert vi2.load()
            assert vi2.index_type == "hnsw"
            assert vi2.count == 8
            res = vi2.search(_one_hot(8, 5), top_k=3)
            assert res[0][0] == 5 and res[0][1] > 0.99

    def test_load_populates_cache_for_post_load_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "idx.faiss")
            vi = VectorIndex(dim=16, index_type="flat", index_path=path,
                             cache_embeddings=True, phantom_rebuild_ratio=0.3)
            vi.add_batch(_build_orthogonal(16, 10))
            vi.save()

            vi2 = VectorIndex(dim=16, index_path=path, cache_embeddings=True,
                              phantom_rebuild_ratio=0.3)
            assert vi2.load()
            # 加载后缓存应被反推出来，支撑自动重建
            for i in range(4):
                vi2.remove(i)  # phantom=4 > 阈值
            assert vi2.maybe_rebuild() is True
            assert vi2.raw_count == 6
