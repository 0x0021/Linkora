"""
RAG 知识库搜索工具。

在回复前先从知识库检索相关资料，确保回答准确、有依据。
意图关键词（intent_keywords）在初始化时从知识库内容动态提取，无需手动维护。
"""
from __future__ import annotations

import logging
from typing import Optional

from src.memory.sqlite_store import SQLiteStore
from src.tools.base import BaseTool
from src.tools.utils import safe_float, safe_int

logger = logging.getLogger(__name__)

# 停用词：中文常用虚词、标点，提取关键词时过滤掉
_STOP_WORDS: set[str] = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一", "一个",
    "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好",
    "自己", "这", "他", "她", "它", "那", "吧", "吗", "呢", "啊", "嗯", "哦",
    "可以", "需要", "如何", "怎么", "什么", "哪里", "为什么", "多少", "哪个",
    "请", "谢谢", "感谢", "您好", "你好", "帮忙", "帮助",
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "is", "are", "was", "be", "been", "has", "have", "had", "do", "does",
}

# PDF 二进制文本中的垃圾语法标记，必须过滤掉
_PDF_GARBAGE: set[str] = {
    "obj", "endobj", "stream", "endstream", "xref", "trailer", "startxref",
    "true", "false", "null", "flatedecode", "length", "width", "height",
    "type", "subtype", "filter", "decode", "predictor", "colors", "bitspercomponent",
    "n", "f", "r", "g", "rg", "k", "cmyk", "bt", "et", "tj", "tj", "td", "tf",
    "im", "imag", "png", "jpg", "jpeg", "gif", "bmp", "ico", "emf", "wmf",
    "http", "https", "www", "com", "cn", "org", "net", "pdf", "doc", "docx",
    "abcdef", " ABC", "def", "ghi", "jkl", "mno", "pqr", "stu", "vwx", "yz",
    "font", "fontname", "basefont", "encoding", "cid", "cmap", "tounicode",
}

# 常用汉字白名单（3500 常用汉字的核心子集，覆盖 95% 日常用词）
# 如果一个「词」里没有任何一个字符在这个集合里，大概率是垃圾
_COMMON_CHARS: set[str] = set(
    "的一是不了在人有我他这个中大来上国个到说们为子和你地出道也时年得就那"
    "要下以生会自着去之过家学对可她里后小机心用多么去好她将还如进多"
    "全公如工第一了最原没已路但变图情那"
    # 补充覆盖 IT/办公 常用字
    "软件硬件网络打印机VPN电脑设置安装配置使用教程指南手册规范流程审批申请"
    "公司部门行政人事财务销售市场技术研发产品运营项目管理"
    "文档表格幻灯片邮件日历会议待办通知公告"
)


class KBSearchTool(BaseTool):
    """
    搜索公司知识库（用于查询公司规范、技术文档、会议纪要等）。

    当用户询问公司流程、技术规范、历史决策时，先搜索知识库，
    基于真实文档回答，而不是让 LLM 自己编答案。
    """
    name = "kb_search"
    display_name = "知识库检索"
    short_description = "在内部 RAG 知识库中检索结构化知识，返回规范、流程、纪要等准确答案"
    description = (
        "搜索公司内部知识库，查询公司规范、技术文档、会议纪要、"
        "审批流程等结构化知识。适用于需要准确答案的场景，"
        "避免基于过时或不准确的 LLM 训练数据回答。"
    )
    # OpenAI function calling 参数定义
    parameters: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索查询（用户问题或关键词）",
            },
            "top_k": {
                "type": "integer",
                "description": "返回最多 top_k 个结果（默认 3）",
                "default": 3,
            },
            "min_similarity": {
                "type": "number",
                "description": "最小相似度阈值（0-1，默认 0.3，较低以返回更多结果）",
                "default": 0.3,
            },
        },
        "required": ["query"],
    }
    # 意图关键词：仅作为「显式要求搜索知识库」时的触发短语（RAG 背景检索已由系统提示词自动注入）
    intent_keywords: list[str] = []

    def __init__(self, store: SQLiteStore, embedding_config: Optional[dict] = None):
        """
        初始化 RAG 搜索工具。

        Args:
            store: SQLite 存储器（包含知识库表）
            embedding_config: Embedding 配置（字典或 EmbeddingConfig 对象）
        """
        self.store = store
        self.embedding_config = embedding_config or {}
        self.embedding_client = None

        # 从知识库动态提取意图关键词（必须在 embedding 加载前完成，不依赖模型）
        self.intent_keywords = self._build_intent_keywords()
        logger.info("[RAG] 动态意图关键词已提取: %s", list(self.intent_keywords)[:10])

        # 延迟加载 EmbeddingClient（避免启动时依赖问题）
        enabled = False
        if isinstance(self.embedding_config, dict):
            enabled = self.embedding_config.get("enabled", False)
        else:
            enabled = getattr(self.embedding_config, "enabled", False)

        if enabled:
            try:
                from src.memory.embedding import EmbeddingClient
                from src.config import EmbeddingConfig

                if isinstance(self.embedding_config, dict):
                    config_obj = EmbeddingConfig(**self.embedding_config)
                else:
                    config_obj = self.embedding_config

                self.embedding_client = EmbeddingClient(config_obj)
                logger.info("[RAG] Embedding 客户端已加载")
            except Exception as e:
                logger.warning("[RAG] Embedding 客户端加载失败: %s", e)
                self.embedding_client = None
        else:
            logger.info("[RAG] Embedding 未启用，将使用全文检索（兜底方案）")

    def _build_intent_keywords(self) -> list[str]:
        """
        返回 kb_search 的意图触发短语。

        设计说明：RAG 背景检索已由 `LLMAgent._retrieve_relevant_knowledge` 自动注入，
        kb_search 仅作为「用户显式要求搜索知识库」时的可选工具。因此这里只返回明确的
        触发短语，避免对每条知识库话题消息都重复暴露该工具造成双重检索。

        注：历史版本曾从 kb_documents/kb_chunks 动态分词提取高频词，但结果最终被
        显式触发词覆盖（分词结果从未被使用），属死代码 + 无谓启动开销，已移除。
        _STOP_WORDS/_PDF_GARBAGE/_COMMON_CHARS 仍保留供未来可能的动态提取复用。

        Returns:
            意图触发短语列表
        """
        return [
            "知识库", "查文档", "查资料", "查一下文档", "搜一下知识库",
            "内部文档", "知识库里", "公司文档", "内部资料", "翻一下文档",
            "知识库里怎么写", "有没有相关文档", "文档里怎么写", "搜索知识库",
        ]

    def _fallback_keywords(self) -> list[str]:
        """数据库不可用时，返回通用触发词。"""
        return [
            "知识库", "文档", "规范", "流程", "审批", "申请",
            "教程", "指南", "手册", "怎么用", "如何使用",
            "安装", "配置", "设置", "VPN", "打印机", "软件",
        ]

    def execute(self, args: dict) -> dict:
        """搜索知识库并返回相关文档片段（BaseTool 基类契约：仅接收 args dict）。

        Args:
            args: LLM function call 参数（dict，包含 query/top_k/min_similarity）

        Returns:
            包含搜索结果的字典
        """
        query = args.get("query", "")
        top_k = args.get("top_k", 3)
        min_similarity = args.get("min_similarity", 0.3)
        return self.search(query, top_k, min_similarity)

    def search(self, query: str, top_k=3, min_similarity=0.3,
               query_embedding: list[float] | None = None) -> dict:
        """程序化检索入口（供 RAG 注入等内部复用）。

        Args:
            query: 搜索查询文本。
            top_k: 返回最多 top_k 个结果。
            min_similarity: 最小相似度阈值（0-1）。
            query_embedding: 调用方已算好的 query 向量（可选）。若提供则跳过内部
                  向量化，复用该向量做检索——避免与 Agent 的语义意图分类重复 embed。

        Returns:
            包含搜索结果的字典（search_method: embedding / fulltext / none）
        """
        if not query or not str(query).strip():
            return {"error": "搜索查询不能为空"}

        query = str(query).strip()
        # LLM 可能传字符串型/中文数字/带单位的值，top_k 参与切片、min_similarity 参与
        # 数值比较，非数字会抛 TypeError 使工具崩溃。统一安全解析并限制上下限。
        top_k = max(1, min(safe_int(top_k, 3), 20))
        min_similarity = min(max(safe_float(min_similarity, 0.3), 0.0), 1.0)
        logger.info("[RAG] 正在搜索知识库: %s", query[:50])

        try:
            # 方法 1：向量检索（如果 embedding 可用）
            if self.embedding_client:
                if query_embedding is None:
                    query_embedding = self.embedding_client.embed(query)
                    if not query_embedding:
                        logger.warning("[RAG] 查询向量化失败")
                        query_embedding = None
                if query_embedding is not None:
                    results = self._search_kb_embedding(
                        query_embedding, top_k, min_similarity, query)
                    if results:
                        return {
                            "success": True,
                            "query": query,
                            "search_method": "embedding",
                            "results": results,
                            "message": f"找到 {len(results)} 条相关结果（向量检索，相似度≥{min_similarity}）",
                        }
                    else:
                        logger.info(
                            "[RAG] 向量检索无结果（min_similarity=%s），降级到全文检索",
                            min_similarity,
                        )

            # 方法 2：全文检索（兜底）
            results = self._search_by_fulltext(query, top_k)
            if results:
                return {
                    "success": True,
                    "query": query,
                    "search_method": "fulltext",
                    "results": results,
                    "message": f"找到 {len(results)} 条相关结果（全文检索）",
                }

            # 都没找到
            return {
                "success": True,
                "query": query,
                "search_method": "none",
                "results": [],
                "message": "知识库中未找到相关内容",
            }

        except Exception as e:
            logger.error("[RAG] 搜索失败: %s", e, exc_info=True)
            return {"error": f"知识库搜索失败: {e}"}

    @staticmethod
    def _format_hit(r: dict, score: float) -> dict:
        """把 repo 层返回的检索结果统一格式化为标准 hit 结构。"""
        return {
            "content": r.get("content", ""),
            "source": r.get("title", "未知文档"),
            "doc_type": r.get("doc_type", ""),
            "score": score,
            "chunk_id": r.get("chunk_id", ""),
        }

    def _search_kb_embedding(self, query_embedding: list[float], top_k: int,
                             min_similarity: float, query_text: str = "") -> list[dict]:
        """向量检索知识库（统一入口：调用方负责准备好 query 向量）。"""
        try:
            results = self.store._kb_repo.search_kb(
                query_embedding=query_embedding,
                top_k=top_k,
                query_text=query_text,
            )

            filtered = [
                self._format_hit(r, r.get("similarity", 0))
                for r in results
                if r.get("similarity", 0) >= min_similarity
            ]

            logger.info("[RAG] 向量检索返回 %d 条结果（过滤后）", len(filtered))
            return filtered

        except Exception as e:
            logger.error("[RAG] 向量检索失败: %s", e)
            return []

    def _search_by_fulltext(self, query: str, top_k: int) -> list[dict]:
        """使用全文检索知识库（兜底方案）。"""
        try:
            results = self.store._kb_repo.search_kb_by_keyword(
                query=query,
                top_k=top_k,
            )

            formatted = []
            for r in results:
                # 纵深防御：score 钳位到 [0,1]。repo 层已按满分归一化，
                # 此处兜底防止任何未归一化的原始计数（如旧数据/新增路径）
                # 泄漏为下游「相关度>100%」异常展示。
                raw_score = float(r.get("score", 0) or 0)
                formatted.append(self._format_hit(
                    r, min(max(raw_score, 0.0), 1.0)))

            logger.info("[RAG] 全文检索返回 %d 条结果", len(formatted))
            return formatted

        except Exception as e:
            logger.error("[RAG] 全文检索失败: %s", e)
            return []

    def check_health(self) -> dict:
        """检查工具健康状态（用于系统状态工具）。"""
        try:
            stats = self.store._kb_repo.kb_stats()
            embedding_enabled = False
            if isinstance(self.embedding_config, dict):
                embedding_enabled = self.embedding_config.get("enabled", False)
            else:
                embedding_enabled = getattr(self.embedding_config, "enabled", False)
            return {
                "status": "healthy",
                "kb_documents": stats.get("total_documents", 0),
                "kb_chunks": stats.get("total_chunks", 0),
                "embedding_enabled": embedding_enabled,
                "embedding_available": self.embedding_client is not None,
                "intent_keywords_count": len(self.intent_keywords),
            }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}
