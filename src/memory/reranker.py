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

    def _tokenize_score(self, text: str) -> list[str]:
        """用于关键词打分的中文切词：英文/数字保留整词，中文按 2-gram 拆分。

        【2026-08-15 检索相关性修复】原 ``_keyword_score`` 直接复用 ``_tokenize``
        的单字切词，导致「打印机」被拆成 打/印/机 三个单字；而「打」「机」这类
        字在几乎所有中文文档中都出现，命中后被 ``min(score,1.0)`` 饱和到 1.0，
        使关键词分对中文失去区分度、退化为纯向量相似度（弱向量下「打印机」反而
        离 Adobe 文档更近，搜打印机出 Adobe）。改用 2-gram 后，「打印机」只匹配
        真正含「打印/印机」的文档，关键词分恢复区分度。
        """
        if not text:
            return []
        text = text.lower()
        tokens: list[str] = []
        for seg in re.findall(r'[\u4e00-\u9fff]+|[a-zA-Z]+|\d+', text):
            if re.match(r'[a-zA-Z]+$', seg) or re.match(r'\d+$', seg):
                tokens.append(seg)
            elif len(seg) == 1:
                tokens.append(seg)
            else:
                # 中文按相邻 2-gram 拆分（覆盖最常见中文词长）
                for i in range(len(seg) - 1):
                    tokens.append(seg[i:i + 2])
        return tokens

    def _keyword_score(self, query: str, document: str) -> float:
        """计算查询和文档的关键词重叠度（BM25 简化版）。"""
        q_tokens = self._tokenize_score(query)
        d_tokens = self._tokenize_score(document)

        if not q_tokens or not d_tokens:
            return 0.0

        q_counter = Counter(q_tokens)
        d_counter = Counter(d_tokens)

        # 计算重叠
        overlap = 0
        for token, q_count in q_counter.items():
            d_count = d_counter.get(token, 0)
            if d_count > 0:
                # IDF 简化：词在当前文档中出现越少，权重越高。
                # 注：原公式误用文档长度 len(d_tokens) 作分子，导致长文档被系统性高估
                # （同一词在长短文档里 d_count 相同，但长文档 len 更大 → idf 虚高，
                # 出现「越长越相关」的错误倾向）。这里改为只依赖词频，与文档长度解耦。
                idf = 1.0 + np.log(1 + 1.0 / (d_count + 1))
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
        if not query:
            # query 为空（防御性）：无法做关键词重排，原样返回避免 _keyword_score(None) 报错
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
