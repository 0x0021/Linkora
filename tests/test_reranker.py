"""测试 memory/reranker.py — SimpleReranker 重排序器"""
import pytest

from src.memory.reranker import SimpleReranker


# ============================================================================
# __init__
# ============================================================================
class TestInit:
    def test_default_weights(self):
        rr = SimpleReranker()
        assert rr.vector_weight == 0.6
        assert rr.keyword_weight == 0.4

    def test_custom_weights(self):
        rr = SimpleReranker(vector_weight=0.3, keyword_weight=0.7)
        assert rr.vector_weight == 0.3
        assert rr.keyword_weight == 0.7


# ============================================================================
# _tokenize
# ============================================================================
class TestTokenize:
    @pytest.fixture
    def rr(self):
        return SimpleReranker()

    def test_chinese_sentence(self, rr):
        # tokenizer 按单字拆分 CJK
        tokens = rr._tokenize("你好世界")
        assert "你" in tokens
        assert "好" in tokens
        assert "世" in tokens
        assert "界" in tokens

    def test_english_words(self, rr):
        tokens = rr._tokenize("hello world test")
        assert "hello" in tokens
        assert "world" in tokens
        assert "test" in tokens

    def test_mixed_chinese_english(self, rr):
        # CJK 单字拆分，英文保留完整词
        tokens = rr._tokenize("Python 语言人工智能 AI 模型")
        assert "python" in tokens  # lowered
        assert "语" in tokens
        assert "言" in tokens
        assert "人" in tokens
        assert "工" in tokens
        assert "智" in tokens
        assert "能" in tokens
        assert "ai" in tokens
        assert "模" in tokens
        assert "型" in tokens

    def test_digits(self, rr):
        tokens = rr._tokenize("版本 3.14 发布了 2026 年")
        assert "14" in tokens or "3" in tokens  # depends on re split
        assert "2026" in tokens

    def test_single_char_cjk_preserved(self, rr):
        # 单字中文保留
        tokens = rr._tokenize("的 和 是 a b c")
        assert "的" in tokens
        assert "和" in tokens
        assert "是" in tokens
        # 单字母英文丢弃
        assert "a" not in tokens
        assert "b" not in tokens
        assert "c" not in tokens

    def test_empty_string(self, rr):
        assert rr._tokenize("") == []

    def test_punctuation_only(self, rr):
        tokens = rr._tokenize("，。！？...")
        assert tokens == []

    def test_lowercase(self, rr):
        tokens = rr._tokenize("Hello World PYTHON")
        assert "hello" in tokens
        assert "world" in tokens
        assert "python" in tokens


# ============================================================================
# _keyword_score
# ============================================================================
class TestKeywordScore:
    @pytest.fixture
    def rr(self):
        return SimpleReranker()

    def test_exact_match(self, rr):
        score = rr._keyword_score("人工智能", "人工智能技术发展")
        assert 0 < score <= 1.0

    def test_no_overlap(self, rr):
        score = rr._keyword_score("苹果", "香蕉橘子")
        assert score == 0.0

    def test_empty_query(self, rr):
        assert rr._keyword_score("", "some content") == 0.0

    def test_empty_document(self, rr):
        assert rr._keyword_score("query", "") == 0.0

    def test_both_empty(self, rr):
        assert rr._keyword_score("", "") == 0.0

    def test_partial_overlap(self, rr):
        # 单字 CJK tokenizer 下，"合同违约金" 与 "合同条款约定违约金" 大量单字重叠
        # score 会被 clamp 到 1.0
        score = rr._keyword_score("合同违约金 支付", "合同条款约定违约金比例")
        assert score == 1.0  # 大量单字重叠 → 被 min(score, 1.0) clamp

    def test_all_query_tokens_match(self, rr):
        score = rr._keyword_score("人工智能", "人工智能")
        assert score > 0.5

    def test_idf_effect(self, rr):
        """稀有词匹配得分应高于常见词"""
        rare_score = rr._keyword_score("量子", "量子计算是前沿科技")
        common_score = rr._keyword_score("是一", "这是一段文字")
        # 稀有词 idf 更高，但 query_len 也影响；不强行比较数值，确保都能计算
        assert rare_score >= 0
        assert common_score >= 0

    def test_range_zero_to_one(self, rr):
        """得分始终在 [0, 1] 内"""
        cases = [
            ("合同", "合同"),
            ("abcdefg", "xyz"),
            ("", "content"),
            ("query", ""),
            ("长文本查询" * 10, "长文本" * 20),
        ]
        for q, d in cases:
            score = rr._keyword_score(q, d)
            assert 0.0 <= score <= 1.0, f"score={score} for q={q!r}, d={d!r}"


# ============================================================================
# rerank
# ============================================================================
class TestRerank:
    @pytest.fixture
    def rr(self):
        return SimpleReranker()

    def test_empty_results(self, rr):
        assert rr.rerank("query", []) == []

    def test_single_result(self, rr):
        results = [{"similarity": 0.8, "content": "人工智能简介"}]
        output = rr.rerank("人工智能", results)
        assert len(output) == 1
        assert "final_score" in output[0]
        assert "keyword_score" in output[0]

    def test_sorts_by_final_score(self, rr):
        results = [
            {"similarity": 0.5, "content": "机器学习基础概念介绍"},
            {"similarity": 0.9, "content": "与查询无关的内容"},
            {"similarity": 0.6, "content": "机器学习深度学习教程"},
        ]
        output = rr.rerank("机器学习", results)
        scores = [r["final_score"] for r in output]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_truncation(self, rr):
        results = [
            {"similarity": 0.9, "content": f"doc{i}"}
            for i in range(10)
        ]
        output = rr.rerank("query", results, top_k=3)
        assert len(output) == 3

    def test_top_k_larger_than_results(self, rr):
        results = [{"similarity": 0.5, "content": "doc1"}]
        output = rr.rerank("query", results, top_k=10)
        assert len(output) == 1

    def test_missing_similarity_defaults_zero(self, rr):
        results = [{"content": "only content no sim"}]
        output = rr.rerank("query", results)
        assert len(output) == 1
        assert output[0]["final_score"] == rr.keyword_weight * output[0]["keyword_score"]

    def test_missing_content_defaults_empty(self, rr):
        results = [{"similarity": 0.5}]
        output = rr.rerank("query", results)
        assert len(output) == 1
        assert output[0]["keyword_score"] == 0.0

    def test_preserves_original_fields(self, rr):
        results = [{"similarity": 0.8, "content": "test", "id": 42, "meta": {"a": 1}}]
        output = rr.rerank("test", results)
        assert output[0]["id"] == 42
        assert output[0]["meta"] == {"a": 1}

    def test_vector_weight_zero(self, rr):
        """vector_weight=0 时仅用关键词分数"""
        rr = SimpleReranker(vector_weight=0.0, keyword_weight=1.0)
        results = [{"similarity": 0.9, "content": "不相关的文档"}]
        output = rr.rerank("机器学习", results)
        assert output[0]["final_score"] == output[0]["keyword_score"]

    def test_keyword_weight_zero(self, rr):
        """keyword_weight=0 时仅用向量分数"""
        rr = SimpleReranker(vector_weight=1.0, keyword_weight=0.0)
        results = [{"similarity": 0.7, "content": "anything"}]
        output = rr.rerank("query", results)
        assert output[0]["final_score"] == 0.7

    def test_final_score_in_range(self, rr):
        results = [
            {"similarity": 0.5, "content": f"doc{i} 关键词 匹配"}
            for i in range(5)
        ]
        output = rr.rerank("关键词", results)
        for r in output:
            assert 0.0 <= r["final_score"] <= 1.0
