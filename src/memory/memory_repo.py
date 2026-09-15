"""Repository for memory repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from src.memory.sqlite_store import cosine_similarity

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

# H4-2026-08-08：记忆召回/去重先按 created_at 倒序截断到最近一批候选，再在内存算余弦，
# 避免记忆量很大时全表 fetchall + 逐行 cosine 拖慢热路径。记忆表通常很小，cap 内行为不变；
# 超大表退化为"近期优先"（符合个人记忆场景）。idx_memories_created 已覆盖 ORDER BY。
_MEMORY_CANDIDATE_CAP = 500

# 同主题"最新优先"：写入时把语义相近的旧 active 记忆标为 superseded 的相似度阈值。
# 仅当新记忆与旧记忆余弦相似度 ≥ 此值（BGE 下≈同结论的不同表述）才作废旧版。
_MEMORY_SUPERSEDE_THRESHOLD = 0.85
# 召回排序的时效权重：给较新记忆一个微弱加成，打破相近相似度时的平局。
# 仅影响排序，不改变返回给下游的 similarity（阈值语义不变）。
_MEMORY_RECENCY_HALF_LIFE_DAYS = 60.0
_MEMORY_RECENCY_BONUS = 0.08


def _recency_weight(created_at_iso: Optional[str]) -> float:
    """时效权重 ∈ (0, 1]，越新越接近 1。无法解析时回落 0.5。"""
    if not created_at_iso:
        return 0.5
    try:
        dt = datetime.fromisoformat(created_at_iso)
    except (ValueError, TypeError):
        return 0.5
    age_days = (datetime.now() - dt).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return float(math.exp(-age_days / _MEMORY_RECENCY_HALF_LIFE_DAYS))


class MemoryRepo:
    """Repository extracted from SQLiteStore for memory operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def recall_memory(self, query_embedding: list[float], top_k: int = 5,
                      chat_id: str = "", query_text: str = "",
                      sender_id: str = "", min_similarity: float = 0.0) -> list[dict]:
        if not query_embedding:
            return []

        cur = self.store.conn.cursor()
        if sender_id:
            # 严格点对点：仅返回「公共记忆」+「该 sender_id 本人的个人记忆」。
            # 第三方（sender_id 不匹配）的个人记忆绝不被召回 ——
            # 满足「个人记忆是我和对方私有的，绝不能出现在第三方」。
            # 仅召回 status='active'（被更新的结论标为 superseded 后不再出现）。
            cur.execute(
                "SELECT id, content, source, chat_id, sender_id, sender_name, embedding, created_at, scope "
                "FROM memories WHERE ((scope = 'public') OR (sender_id = ? AND (scope = 'personal' OR scope IS NULL))) "
                "AND (status = 'active' OR status IS NULL) "
                "ORDER BY created_at DESC LIMIT ?",
                (sender_id, _MEMORY_CANDIDATE_CAP),
            )
        else:
            # 安全兜底：缺少 sender_id（异常/系统消息/未来新调用方）时，
            # 绝不返回任何个人记忆，只返回公共记忆。防止第三方（sender_id 缺失或错位）
            # 误召回他人私聊记忆，造成隐私泄露。chat_id 不足以解锁个人记忆。
            cur.execute(
                "SELECT id, content, source, chat_id, sender_id, sender_name, embedding, created_at, scope "
                "FROM memories WHERE scope = 'public' AND (status = 'active' OR status IS NULL) "
                "ORDER BY created_at DESC LIMIT ?",
                (_MEMORY_CANDIDATE_CAP,),
            )

        rows = cur.fetchall()
        results = []
        for row in rows:
            try:
                emb_str = row["embedding"] or "[]"
                embedding = json.loads(emb_str)
                if embedding:
                    sim = cosine_similarity(query_embedding, embedding)
                    # 时效加权仅影响排序（_rank）：越新的记忆在相近相似度时越靠前，
                    # 但返回的 similarity 保持原始余弦，下游阈值语义不变。
                    results.append({
                        "id": row["id"],
                        "content": row["content"],
                        "source": row["source"],
                        "chat_id": row["chat_id"],
                        "scope": row["scope"] or "personal",
                        "similarity": sim,
                        "created_at": row["created_at"],
                        "_rank": sim + _MEMORY_RECENCY_BONUS * _recency_weight(row["created_at"]),
                    })
            except Exception as e:
                logger.debug("向量搜索单条记录处理失败: %s", e)
                continue

        # 先按（相似度 + 时效）粗排，取 top_k * 2
        results.sort(key=lambda x: x["_rank"], reverse=True)
        # 过滤掉相似度低于 min_similarity 的结果（在截取 top_k 之前）
        if min_similarity > 0:
            results = [r for r in results if r["similarity"] >= min_similarity]
        candidates = results[:top_k * 2]

        # 如果有查询文本，进行重排序
        if query_text and candidates:
            try:
                from src.memory.reranker import SimpleReranker
                reranker = SimpleReranker()
                candidates = reranker.rerank(query_text, candidates, top_k=top_k)
            except Exception as e:
                logger.warning("重排序失败: %s，降级使用向量相似度", e)
                candidates = candidates[:top_k]
        else:
            candidates = candidates[:top_k]

        for c in candidates:
            c.pop("_rank", None)
        return candidates

    def get_all_memories(self, chat_id: str = "") -> list[dict]:
        cur = self.store.conn.cursor()
        if chat_id:
            cur.execute("SELECT * FROM memories WHERE chat_id = ? ORDER BY created_at DESC", (chat_id,))
        else:
            cur.execute("SELECT * FROM memories ORDER BY created_at DESC")
        rows = cur.fetchall()
        return [dict(row) for row in rows]

    def _build_memories_where(self, object_type: str, sender: str, keyword: str,
                              chat_id: str, scope: str) -> tuple[str, list]:
        """构造记忆筛选的 WHERE 子句与前缀参数。

        返回 (where_sql, params)：where_sql 含前导 " WHERE "，无筛选时为空串。
        范围(scope)语义：public=显式公共；personal=个人(含历史未标注 NULL)。
        """
        where = []
        params: list = []
        if scope and scope != "all":
            if scope == "public":
                where.append("m.scope = 'public'")
            elif scope == "personal":
                where.append("(m.scope = 'personal' OR m.scope IS NULL)")
        if object_type and object_type != "all":
            if object_type == "person":
                where.append("c.chat_type = 'single'")
            elif object_type == "group":
                where.append("c.chat_type = 'group'")
            elif object_type == "other":
                where.append("(c.chat_type IS NULL OR (c.chat_type <> 'single' AND c.chat_type <> 'group'))")
        if sender:
            where.append("(m.sender_name LIKE ? OR m.sender_id LIKE ?)")
            params.extend([f"%{sender}%", f"%{sender}%"])
        if keyword:
            where.append("m.content LIKE ?")
            params.append(f"%{keyword}%")
        if chat_id:
            where.append("m.chat_id = ?")
            params.append(chat_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        return where_sql, params

    def get_memories_filtered(self, object_type: str = "all", sender: str = "",
                             keyword: str = "", chat_id: str = "", limit: int = 200,
                             offset: int = 0, scope: str = "all") -> list[dict]:
        """按对象类型/具体人/关键词/范围(scope)筛选记忆（支持分页 offset）。

        object_type 通过 chat_id LEFT JOIN conversations 的 chat_type 动态推断：
        - single -> person（人/单聊）
        - group  -> group（群）
        - 其它/无会话 -> other（手动添加或其它类型消息）
        无需为 memories 表新增列，历史数据即生即效。

        scope: 'all'（全部）/ 'public'（公共记忆）/ 'personal'（个人记忆，含未标注的历史数据）。
        offset: 跳过的记录数，用于分页；limit: 单页上限。
        """
        cur = self.store.conn.cursor()
        where_sql, params = self._build_memories_where(
            object_type, sender, keyword, chat_id, scope)
        sql = (
            "SELECT m.*, c.chat_type AS conv_chat_type, c.chat_name AS conv_chat_name "
            "FROM memories m "
            "LEFT JOIN conversations c ON m.chat_id = c.chat_id"
            + where_sql +
            " ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
        )
        cur.execute(sql, params + [limit, max(0, int(offset))])
        rows = cur.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            ct = d.get("conv_chat_type")
            if ct == "single":
                obj_type = "person"
            elif ct == "group":
                obj_type = "group"
            else:
                obj_type = "other"
            d["object_type"] = obj_type
            d["chat_name"] = d.get("conv_chat_name") or ""
            d.pop("conv_chat_type", None)
            d.pop("conv_chat_name", None)
            result.append(d)
        return result

    def count_memories_filtered(self, object_type: str = "all", sender: str = "",
                                keyword: str = "", chat_id: str = "",
                                scope: str = "all") -> int:
        """返回与筛选条件匹配的记忆总数，用于分页 total。"""
        cur = self.store.conn.cursor()
        where_sql, params = self._build_memories_where(
            object_type, sender, keyword, chat_id, scope)
        sql = (
            "SELECT COUNT(*) AS cnt FROM memories m "
            "LEFT JOIN conversations c ON m.chat_id = c.chat_id"
            + where_sql
        )
        cur.execute(sql, params)
        row = cur.fetchone()
        if not row:
            return 0
        return int(row["cnt"] or 0)

    def get_memory_facets(self) -> dict:
        """返回记忆筛选所需的 facets：对象类型计数 + 范围(scope)计数 + 去重的人列表。"""
        cur = self.store.conn.cursor()
        cur.execute(
            "SELECT CASE WHEN c.chat_type = 'single' THEN 'person' "
            "WHEN c.chat_type = 'group' THEN 'group' ELSE 'other' END AS obj_type, "
            "COUNT(*) AS cnt "
            "FROM memories m LEFT JOIN conversations c ON m.chat_id = c.chat_id "
            "GROUP BY obj_type"
        )
        type_counts = {row["obj_type"]: row["cnt"] for row in cur.fetchall()}
        # 范围计数：public 明确标记；personal 含未标注(NULL)的历史数据
        cur.execute(
            "SELECT CASE WHEN scope = 'public' THEN 'public' ELSE 'personal' END AS sc, "
            "COUNT(*) AS cnt FROM memories GROUP BY sc"
        )
        scope_counts = {row["sc"]: row["cnt"] for row in cur.fetchall()}
        cur.execute(
            "SELECT sender_id, sender_name, COUNT(*) AS cnt FROM memories "
            "WHERE sender_id IS NOT NULL AND sender_id <> '' "
            "GROUP BY sender_id, sender_name ORDER BY cnt DESC"
        )
        people = [
            {"sender_id": row["sender_id"], "sender_name": row["sender_name"], "count": row["cnt"]}
            for row in cur.fetchall()
        ]
        return {
            "object_types": [
                {"value": "person", "label": "人", "count": type_counts.get("person", 0)},
                {"value": "group", "label": "群", "count": type_counts.get("group", 0)},
                {"value": "other", "label": "其他", "count": type_counts.get("other", 0)},
            ],
            "scopes": [
                {"value": "public", "label": "公共", "count": scope_counts.get("public", 0)},
                {"value": "personal", "label": "个人", "count": scope_counts.get("personal", 0)},
            ],
            "people": people,
        }

    def delete_memory(self, mem_id: int) -> None:
        cur = self.store.conn.cursor()
        cur.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        self.store.conn.commit()

    def update_memory(self, mem_id: int, content: str | None = None,
                      scope: str | None = None) -> None:
        """按需更新单条记忆的正文与可见范围（传 None 的字段保持不变）。

        scope 仅接受 'public' / 'personal'，其他取值忽略（不写库）。
        """
        cur = self.store.conn.cursor()
        if content is not None:
            cur.execute("UPDATE memories SET content = ? WHERE id = ?", (content, mem_id))
        if scope in ("public", "personal"):
            cur.execute("UPDATE memories SET scope = ? WHERE id = ?", (scope, mem_id))
        self.store.conn.commit()

    def count_memories(self) -> int:
        """记忆总条数（供状态面板概览）。"""
        cur = self.store.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM memories")
        return cur.fetchone()[0]

    # ============ KB Documents ============

    def check_memory_duplicate(self, content: str, embedding_client=None, similarity_threshold: float = 0.85,
                               sender_id: str = "", scope: str = "personal") -> bool:
        """检查是否已存在「内容完全相同」的记忆（防止逐字重复保存）。

        注意：语义相近但表述不同的记忆不再在此跳过——交由 ``save_memory`` 写入后
        通过 ``_supersede_similar_memories`` 把旧版标为 superseded（最新优先），
        避免「新结论被语义去重拦截、旧结论被保留」的反直觉行为。

        去重范围按 scope 区分：
        - public（公共记忆）：全局唯一，跨所有人比对；
        - personal（个人记忆）：在同一发送者范围内比对，同时若已存在相同内容的
          公共记忆也判为重复。

        异常处理：查询失败时保守判定为重复（返回 True），避免因检查失败导致重复入库。
        """
        cur = self.store.conn.cursor()
        # 1. 完全匹配（逐字相同）
        if scope == "public":
            cur.execute("SELECT id FROM memories WHERE content = ? AND scope = 'public' LIMIT 1", (content,))
        else:
            cur.execute(
                "SELECT id FROM memories WHERE content = ? AND "
                "((sender_id = ? AND (scope = 'personal' OR scope IS NULL)) OR scope = 'public') LIMIT 1",
                (content, sender_id),
            )
        if cur.fetchone():
            return True
        return False

    def save_memory(
        self,
        key: str,
        content: str,
        source: str = "auto",
        chat_id: Optional[str] = None,
        embedding: Optional[list[float]] = None,
        sender_id: str = "",
        sender_name: str = "",
        scope: str = "personal",
    ) -> int:
        """保存一条长期记忆。

        scope: 'personal'（默认，点对点个人记忆）或 'public'（公共记忆）。
        历史数据 scope 为空时按 'personal' 处理，保持原有 1对1 行为不变。
        """
        cur = self.store.conn.cursor()
        emb_str = json.dumps(embedding) if embedding else None
        cur.execute(
            """INSERT INTO memories (key, content, source, chat_id, sender_id, sender_name, embedding, created_at, scope)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, content, source, chat_id, sender_id, sender_name, emb_str, datetime.now().isoformat(), scope),
        )
        self.store.conn.commit()
        # 插入后 lastrowid 必然存在（自增主键），None 实际不可能。
        assert cur.lastrowid is not None
        memory_id = cur.lastrowid

        # 注意：记忆向量【不】写入共享的 faiss 索引（_vector_index）。
        # 该 faiss 索引专供知识库(kb_chunks)使用，其 id 空间是 kb_chunks.id；
        # memories.id 与 kb_chunks.id 均为自增整数，共用会造成 id 空间碰撞
        # （_id_map 相互覆盖 → KB 检索召回记忆向量位、记忆去重误命中 KB 内容）。
        # 记忆检索(recall_memory)走全表扫描 + 内存 cosine + rerank，不依赖 faiss。
        # emb_str 已随行持久化，召回时即时计算相似度。

        # 同主题"最新优先"：把语义相近的旧 active 记忆标为 superseded，
        # 使召回只返回最新结论（解决多次会话对同一事得出不同结论时旧结论残留问题）。
        if embedding:
            self._supersede_similar_memories(
                new_id=memory_id, embedding=embedding, scope=scope, sender_id=sender_id or "",
            )

        logger.info("Saved memory #%d: %s", memory_id, key)
        return memory_id

    def _supersede_similar_memories(self, new_id: int, embedding: list[float],
                                    scope: str, sender_id: str) -> int:
        """将语义相近的旧 active 记忆标为 superseded（指向 new_id）。

        作用域隔离：
        - public 新记忆只作废旧的 public 记忆（全局共享事实的最新版唯一）；
        - personal 新记忆只作废同 sender_id 的旧 personal 记忆（不波及他人）。

        Returns: 被作废的记忆数量
        """
        cur = self.store.conn.cursor()
        if scope == "public":
            cur.execute(
                "SELECT id, embedding FROM memories "
                "WHERE scope = 'public' AND (status = 'active' OR status IS NULL) "
                "AND id != ? AND embedding IS NOT NULL AND embedding != '' "
                "ORDER BY created_at DESC LIMIT ?",
                (new_id, _MEMORY_CANDIDATE_CAP),
            )
        else:
            cur.execute(
                "SELECT id, embedding FROM memories "
                "WHERE sender_id = ? AND (scope = 'personal' OR scope IS NULL) "
                "AND (status = 'active' OR status IS NULL) "
                "AND id != ? AND embedding IS NOT NULL AND embedding != '' "
                "ORDER BY created_at DESC LIMIT ?",
                (sender_id, new_id, _MEMORY_CANDIDATE_CAP),
            )
        superseded: list[int] = []
        for row in cur.fetchall():
            try:
                emb = json.loads(row["embedding"])
            except (ValueError, TypeError):
                continue
            if emb and cosine_similarity(embedding, emb) >= _MEMORY_SUPERSEDE_THRESHOLD:
                superseded.append(row["id"])
        if superseded:
            with self.store._lock:
                cur.executemany(
                    "UPDATE memories SET status = 'superseded', superseded_by = ? WHERE id = ?",
                    [(new_id, mid) for mid in superseded],
                )
                self.store.conn.commit()
            logger.info("记忆 #%d 作废了 %d 条语义相近旧记忆", new_id, len(superseded))
        return len(superseded)

    # ============ 风格 / 人设画像（Feature B） ============

    def cleanup_old_memories(
        self,
        max_age_days: int = 90,
        min_similarity_threshold: float = 0.3,
        gc_superseded: bool = True,
    ) -> int:
        """智能清理过期/已作废记忆。

        策略：
        1. 已作废（status='superseded'，即被更新的结论替代）的记忆直接删除——
           它们已无召回价值，是在写入时由 ``_supersede_similar_memories`` 标记的。
        2. 仍 active 的记忆按年龄清理：created_at 早于 max_age_days 的删除。
           配合"写入即作废旧版"机制，active 集中每条都是某主题的最新结论，
           超龄即视为真正过期，安全删除。

        Args:
            max_age_days: 最大保留天数，超过此时间的 active 记忆会被删除
            min_similarity_threshold: 保留参数（向后兼容，本实现不再使用）
            gc_superseded: 是否回收已作废记忆（默认 True）

        Returns:
            删除的记忆数量
        """
        cutoff_date = (datetime.now().timestamp() - max_age_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff_date).isoformat()

        # P0-1: 使用 store 级 RLock 保证清理操作原子性，避免多 daemon 线程竞态
        with self.store._lock:
            cur = self.store.conn.cursor()
            deleted_count = 0
            if gc_superseded:
                cur.execute("DELETE FROM memories WHERE status = 'superseded'")
                deleted_count += cur.rowcount
            cur.execute(
                "DELETE FROM memories WHERE (status = 'active' OR status IS NULL) AND created_at < ?",
                (cutoff_iso,),
            )
            deleted_count += cur.rowcount
            self.store.conn.commit()

        if deleted_count > 0:
            logger.info("清理了 %d 个过期/已作废记忆（保留 %d 天）", deleted_count, max_age_days)

        return deleted_count

