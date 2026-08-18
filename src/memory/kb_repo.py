"""Repository for kb repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

from src.memory.index_lock import with_index_lock
from src.memory.sqlite_store import cosine_similarity

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class KbRepo:
    """Repository extracted from SQLiteStore for kb operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def check_duplicate_document(self, title: str, content: str = "",
                                  source_id: str = "", url: str = "",
                                  embedding: list[float] | None = None,
                                  similarity_threshold: float = 0.92) -> dict | None:
        """检查知识库中是否已存在重复或高度相似的文档。

        返回: {"duplicate": True, "doc": {...}, "reason": "..."} 或 {"duplicate": False}
        """
        cur = self.store.conn.cursor()

        # 1. 标题完全匹配
        cur.execute("SELECT * FROM kb_documents WHERE title = ? LIMIT 1", (title,))
        row = cur.fetchone()
        if row:
            return {"duplicate": True, "doc": dict(row), "reason": "标题完全匹配"}

        # 2. source_id 或 url 完全匹配
        if source_id:
            cur.execute("SELECT * FROM kb_documents WHERE source_id = ? LIMIT 1", (source_id,))
            row = cur.fetchone()
            if row:
                return {"duplicate": True, "doc": dict(row), "reason": "来源ID重复"}
        if url:
            cur.execute("SELECT * FROM kb_documents WHERE url = ? LIMIT 1", (url,))
            row = cur.fetchone()
            if row:
                return {"duplicate": True, "doc": dict(row), "reason": "URL重复"}

        # 3. 内容哈希匹配（如果提供了内容）
        if content:
            import hashlib
            content_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]
            cur.execute("SELECT * FROM kb_documents WHERE metadata LIKE ? LIMIT 1",
                        (f'%"content_hash": "{content_hash}"%',))
            row = cur.fetchone()
            if row:
                return {"duplicate": True, "doc": dict(row), "reason": "内容哈希匹配"}

        # 4. 向量相似度匹配（如果提供了 embedding）
        if embedding:
            cur.execute("""
                SELECT d.*, c.embedding
                FROM kb_documents d
                JOIN kb_chunks c ON d.id = c.doc_id
                WHERE c.embedding != ''
            """)
            best_sim = 0.0
            best_doc = None
            for r in cur.fetchall():
                try:
                    emb = json.loads(r["embedding"])
                    if emb:
                        sim = cosine_similarity(embedding, emb)
                        if sim > best_sim:
                            best_sim = sim
                            best_doc = dict(r)
                except Exception as e:
                    logger.debug("查重相似度计算失败: %s", e)
                    continue
            if best_sim >= similarity_threshold and best_doc:
                return {
                    "duplicate": True,
                    "doc": best_doc,
                    "reason": f"向量相似度 {(best_sim * 100):.1f}%",
                }

        return {"duplicate": False}

    def add_kb_document(self, title: str, doc_type: str, source: str,
                        source_id: str = "", url: str = "",
                        metadata: dict | None = None,
                        content: str = "",
                        embedding: list[float] | None = None) -> int:
        cur = self.store.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            now = datetime.now().isoformat()
            meta = metadata or {}
            if content:
                import hashlib
                meta["content_hash"] = hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]
            meta_str = json.dumps(meta, ensure_ascii=False) if meta else ""
            cur.execute(
                """INSERT INTO kb_documents
                   (title, doc_type, source, source_id, url, status, metadata, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                (title, doc_type, source, source_id, url, meta_str, now, now),
            )
            self.store.conn.commit()
            # 插入后 lastrowid 必然存在（自增主键），None 实际不可能。
            assert cur.lastrowid is not None
            return cur.lastrowid
        except Exception:
            logger.warning("[resilience] silent exception in add_kb_document", exc_info=True)
            self.store.conn.rollback()
            raise

    def update_kb_document(self, doc_id: int, **kwargs) -> None:
        if not kwargs:
            return
        # 字段白名单过滤，防止 SQL 注入（kwargs.keys() 不可直接拼接到 SQL）
        allowed_fields = {
            "title", "doc_type", "source", "source_id", "url", "chunk_count",
            "status", "metadata", "created_at", "updated_at",
        }
        filtered = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not filtered:
            return
        filtered["updated_at"] = datetime.now().isoformat()
        cur = self.store.conn.cursor()
        fields = ", ".join(f"{k} = ?" for k in filtered.keys())
        values = list(filtered.values()) + [doc_id]
        cur.execute(f"UPDATE kb_documents SET {fields} WHERE id = ?", values)
        self.store.conn.commit()

    def list_kb_documents(self, status: str = "", doc_type: str = "",
                          limit: int = 100, offset: int = 0,
                          q: str = "") -> tuple[list[dict], int]:
        """列出知识库文档（支持分页 + 按状态/类型/关键词筛选）。

        Args:
            status:  按状态精确过滤（空字符串=不过滤）。
            doc_type: 按 doc_type 精确过滤。
            limit:    单页条数。
            offset:   偏移。
            q:        关键词（空字符串=不过滤），匹配 title OR 任一 chunk.content
                      子串（LIKE）。子查询走 kb_chunks.doc_id，不引入额外表连接。

        Returns:
            (items, total) —— items 为当前页，total 为「过滤后」的总数（用于翻页，
            不受 limit/offset 影响；与应用顶栏 stats 的全局 total 区分）。
        """
        cur = self.store.conn.cursor()
        where = " WHERE 1=1"
        params: list = []
        if status:
            where += " AND status = ?"
            params.append(status)
        if doc_type:
            where += " AND doc_type = ?"
            params.append(doc_type)
        if q:
            # 标题或正文任一 chunk 子串匹配；走子查询避免与 chunks 表做 DISTINCT 排序
            where += (" AND (title LIKE ? "
                      "OR id IN (SELECT DISTINCT doc_id FROM kb_chunks WHERE content LIKE ?))")
            like = f"%{q}%"
            params.extend([like, like])
        # 过滤后总数
        cur.execute(f"SELECT COUNT(*) FROM kb_documents{where}", params)
        total = cur.fetchone()[0]
        query = f"SELECT * FROM kb_documents{where} ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()], total

    def get_kb_document(self, doc_id: int) -> dict | None:
        cur = self.store.conn.cursor()
        cur.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def delete_kb_document(self, doc_id: int) -> None:
        cur = self.store.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            # 先取出该文档的 chunk_id，用于同步从 faiss 向量索引移除（防止幽灵向量）
            cur.execute("SELECT id FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            chunk_ids = [row["id"] for row in cur.fetchall()]
            cur.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            cur.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
            self.store.conn.commit()
        except Exception:
            logger.warning("[resilience] silent exception in delete_kb_document", exc_info=True)
            self.store.conn.rollback()
            raise
        if chunk_ids:
            self._remove_chunks_from_index(chunk_ids)
        # WAL checkpoint: 将 WAL 日志写回主数据库，避免 WAL 无限增长
        try:
            self.store.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            logger.warning("[resilience] silent exception in delete_kb_document", exc_info=True)
        # KB 内容变更 → 自增版本号，触常驻进程重建 FAISS 索引
        self.store.bump_kb_revision()

    def delete_kb_chunk(self, chunk_id: int) -> None:
        cur = self.store.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            # 先查 chunk 所属文档，删除后同步减少 kb_documents.chunk_count
            cur.execute("SELECT doc_id FROM kb_chunks WHERE id = ?", (chunk_id,))
            row = cur.fetchone()
            doc_id = row["doc_id"] if row else None
            cur.execute("DELETE FROM kb_chunks WHERE id = ?", (chunk_id,))
            if doc_id is not None and cur.rowcount > 0:
                cur.execute(
                    "UPDATE kb_documents SET chunk_count = MAX(0, COALESCE(chunk_count, 0) - 1) "
                    "WHERE id = ?",
                    (doc_id,),
                )
            self.store.conn.commit()
        except Exception:
            logger.warning("[resilience] silent exception in delete_kb_chunk", exc_info=True)
            self.store.conn.rollback()
            raise
        self._remove_chunks_from_index([chunk_id])
        # KB 内容变更 → 自增版本号，触常驻进程重建 FAISS 索引
        self.store.bump_kb_revision()

    @with_index_lock

    def _remove_chunks_from_index(self, chunk_ids: list[int]) -> None:
        """从 faiss 向量索引中移除一批 chunk（与 SQLite 删除保持一致）。

        faiss 不支持原地删除，仅从 id 映射中摘除（search 会因映射缺失而跳过）；
        底层向量会在下次 rebuild 时真正清除。若移除后失效向量占比过高，触发一次重建。
        """
        vi = self.store._vector_index
        if vi is None:
            return
        removed = 0
        for cid in chunk_ids:
            try:
                if vi.remove(cid):
                    removed += 1
            except Exception as e:
                logger.warning("从向量索引移除 chunk %d 失败: %s", cid, e)
        if removed and getattr(vi, "index_path", None):
            try:
                vi.save(vi.index_path)
            except Exception as e:
                logger.warning("删除后保存向量索引失败: %s", e)

    def add_kb_chunks(self, doc_id: int, chunks: list[str]) -> None:
        cur = self.store.conn.cursor()
        now = datetime.now().isoformat()
        try:
            for i, content in enumerate(chunks):
                cur.execute(
                    """INSERT INTO kb_chunks (doc_id, chunk_index, content, embedding, created_at)
                       VALUES (?, ?, ?, '', ?)""",
                    (doc_id, i, content, now),
                )
            cur.execute(
                "UPDATE kb_documents SET chunk_count = ?, status = 'indexed', updated_at = ? WHERE id = ?",
                (len(chunks), now, doc_id),
            )
            self.store.conn.commit()
            # KB 内容变更 → 自增版本号，触常驻进程重建 FAISS 索引
            self.store.bump_kb_revision()
        except Exception:
            # 中途异常时回滚，避免残留部分 chunk 与 doc 状态不一致（F6）
            self.store.conn.rollback()
            raise

    def mark_chunk_retry_pending(self, chunk_id: int) -> None:
        """标记 chunk 的 embedding 需要重试。"""
        cur = self.store.conn.cursor()
        cur.execute("UPDATE kb_chunks SET retry_pending = 1 WHERE id = ?", (chunk_id,))
        self.store.conn.commit()

    @with_index_lock

    def update_chunk_embedding(self, chunk_id: int, embedding: list[float]) -> None:
        cur = self.store.conn.cursor()
        emb_str = json.dumps(embedding)
        cur.execute("UPDATE kb_chunks SET embedding = ? WHERE id = ?", (emb_str, chunk_id))
        self.store.conn.commit()

        # 同步更新 faiss 索引
        if self.store._vector_index is None and embedding:
            self.store._init_vector_index(len(embedding))
        if self.store._vector_index:
            # 维度基准以「索引实例自身的 dim」为准，store._index_dim 仅作兜底：
            # 两者可能不一致（磁盘残留索引被按新维度请求加载时），只信 store._index_dim
            # 会让守卫误判通过，进而 remove 生效、add 触发 faiss 裸 assert（F19）。
            index_dim = getattr(self.store._vector_index, "dim", 0) or self.store._index_dim
            if index_dim and len(embedding) != index_dim:
                # 维度漂移：强行 add 会抛错并破坏 DB/索引一致性（F7）。
                # 跳过索引更新，留待下次 _ensure_index_loaded 全量重建时按维度族校正。
                logger.warning(
                    "跳过向量索引更新：chunk %d embedding 维度(%d) 与索引维度(%d) 不一致",
                    chunk_id, len(embedding), index_dim,
                )
            else:
                try:
                    self.store._vector_index.remove(chunk_id)
                    self.store._vector_index.add(chunk_id, embedding)
                except Exception as e:
                    # add 失败时 remove 已生效 → 该 chunk 从索引中消失（逐条累积会
                    # 掏空索引）。丢弃整个索引，交由 _ensure_index_loaded 全量重建，
                    # 避免留下「DB 有向量、索引查不到」的静默残缺状态。
                    # 用 %r：faiss 维度断言是裸 AssertionError，str(e) 为空字符串，
                    # 只打 %s 会得到「... vector index: 」这种无法诊断的日志。
                    logger.warning(
                        "向量索引更新失败（chunk %d，已丢弃索引待全量重建）: %r",
                        chunk_id, e,
                    )
                    self.store._vector_index = None
                    self.store._index_dim = 0
        # 向量刷新（可能同计数异向量）→ 自增版本号，触常驻进程全量重建索引
        self.store.bump_kb_revision()

    def list_kb_chunks(self, doc_id: int) -> list[dict]:
        cur = self.store.conn.cursor()
        cur.execute("SELECT * FROM kb_chunks WHERE doc_id = ? ORDER BY chunk_index", (doc_id,))
        return [dict(row) for row in cur.fetchall()]

    @with_index_lock

    def search_kb(self, query_embedding: list[float], top_k: int = 5,
                  doc_type: str = "", min_similarity: float = 0.0,
                  query_text: str = "") -> list[dict]:
        if not query_embedding or not any(query_embedding):
            # 空向量或全零向量：归一化后仍是零向量，FAISS 会对所有 chunk 返回
            # sim=0.0，误当成"匹配"返回不相关结果；直接短路返回空。
            return []

        # 尝试使用 faiss 索引加速检索
        # 已加载的索引需校验是否与 DB 同步：常驻 bot 进程不经 web.dependencies
        # 的 COUNT 失效机制，新文档经 web 写入 SQLite 后，bot 的 faiss 内存索引
        # 仍是启动快照，必须失效重建才能检索到（否则"部分文档不生效"）。
        if self.store._vector_index is None:
            self.store._ensure_index_loaded()
        elif not self.store._index_in_sync():
            logger.info(
                "faiss 索引与 DB 不同步（索引 %d 向量 ≠ DB 已索引 chunk 数），触发全量重建",
                self.store._vector_index.count,
            )
            self.store._vector_index = None
            self.store._ensure_index_loaded()

        vi = self.store._vector_index
        if vi and vi.count > 0 and len(query_embedding) == vi.dim:
            try:
                results = vi.search(
                    query_embedding, top_k=top_k * (10 if (min_similarity > 0 or doc_type) else 2))
                if results:
                    chunk_ids = [r[0] for r in results]
                    # 批量查询 chunk 详情
                    placeholders = ",".join(["?"] * len(chunk_ids))
                    cur = self.store.conn.cursor()
                    query = f"""
                        SELECT c.id, c.doc_id, c.chunk_index, c.content,
                               d.title, d.doc_type, d.source, d.url
                        FROM kb_chunks c
                        JOIN kb_documents d ON c.doc_id = d.id
                        WHERE c.id IN ({placeholders})
                    """
                    cur.execute(query, chunk_ids)
                    rows = {row["id"]: dict(row) for row in cur.fetchall()}

                    output = []
                    for chunk_id, sim in results:
                        row = rows.get(chunk_id)
                        if row:
                            if doc_type and row.get("doc_type") != doc_type:
                                continue
                            output.append({
                                "chunk_id": row["id"],
                                "doc_id": row["doc_id"],
                                "chunk_index": row["chunk_index"],
                                "content": row["content"],
                                "title": row["title"],
                                "doc_type": row["doc_type"],
                                "source": row["source"],
                                "url": row["url"],
                                "similarity": sim,
                            })
                    if min_similarity > 0:
                        # 在重排/截断前先按原始相似度过滤，避免高相似度结果被关键词分
                        # 压到 top_k 之外而丢失（重排后的 final_score 含关键词权重）。
                        output = [r for r in output if r["similarity"] >= min_similarity]
                    if query_text and len(output) > 1:
                        try:
                            from src.memory.reranker import SimpleReranker
                            output = SimpleReranker().rerank(query_text, output,
                                                            top_k=max(top_k, len(output)))
                        except Exception as e:
                            logger.debug("[RAG] rerank 失败，跳过: %s", e)
                    return output[:top_k]
            except Exception as e:
                # 用 %r：faiss 维度断言是裸 AssertionError，str(e) 为空字符串，
                # 只打 %s 会得到「FAISS 搜索失败: ，降级...」这种无法诊断的日志。
                # 并丢弃索引：维度错配等结构性问题不会自愈，若不失效则每次查询都
                # 永久降级为全表暴力搜索（结果正确但 FAISS 形同废弃）。
                logger.warning("FAISS 搜索失败（已丢弃索引待重建）: %r，本次降级暴力搜索", e)
                self.store._vector_index = None
                self.store._index_dim = 0

        # 兜底：全表扫描
        cur = self.store.conn.cursor()
        query = """
            SELECT c.id, c.doc_id, c.chunk_index, c.content, c.embedding,
                   d.title, d.doc_type, d.source, d.url
            FROM kb_chunks c
            JOIN kb_documents d ON c.doc_id = d.id
            WHERE c.embedding != ''
        """
        params = []
        if doc_type:
            query += " AND d.doc_type = ?"
            params.append(doc_type)
        cur.execute(query, params)
        rows = cur.fetchall()

        results = []
        for row in rows:
            try:
                emb = json.loads(row["embedding"])
                if emb:
                    sim = cosine_similarity(query_embedding, emb)
                    results.append({
                        "chunk_id": row["id"],
                        "doc_id": row["doc_id"],
                        "chunk_index": row["chunk_index"],
                        "content": row["content"],
                        "title": row["title"],
                        "doc_type": row["doc_type"],
                        "source": row["source"],
                        "url": row["url"],
                        "similarity": sim,
                    })
            except Exception as e:
                logger.debug("知识库搜索单条记录处理失败: %s", e)
                continue
        results.sort(key=lambda x: x["similarity"], reverse=True)
        if query_text and len(results) > 1:
            try:
                from src.memory.reranker import SimpleReranker
                results = SimpleReranker().rerank(query_text, results,
                                                 top_k=max(top_k, len(results)))
            except Exception as e:
                logger.debug("[RAG] rerank 失败，跳过: %s", e)
        if min_similarity > 0:
            results = [r for r in results if r["similarity"] >= min_similarity]
        return results[:top_k]

    def kb_stats(self) -> dict:
        cur = self.store.conn.cursor()
        cur.execute("SELECT COUNT(*) as total FROM kb_documents")
        total_docs = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as total FROM kb_chunks")
        total_chunks = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as total FROM kb_chunks WHERE embedding IS NOT NULL AND embedding != ''")
        indexed_chunks = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(DISTINCT doc_id) FROM kb_chunks WHERE embedding IS NOT NULL AND embedding != ''")
        indexed_docs = cur.fetchone()[0] or 0
        cur.execute("SELECT doc_type, COUNT(*) as cnt FROM kb_documents GROUP BY doc_type ORDER BY cnt DESC")
        by_type = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT source, COUNT(*) as cnt FROM kb_documents GROUP BY source ORDER BY cnt DESC")
        by_source = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT COUNT(*) as total FROM memories")
        total_memories = cur.fetchone()["total"]
        return {
            "total_documents": total_docs,
            "total_chunks": total_chunks,
            "indexed_docs": indexed_docs,
            "indexed_chunks": indexed_chunks,
            "by_type": by_type,
            "by_source": by_source,
            "total_memories": total_memories,
        }

    def count_kb_documents(self) -> int:
        """知识库文档总数（供状态面板概览）。"""
        cur = self.store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM kb_documents")
        return cur.fetchone()[0]

    def count_embedded_chunks(self) -> int:
        """已生成向量（embedding 非空）的分块数——向量索引失效校验的廉价基准。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM kb_chunks WHERE embedding != '' AND embedding IS NOT NULL"
        )
        return cur.fetchone()[0]

    def search_kb_by_keyword(self, query: str, top_k: int = 5) -> list[dict]:
        """全文检索知识库（兜底方案，当向量检索不可用时使用）。"""
        if not query or not query.strip():
            return []

        # 【H7修复】中文关键词需按字符拆分（而非整句作为一个 token），
        # 否则 "钉钉文档管理" 只能匹配完整子串，无法命中只含"钉钉"的文档。
        # 英文保留按单词分词（\w+），中文按每 2-4 字的 bigram/trigram 拆分。
        keywords = []
        for part in re.findall(r'[a-zA-Z0-9_]+|[\u4e00-\u9fff]+', query.lower()):
            if re.match(r'^[a-zA-Z0-9_]+$', part):
                keywords.append(part)
            else:
                # 中文：按 2-gram 拆分（覆盖最常见的中文词长）
                if len(part) >= 2:
                    for i in range(len(part) - 1):
                        keywords.append(part[i:i+2])
                else:
                    keywords.append(part)
        if not keywords:
            return []

        # 构建 WHERE：content 或 title 包含任意关键词
        content_conds = " OR ".join(["c.content LIKE ?"] * len(keywords))
        title_conds = " OR ".join(["d.title LIKE ?"] * len(keywords))
        where_clause = f"({content_conds}) OR ({title_conds})"
        # 参数：content 关键词一套 + title 关键词一套
        params = [f"%{kw}%" for kw in keywords] * 2

        # 【H6修复】SQL LIMIT 在 Python 评分之前截断，导致高分结果可能被丢弃。
        # 改为 LIMIT top_k * 5 扩大候选集，评分后取 top_k。
        sql_limit = top_k * 5

        query_sql = f"""
            SELECT c.id, c.doc_id, c.chunk_index, c.content,
                   d.title, d.doc_type, d.source, d.url
            FROM kb_chunks c
            JOIN kb_documents d ON c.doc_id = d.id
            WHERE {where_clause}
            LIMIT ?
        """

        try:
            # 游标获取也必须在 try 内：连接已关闭/失效时 conn.cursor() 本身就会抛
            # ProgrammingError，放在 try 外会让本该「兜底返回空」的检索直接炸穿调用方。
            cur = self.store.conn.cursor()
            cur.execute(query_sql, params + [sql_limit])
            rows = cur.fetchall()

            # 归一化分母：每个关键词最多贡献 content(2) + title(1) = 3 分。
            # 【根源修复】原始命中计数是无上界整数（3、5、8...），会顺着
            # kb_search → citations → 引文页脚一路漏到用户可见的「相关度300%」。
            # 此处按满分归一化到 [0,1]，与向量检索的余弦相似度同域可比，
            # 下游所有 score/similarity 消费方（min_similarity 门槛、引文
            # 阈值、百分比展示）无需再区分检索来源。排序不受影响（保序缩放）。
            max_possible = 3 * len(keywords)

            results = []
            for row in rows:
                content_lower = row["content"].lower()
                title_lower = row["title"].lower()
                # 评分：content 命中权重2，title 命中权重1
                score = 0
                for kw in keywords:
                    if kw in content_lower:
                        score += 2
                    if kw in title_lower:
                        score += 1

                results.append({
                    "chunk_id": row["id"],
                    "doc_id": row["doc_id"],
                    "chunk_index": row["chunk_index"],
                    "content": row["content"],
                    "title": row["title"],
                    "doc_type": row["doc_type"],
                    "source": row["source"],
                    "url": row["url"],
                    "score": round(score / max_possible, 4) if max_possible else 0.0,
                })

            # 按评分降序排列，截取 top_k
            results.sort(key=lambda x: x["score"], reverse=True)
            results = results[:top_k]
            logger.info("[RAG] 全文检索返回 %d 条结果", len(results))
            return results

        except Exception as e:
            logger.error("[RAG] 全文检索失败: %s", e)
            return []

