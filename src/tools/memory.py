from __future__ import annotations

import hashlib
import logging

from src.memory.classifier import classify_memory_scope
from src.memory.embedding import EmbeddingClient
from src.memory.sqlite_store import SQLiteStore
from src.tools.base import BaseTool
from src.tools.utils import safe_int

logger = logging.getLogger(__name__)


class RecallMemoryTool(BaseTool):
    name = "recall_memory"
    display_name = "召回长期记忆"
    short_description = "从长期记忆库中按主题检索相关历史信息，用于补全当前对话上下文"
    description = "从长期记忆中召回与当前话题相关的信息（基于向量相似度，自动按当前对话人过滤）"
    intent_keywords: list[str] = []  # 基础工具，始终包含（帮助 LLM 获取上下文）
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询文本（用于计算相似度）"
            },
            "top_k": {
                "type": "integer",
                "description": "返回数量，默认 5",
                "default": 5
            },
        },
        "required": ["query"],
    }

    def __init__(self, store: SQLiteStore, embedding_client: EmbeddingClient, min_similarity: float = 0.0):
        self.store = store
        self.embedding_client = embedding_client
        self.min_similarity = min_similarity

    def execute(self, args: dict) -> str | dict:
        query = str(args.get("query", "")).strip()
        chat_id = args.get("chat_id", "")
        sender_id = args.get("sender_id", "")
        # LLM 可能传入字符串型/中文数字，recall_memory 内部 [:top_k*2] 切片
        # 遇非 int 会抛 TypeError 使工具崩溃，这里统一安全解析并限制上下限。
        top_k = max(1, min(safe_int(args.get("top_k", 5), 5), 50))

        if not query:
            return {"error": "query is required"}

        if not self.embedding_client.enabled:
            return {"error": "embedding is not enabled"}

        try:
            query_embedding = self.embedding_client.embed(query)
            if not query_embedding:
                return {"error": "failed to create query embedding"}

            memories = self.store._memory_repo.recall_memory(
                query_embedding, top_k, chat_id, query_text=query,
                sender_id=sender_id, min_similarity=self.min_similarity)
            results = []
            for m in memories:
                results.append({
                    "content": m.get("content", ""),
                    "source": m.get("source", ""),
                    "similarity": round(float(m.get("similarity", 0.0)), 4),
                    "created_at": m.get("created_at", ""),
                })
            return {"count": len(results), "memories": results}
        except Exception as e:
            logger.exception("召回长期记忆失败: %s", e)
            return {"error": f"召回长期记忆失败: {e}"}


class SaveMemoryTool(BaseTool):
    name = "save_memory"
    display_name = "写入长期记忆"
    short_description = "将重要信息写入长期记忆（按主题向量化），便于后续对话中按主题自动召回"
    description = "保存信息到长期记忆中（自动绑定当前对话人，后续召回时按人过滤）"
    intent_keywords: list[str] = []  # 基础工具，始终包含
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "要保存的记忆内容（应该是精炼后的关键信息）"
            },
            "source": {
                "type": "string",
                "description": "来源（如：对话记录、文档内容等，可选）"
            },
            "scope": {
                "type": "string",
                "description": "记忆范围：'personal'（默认，点对点个人记忆）或 'public'（公共记忆）。不传则由算法自动判断。",
                "enum": ["personal", "public"],
            },
        },
        "required": ["content"],
    }

    def __init__(self, store: SQLiteStore, embedding_client: EmbeddingClient):
        self.store = store
        self.embedding_client = embedding_client

    def execute(self, args: dict) -> str | dict:
        try:
            content = args.get("content", "")
            source = args.get("source", "")
            chat_id = args.get("chat_id", "")
            sender_id = args.get("sender_id", "")
            sender_name = args.get("sender_name", "")
            explicit_scope = args.get("scope")

            content = str(content).strip()
            if not content:
                return {"error": "content is required"}

            # 自动判定范围（个人 / 公共）：显式传入 scope 时优先采用，否则按信号词算法判断。
            chat_type = self.store._conversation_repo.get_chat_type(chat_id)
            scope, reason, _ = classify_memory_scope(
                content, sender_id=sender_id, sender_name=sender_name,
                chat_type=chat_type, source=source or "manual", explicit_scope=explicit_scope,
            )

            # 去重：内容完全相同或语义高度相似(按 scope 范围)则跳过，
            # 避免 LLM 反复保存同一事实灌满记忆表、召回时返回大量近重复项。
            if self.store._memory_repo.check_memory_duplicate(
                content, embedding_client=self.embedding_client,
                sender_id=sender_id, scope=scope,
            ):
                logger.info("记忆去重命中，跳过保存: %s...", content[:30])
                return {"success": True, "skipped": True, "reason": "duplicate",
                        "content_length": len(content), "scope": scope}

            embedding = None
            if self.embedding_client.enabled:
                embedding = self.embedding_client.embed(content)

            key = "mem_" + hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
            memory_id = self.store._memory_repo.save_memory(
                key=key, content=content, source=source or "manual",
                chat_id=chat_id, embedding=embedding,
                sender_id=sender_id, sender_name=sender_name, scope=scope,
            )
            return {"success": True, "memory_id": memory_id, "content_length": len(content),
                    "has_embedding": embedding is not None, "scope": scope, "scope_reason": reason}
        except Exception as e:
            logger.exception("写入长期记忆失败: %s", e)
            return {"error": f"写入长期记忆失败: {e}"}
