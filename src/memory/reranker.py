from __future__ import annotations

import logging
import re
from collections import Counter

import numpy as np

logger = logging.getLogger(__name__)


class SimpleReranker:
    """简单的重排序器：结合向量相似度和关键词匹配度。"""

    def __init__(self, vector_weight: float = 0.6, keyword_weight: float = 0.4):
        self.vector_weight = vector_weight
        self.keyword_weight = keyword_weight

    def _tokenize(self, text: str) -> list[str]:
        """简单的中文分词：按字符和英文单词拆分。"""
        # 提取中文字符和英文单词
        tokens = []
        for token in re.findall(r'[\u4e00-\u9fff]|[a-zA-Z]+|\d+', text.lower()):
            if len(token) > 1 or '\u4e00' <= token <= '\u9fff':
                tokens.append(token)
        return tokens

    def _keyword_score(self, query: str, document: str) -> float:
        """计算查询和文档的关键词重叠度（BM25 简化版）。"""
        q_tokens = self._tokenize(query)
        d_tokens = self._tokenize(document)

        if not q_tokens or not d_tokens:
            return 0.0

        q_counter = Counter(q_tokens)
        d_counter = Counter(d_tokens)

        # 计算重叠
        overlap = 0
        for token, q_count in q_counter.items():
            d_count = d_counter.get(token, 0)
            if d_count > 0:
                # IDF 简化： rare terms get higher weight
                idf = 1.0 + np.log(1 + len(d_tokens) / (d_count + 1))
                overlap += min(q_count, d_count) * idf

        # 归一化
        query_len = len(q_tokens)

        # 结合查询覆盖率和文档覆盖率
        score = overlap / (query_len + 0.5)  # 轻微惩罚长文档
        return min(score, 1.0)

    def rerank(self, query: str, results: list[dict],
               top_k: int = 5) -> list[dict]:
        """
        对检索结果进行重排序。

        Args:
            query: 查询文本
            results: 原始检索结果，每项应包含 similarity 和 content
            top_k: 返回前 k 个

        Returns:
            重排序后的结果
        """
        if not results:
            return results

        scored = []
        for r in results:
            vector_sim = r.get("similarity", 0)
            content = r.get("content", "")
            keyword_sim = self._keyword_score(query, content)

            # 综合得分
            final_score = (self.vector_weight * vector_sim +
                           self.keyword_weight * keyword_sim)

            scored.append({**r, "final_score": final_score,
                           "keyword_score": keyword_sim})

        # 按综合得分排序
        scored.sort(key=lambda x: x["final_score"], reverse=True)
        return scored[:top_k]
