from __future__ import annotations

import json
import logging
import math
import os
import threading
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:  # 仅供类型检查；faiss 保持方法体内延迟导入（避免启动开销）
    import faiss

logger = logging.getLogger(__name__)

# F18: 索引类型白名单（仅这两种被接受，其余回落 flat）。
_VECTOR_INDEX_TYPES = ("flat", "hnsw")


class VectorIndex:
    """基于 faiss 的向量索引，用于高效相似度检索。

    支持增量添加、索引持久化到磁盘，以及 F18 的规模化增强：
    - 索引类型可配置：flat（默认，IndexFlatIP 精确 O(N)）或 hnsw（IndexHNSWFlat 近似，提速）。
    - 内存 embedding 缓存：支撑「幽灵向量占比超阈值时自动重建回收底层空间」，
      避免 faiss 不支持原地删除导致底层向量无限膨胀。
    """

    def __init__(self, dim: int, index_path: str | None = None, *,
                 index_type: str = "flat", hnsw_ef: int = 64,
                 phantom_rebuild_ratio: float = 0.1, cache_embeddings: bool = True):
        # P0-4: 降低重建阈值从 0.3 到 0.1，更早触发重建回收内存
        self.dim = dim
        self.index_path = index_path
        # 校验并回落不安全值，避免外部传入未知类型导致静默异常
        self.index_type = index_type if index_type in _VECTOR_INDEX_TYPES else "flat"
        self.hnsw_ef = max(1, int(hnsw_ef))
        self.phantom_rebuild_ratio = float(phantom_rebuild_ratio)
        self.cache_embeddings = bool(cache_embeddings)
        self._id_map: dict[int, int] = {}        # faiss_idx -> chunk_id
        self._reverse_map: dict[int, int] = {}    # chunk_id -> faiss_idx
        # 归一化后的 embedding 缓存：chunk_id -> np.ndarray（F18 自动重建用）
        self._emb_cache: Optional[dict[int, np.ndarray]] = {} if self.cache_embeddings else None
        # 惰性注解（from __future__ import annotations）：运行时不求值，
        # 不会因 faiss 未导入而报错；此前写成 object 使全部 faiss 成员访问失去检查
        self._index: Optional[faiss.Index] = None
        self._lock = threading.RLock()
        self._init_index()

    def _init_index(self) -> None:
        import faiss
        if self.index_type == "hnsw":
            try:
                # 必须用 METRIC_INNER_PRODUCT：归一化后内积 = 余弦相似度。
                # 默认 L2 度量会让余弦相似度全部归零（已踩坑验证）。
                index = faiss.IndexHNSWFlat(self.dim, self.hnsw_ef, faiss.METRIC_INNER_PRODUCT)
                # efSearch 提高召回；建议 >= efConstruction。默认取较大值保召回。
                index.hnsw.efSearch = max(self.hnsw_ef, 32)
                self._index = index
                logger.info("FAISS HNSW 索引已创建，维度=%d, ef=%d", self.dim, self.hnsw_ef)
                return
            except Exception as e:
                logger.warning("HNSW 索引创建失败(%s)，回退 IndexFlatIP", e)
        self._index = faiss.IndexFlatIP(self.dim)
        logger.info("FAISS 索引已创建(flat)，维度=%d", self.dim)

    def _normalize(self, vectors: np.ndarray) -> np.ndarray:
        """L2 归一化，使内积等价于余弦相似度。"""
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1  # 避免除以零
        return vectors / norms

    def _maybe_rebuild_locked(self) -> bool:
        """（持锁调用）幽灵向量占比超阈值时，用内存缓存重建回收底层空间。

        返回是否执行了重建。cache 关闭或为空、阈值未超、无幽灵时均返回 False。
        """
        if self.phantom_rebuild_ratio <= 0 or self._emb_cache is None:
            return False
        live = len(self._id_map)
        phantom = (self._index.ntotal if self._index else 0) - live
        if phantom <= 0:
            return False
        denom = live if live > 0 else 1
        # 超过阈值才重建（ceil 保证至少多出一个幽灵即触发，避免浮点边界漏判）
        if phantom < math.ceil(self.phantom_rebuild_ratio * denom):
            return False
        items = [(cid, emb.tolist()) for cid, emb in self._emb_cache.items()]
        if not items:
            return False
        logger.info("幽灵向量占比超阈值(phantom=%d, live=%d, ratio=%.2f)，自动重建索引回收空间",
                    phantom, live, self.phantom_rebuild_ratio)
        self._rebuild_core(items)
        return True

    def maybe_rebuild(self) -> bool:
        """幽灵向量（remove 后底层未删）占比超阈值时，自动重建回收空间。

        阈值由 vector_phantom_rebuild_ratio 控制；依赖内存 embedding 缓存
        （cache_embeddings=True 时生效）。调用方也可周期性调用此方法做主动清理。
        """
        with self._lock:
            return self._maybe_rebuild_locked()

    def _add_single_core(self, chunk_id: int, embedding: list[float]) -> None:
        """（持锁、无自动重建）添加单个向量到底层索引。"""
        if self._index is None:
            return
        vec = np.array([embedding], dtype=np.float32)
        vec = self._normalize(vec)
        faiss_idx = self._index.ntotal
        self._index.add(vec)
        self._id_map[faiss_idx] = chunk_id
        self._reverse_map[chunk_id] = faiss_idx
        if self._emb_cache is not None:
            self._emb_cache[chunk_id] = vec[0].copy()

    def add(self, chunk_id: int, embedding: list[float]) -> None:
        """添加一个向量到索引。进入前先尝试幽灵向量自动重建（健康时为零成本 no-op）。"""
        with self._lock:
            self._maybe_rebuild_locked()
            self._add_single_core(chunk_id, embedding)

    def _add_batch_core(self, items: list[tuple[int, list[float]]]) -> None:
        """（持锁、无自动重建）批量添加向量到底层索引。"""
        if not items or self._index is None:
            return
        chunk_ids = [c for c, _ in items]
        vectors = [e for _, e in items]
        mat = np.array(vectors, dtype=np.float32)
        mat = self._normalize(mat)
        start_idx = self._index.ntotal
        self._index.add(mat)
        for i, chunk_id in enumerate(chunk_ids):
            faiss_idx = start_idx + i
            self._id_map[faiss_idx] = chunk_id
            self._reverse_map[chunk_id] = faiss_idx
        if self._emb_cache is not None:
            for i, chunk_id in enumerate(chunk_ids):
                self._emb_cache[chunk_id] = mat[i].copy()
        logger.info("已添加 %d 个向量到索引，总计=%d", len(items), self._index.ntotal)

    def add_batch(self, items: list[tuple[int, list[float]]]) -> None:
        """批量添加向量。进入前先尝试幽灵向量自动重建（健康时为零成本 no-op）。"""
        with self._lock:
            self._maybe_rebuild_locked()
            self._add_batch_core(items)

    def search(self, query_embedding: list[float], top_k: int = 5
               ) -> list[tuple[int, float]]:
        """搜索最相似的向量。

        返回: [(chunk_id, similarity), ...]
        """
        with self._lock:
            if self._index is None or self._index.ntotal == 0:
                return []

            vec = np.array([query_embedding], dtype=np.float32)
            vec = self._normalize(vec)

            k = min(top_k, self._index.ntotal)
            scores, indices = self._index.search(vec, k)

            results = []
            for score, idx in zip(scores[0], indices[0], strict=True):
                if idx < 0:
                    continue
                chunk_id = self._id_map.get(int(idx))
                if chunk_id is not None:
                    # IP 归一化后，score 就是余弦相似度
                    results.append((chunk_id, float(score)))

            return results

    def remove(self, chunk_id: int) -> bool:
        """从索引中移除一个向量（逻辑摘除映射 + 缓存）。

        faiss 不支持原地删除，底层向量（幽灵）需经重建回收；
        幽灵向量占比超阈值时由后续 add 的 maybe_rebuild 自动清理。
        注意：此处不立即触发重建，以保持 remove 后 raw_count 语义稳定（既有测试依赖）。
        """
        with self._lock:
            if chunk_id not in self._reverse_map:
                return False
            faiss_idx = self._reverse_map[chunk_id]
            del self._id_map[faiss_idx]
            del self._reverse_map[chunk_id]
            if self._emb_cache is not None:
                self._emb_cache.pop(chunk_id, None)
            return True

    def _rebuild_core(self, items: list[tuple[int, list[float]]]) -> None:
        """（持锁）用给定 items 重建底层索引与映射。items 为空则仅清空。"""
        old_count = len(self._id_map)
        self._id_map.clear()
        self._reverse_map.clear()
        if self._emb_cache is not None:
            self._emb_cache.clear()
        self._init_index()
        if items:
            self._add_batch_core(items)
        logger.info("索引已重建（前=%d, 后=%d），包含 %d 个向量",
                    old_count, len(items), self._index.ntotal if self._index else 0)

    MAX_FAILED_RATIO = 0.3  # 历史兼容：含义同 vector_phantom_rebuild_ratio 默认值

    def rebuild(self, items: Optional[list[tuple[int, list[float]]]] = None) -> None:
        """重建整个索引。

        - items 显式提供：用其重建（调用方从 DB/外部真源取得）。
        - items 为 None 且开启缓存：用内存 embedding 缓存重建（自包含）。
        - 两者皆无：仅清空并重置为空索引。
        """
        with self._lock:
            if items is None and self._emb_cache is not None:
                items = [(cid, emb.tolist()) for cid, emb in self._emb_cache.items()]
            self._rebuild_core(items or [])

    def _populate_cache_from_index(self) -> None:
        """（持锁）从已加载的 faiss 索引反推 embedding 缓存，使加载后也能自动重建。

        flat / hnsw 均保留原始向量，reconstruct 可还原；失败则放弃缓存（退化为
        仅搜索，phantom 清理交由调用方的 DB 同步重建路径处理）。
        """
        if self._emb_cache is None or self._index is None or self._index.ntotal == 0:
            return
        try:
            n = self._index.ntotal
            # flat / hnsw 均保留原始向量，reconstruct_n 可整体还原（已验证两种类型均可）
            vectors = self._index.reconstruct_n(0, n)
            for faiss_idx, chunk_id in self._id_map.items():
                if faiss_idx < n:
                    self._emb_cache[chunk_id] = np.asarray(vectors[faiss_idx], dtype=np.float32).copy()
        except Exception as e:
            logger.warning("从磁盘索引反推 embedding 缓存失败（自动重建将不可用）: %s", e)
            if self._emb_cache is not None:
                self._emb_cache.clear()

    def save(self, path: str | None = None) -> None:
        """保存索引到磁盘。

        使用临时文件 + os.replace 原子替换，避免主进程与 web 进程并发
        save 同一 .faiss 时互相截断/覆盖导致索引文件损坏。
        """
        with self._lock:
            if self._index is None:
                return

            save_path = path or self.index_path
            if not save_path:
                return

            import faiss
            import tempfile

            dir_name = os.path.dirname(save_path) or "."
            fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".faiss.tmp")
            os.close(fd)
            try:
                faiss.write_index(self._index, tmp)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

            # 原子替换索引文件
            os.replace(tmp, save_path)

            # 保存 id 映射 + F18 索引元数据（同样走临时文件 + 原子替换）
            map_path = save_path + ".map.json"
            map_tmp = tmp + ".map.json"
            with open(map_tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "id_map": self._id_map,
                    "dim": self.dim,
                    "index_type": self.index_type,
                    "hnsw_ef": self.hnsw_ef,
                    "phantom_rebuild_ratio": self.phantom_rebuild_ratio,
                    "cache_embeddings": self.cache_embeddings,
                }, f)
            os.replace(map_tmp, map_path)

            logger.info("索引已保存到 %s", save_path)

    def load(self, path: str | None = None) -> bool:
        """从磁盘加载索引。"""
        with self._lock:
            load_path = path or self.index_path
            if not load_path or not os.path.exists(load_path):
                return False

            import faiss
            self._index = faiss.read_index(load_path)

            map_path = load_path + ".map.json"
            if os.path.exists(map_path):
                with open(map_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._id_map = {int(k): v for k, v in data.get("id_map", {}).items()}
                    self._reverse_map = {v: int(k) for k, v in self._id_map.items()}
                    self.dim = data.get("dim", self.dim)
                    # F18：恢复索引类型与 HNSW 参数（缺省回落安全默认）
                    self.index_type = data.get("index_type", self.index_type)
                    if self.index_type not in _VECTOR_INDEX_TYPES:
                        self.index_type = "flat"
                    self.hnsw_ef = max(1, int(data.get("hnsw_ef", self.hnsw_ef)))
                    self.phantom_rebuild_ratio = float(data.get("phantom_rebuild_ratio", self.phantom_rebuild_ratio))
                    self.cache_embeddings = bool(data.get("cache_embeddings", self.cache_embeddings))
                    if self._emb_cache is not None and not self.cache_embeddings:
                        self._emb_cache = None
                    # HNSW 加载后 efSearch 不随序列化保存，需重新应用
                    hnsw = getattr(self._index, "hnsw", None)
                    if self.index_type == "hnsw" and hnsw is not None:
                        hnsw.efSearch = max(self.hnsw_ef, 32)

            # 反推 embedding 缓存（支撑加载后自动重建）
            if self.cache_embeddings:
                self._emb_cache = {}
                self._populate_cache_from_index()
            else:
                self._emb_cache = None

            logger.info("索引已从 %s 加载，包含 %d 个向量", load_path, self._index.ntotal)
            return True

    @property
    def count(self) -> int:
        """有效（可检索）向量数。

        faiss 不支持原地删除，remove() 仅摘除 id 映射，底层 ntotal 不减。
        这里返回映射数（去除已删幽灵向量），才是真实可检索数量。
        """
        with self._lock:
            return len(self._id_map)

    @property
    def raw_count(self) -> int:
        """faiss 底层向量总数（含已删除未重建的幽灵向量）。"""
        with self._lock:
            return self._index.ntotal if self._index else 0
