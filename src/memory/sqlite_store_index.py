"""SQLiteStore 向量索引 mixin（FAISS 加载/增量/重建/KB 元数据）。

拆分自 sqlite_store.py；VectorIndex/get_config 保持函数内延迟导入（防循环）。
"""
from __future__ import annotations
from .sqlite_store_mixins_base import SQLiteStoreBase

import json
import logging
from pathlib import Path

from src.memory.index_lock import with_index_lock

logger = logging.getLogger(__name__)
_with_index_lock = with_index_lock  # 兼容别名


class SQLiteStoreIndexMixin(SQLiteStoreBase):
    def _vi_kwargs(self) -> dict:
        """F18: 从全局配置读取向量索引参数，缺省回落安全默认。

        全局配置单例（shared_state.get_config）由主进程发布；Web 进程或独立测试
        未发布时为 None，此时回落默认（flat / ef=64 / ratio=0.3 / cache=True）。
        """
        try:
            from src.shared_state import get_config
            cfg = get_config()
        except Exception:
            cfg = None
        mem = getattr(cfg, "memory", None) if cfg is not None else None
        if mem is None:
            return {"index_type": "flat", "hnsw_ef": 64,
                    "phantom_rebuild_ratio": 0.3, "cache_embeddings": True}
        return {
            "index_type": getattr(mem, "vector_index_type", "flat"),
            "hnsw_ef": getattr(mem, "vector_index_hnsw_ef", 64),
            "phantom_rebuild_ratio": getattr(mem, "vector_phantom_rebuild_ratio", 0.3),
            "cache_embeddings": getattr(mem, "vector_cache_embeddings", True),
        }

    def _init_vector_index(self, dim: int) -> None:
        """初始化 faiss 向量索引。"""
        if self._vector_index is not None and self._index_dim == dim:
            return
        try:
            from src.memory.vector_index import VectorIndex
            index_path = str(Path(self.db_path).with_suffix(".faiss"))
            self._vector_index = VectorIndex(dim, index_path=index_path, **self._vi_kwargs())
            if not self._vector_index.load():
                logger.info("没有找到现有的faiss索引，重新开始")
            self._index_dim = dim
            self._index_revision = self.get_kb_revision()
        except Exception as e:
            logger.warning("初始化向量索引失败：%s", e)
            self._vector_index = None

    def _index_in_sync(self) -> bool:
        """faiss 索引已加载时，校验其是否与 DB 同步。

        同步需同时满足：
        1. 已索引 chunk 数量一致（增删 chunk）
        2. KB 版本号一致（kb_meta.revision；覆盖「同计数、向量被重索引」场景）

        任一不满足说明其它进程/后续写入改动了 KB，旧索引已陈旧需重建。
        校验失败（异常）时保守返回 True（不重建），避免误丢可用索引。
        """
        if self._vector_index is None:
            return True
        try:
            cur = self.conn.cursor()
            if self._index_dim:
                # 只统计与索引同维度的已索引 chunk：库里残留的异维向量（如旧模型 1024 维）
                # 不应算入同步计数，否则 db_count 恒大于 index.count → 每次查询都误判失同步、
                # 触发全量重建（H1）。
                cur.execute(
                    "SELECT COUNT(*) FROM kb_chunks "
                    "WHERE embedding != '' AND embedding IS NOT NULL "
                    "AND json_array_length(embedding) = ?",
                    (self._index_dim,),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM kb_chunks "
                    "WHERE embedding != '' AND embedding IS NOT NULL"
                )
            db_count = cur.fetchone()[0]
            if db_count != self._vector_index.count:
                return False
            db_rev = self.get_kb_revision()
            return db_rev == self._index_revision
        except Exception:
            logger.warning("[resilience] silent exception in _index_in_sync", exc_info=True)
            return True

    def _index_count_matches_db(self) -> bool:
        """仅比对已索引 chunk 数量（不计版本号），用于区分「增删」vs「重索引」。"""
        if self._vector_index is None:
            return False
        try:
            cur = self.conn.cursor()
            if self._index_dim:
                cur.execute(
                    "SELECT COUNT(*) FROM kb_chunks "
                    "WHERE embedding != '' AND embedding IS NOT NULL "
                    "AND json_array_length(embedding) = ?",
                    (self._index_dim,),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM kb_chunks "
                    "WHERE embedding != '' AND embedding IS NOT NULL"
                )
            db_count = cur.fetchone()[0]
            return db_count == self._vector_index.count
        except Exception as _exc:
            logger.warning(f"_index_count_matches_db: swallowed exception: {_exc}")
            return False

    def _ensure_kb_meta(self) -> None:
        """惰性建表并初始化 kb_meta.revision（KB 版本号）。"""
        cur = self.conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        cur.execute(
            "INSERT OR IGNORE INTO kb_meta (key, value) VALUES ('revision', '0')"
        )
        self.conn.commit()

    def get_kb_revision(self) -> int:
        """读取当前 KB 版本号（每次 KB 写入/重索引自增）。异常时保守返回 0。"""
        try:
            self._ensure_kb_meta()
            cur = self.conn.cursor()
            cur.execute("SELECT value FROM kb_meta WHERE key = 'revision'")
            row = cur.fetchone()
            return int(row["value"]) if row else 0
        except Exception:
            logger.warning("[resilience] get_kb_revision failed", exc_info=True)
            return 0

    def bump_kb_revision(self) -> int:
        """KB 内容（文档/分块/向量）变更后自增版本号。

        常驻 bot 进程据此判定其内存 FAISS 索引已陈旧并触发重建，
        解决「后台能搜到、机器人搜不到」的索引陈旧问题。
        返回自增后的版本号；异常时保守返回 0。
        """
        try:
            self._ensure_kb_meta()
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE kb_meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                "WHERE key = 'revision'"
            )
            if cur.rowcount == 0:
                cur.execute(
                    "INSERT INTO kb_meta (key, value) VALUES ('revision', '1')"
                )
            self.conn.commit()
            cur.execute("SELECT value FROM kb_meta WHERE key = 'revision'")
            row = cur.fetchone()
            return int(row["value"]) if row else 1
        except Exception:
            logger.warning("[resilience] bump_kb_revision failed", exc_info=True)
            return 0

    @_with_index_lock
    def _ensure_index_loaded(self) -> None:
        """确保向量索引已加载并与 DB 同步。

        同步判据（任一不满足即视为不同步，触发重建）：
        1. 已索引 chunk 数量一致（增删 chunk）
        2. KB 版本号一致（kb_meta.revision；覆盖「同计数、向量被重索引」场景）

        重建策略：
        - 数量不一致（增删）→ 增量补齐（保留已有向量）
        - 数量一致但版本号变化（重索引刷新向量）→ 全量重建（丢弃旧向量）

        优化路径（按优先级）：
        1. 已加载且同步 → 零开销返回
        2. 未加载 → 尝试从磁盘 .faiss 文件加载；加载成功则仅增量添加 DB 新增 chunk
        3. 磁盘加载失败 / 维度不匹配 → 全量 DB 重建（兜底）
        """
        # ── 路径 1：已加载 ──
        if self._vector_index is not None:
            if self._index_in_sync():
                return
            # 不同步：区分「增删」与「重索引」
            if self._index_count_matches_db():
                # 计数一致但版本号变化 → 向量被重索引（同计数异向量）。
                # 直接对 DB 全量重建（DB 为权威源），不信任可能陈旧的磁盘索引。
                logger.info(
                    "[FAISS] 索引版本号变化（chunk 数未变）→ 全量重建以刷新向量"
                )
                self._vector_index = None
                self._index_dim = 0
                self._full_rebuild_from_db()
                return
            elif self._incremental_add_new_chunks():
                return
            else:
                # 增量失败（如维度变化）→ 丢弃，走全量重建
                self._vector_index = None
                self._index_dim = 0

        # ── 路径 2：尝试磁盘加载 + 增量 ──
        best_dim = self._get_best_embedding_dim()
        if best_dim and self._try_load_from_disk(best_dim):
            if self._incremental_add_new_chunks():
                return
            # 增量失败，丢弃后走全量
            self._vector_index = None
            self._index_dim = 0

        # ── 路径 3：全量 DB 重建（兜底） ──
        self._full_rebuild_from_db()

    def _get_best_embedding_dim(self) -> int | None:
        """从 DB 查询最佳向量维度（选最多 chunk 所在的维度族，平局优先高维）。"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT embedding FROM kb_chunks "
                "WHERE embedding != '' AND embedding IS NOT NULL LIMIT 2000"
            )
            dim_counts: dict[int, int] = {}
            for row in cur.fetchall():
                try:
                    emb = json.loads(row["embedding"])
                except (json.JSONDecodeError, ValueError, TypeError) as _exc:
                    logger.debug(f"_get_best_embedding_dim: swallowed exception: {_exc}")
                    continue
                if emb:
                    dim_counts[len(emb)] = dim_counts.get(len(emb), 0) + 1
            if not dim_counts:
                return None
            return max(dim_counts.keys(), key=lambda d: (dim_counts[d], d))
        except Exception:
            logger.warning("[resilience] _get_best_embedding_dim failed", exc_info=True)
            return None

    def _try_load_from_disk(self, dim: int) -> bool:
        """尝试从 .faiss 磁盘文件加载索引。成功返回 True。"""
        try:
            from src.memory.vector_index import VectorIndex
            index_path = str(Path(self.db_path).with_suffix(".faiss"))
            vi = VectorIndex(dim, index_path=index_path, **self._vi_kwargs())
            if vi.load():
                self._vector_index = vi
                self._index_dim = dim
                self._index_revision = self.get_kb_revision()
                logger.info(
                    "FAISS 索引从磁盘加载：%d 向量，dim=%d", vi.count, dim
                )
                return True
        except Exception as e:
            logger.warning("从磁盘加载 FAISS 索引失败: %s", e)
        return False

    def _incremental_add_new_chunks(self) -> bool:
        """增量添加 DB 中有但索引中没有的 chunk。成功返回 True。

        失败时（如维度不匹配）返回 False，调用方应降级到全量重建。
        """
        vi = self._vector_index
        if vi is None:
            return False
        # 进入同步流程：记录当前 DB 版本号（重建/增量后索引即对应此版本）
        self._index_revision = self.get_kb_revision()
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT id, embedding FROM kb_chunks "
                "WHERE embedding != '' AND embedding IS NOT NULL "
                "ORDER BY id"
            )
            new_items: list[tuple[int, list[float]]] = []
            bad_json = 0
            dim_mismatch = 0
            for row in cur.fetchall():
                cid = row["id"]
                if cid in vi._reverse_map:
                    continue  # 已在索引中
                try:
                    emb = json.loads(row["embedding"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    bad_json += 1
                    continue
                if not emb:
                    continue
                if len(emb) != self._index_dim:
                    dim_mismatch += 1
                    continue
                new_items.append((cid, emb))

            if not new_items:
                if bad_json or dim_mismatch:
                    logger.debug(
                        "FAISS 增量更新：无新增有效向量（坏JSON %d, 维度不匹配 %d）",
                        bad_json, dim_mismatch,
                    )
                # 无新增，但仍需确认同步状态（可能因 remove 导致不同步）
                if not self._index_in_sync():
                    # 不同步且无新增 → 说明有 chunk 被删除但索引未感知。
                    # 当前索引的 _reverse_map 已通过 remove 清理，count 应匹配。
                    # 如果不匹配（极端情况），回退全量重建。
                    return False
                return True

            vi.add_batch(new_items)
            try:
                vi.save()
                logger.info(
                    "FAISS 增量更新：+%d 向量（坏JSON %d, 维度不匹配 %d），总计 %d",
                    len(new_items), bad_json, dim_mismatch, vi.count,
                )
            except Exception as save_err:
                logger.warning("FAISS 增量后保存失败: %s", save_err)
            return True

        except Exception as e:
            logger.warning("FAISS 增量更新失败: %s", e)
            return False

    def _full_rebuild_from_db(self) -> None:
        """从 DB 全量重建 FAISS 索引（兜底路径）。"""
        try:
            from src.memory.vector_index import VectorIndex
            cur = self.conn.cursor()
            # 获取所有已索引的 chunks（按 id 排序，保证重建结果可复现）
            cur.execute("""
                SELECT id, embedding FROM kb_chunks
                WHERE embedding != '' AND embedding IS NOT NULL
                ORDER BY id
            """)
            # 按向量维度分组：规避「首行维度基准」陷阱——旧模型 768 维残留若恰好
            # 排在最前，会让当前 1024 维全被跳过/建错索引（F3）。
            dim_groups: dict[int, list[tuple[int, list[float]]]] = {}
            bad_json = 0
            for row in cur.fetchall():
                try:
                    emb = json.loads(row["embedding"])
                except (json.JSONDecodeError, ValueError, TypeError):
                    # 单条坏 JSON 不应拖垮整库索引（F4）：跳过该条并告警，而非置空全库。
                    logger.warning("跳过索引加载：chunk %d embedding JSON 解析失败", row["id"])
                    bad_json += 1
                    continue
                if not emb:
                    continue
                dim_groups.setdefault(len(emb), []).append((row["id"], emb))
            if not dim_groups:
                if bad_json:
                    logger.warning("向量索引无可加载向量（%d 条 embedding JSON 损坏）", bad_json)
                return
            best_dim = max(dim_groups.keys(), key=lambda d: (len(dim_groups[d]), d))
            items = dim_groups[best_dim]
            skipped = sum(len(v) for d, v in dim_groups.items() if d != best_dim) + bad_json
            index_path = str(Path(self.db_path).with_suffix(".faiss"))
            self._vector_index = VectorIndex(best_dim, index_path=index_path, **self._vi_kwargs())
            self._vector_index.add_batch(items)
            self._index_dim = best_dim
            try:
                self._vector_index.save()
                saved_note = "，已持久化"
            except Exception as save_err:
                logger.warning("向量索引重建后保存失败: %s", save_err)
                saved_note = "，保存失败（下次启动将重建）"
            logger.info(
                "Vector index rebuilt: dim=%d, %d vectors (skipped %d 异维/坏JSON), chosen-from %d dim-family(ies)%s",
                best_dim, len(items), skipped, len(dim_groups), saved_note,
            )
            self._index_revision = self.get_kb_revision()
        except Exception as e:
            logger.warning("Failed to load vector index: %s", e)
            self._vector_index = None
