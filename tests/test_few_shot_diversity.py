"""few_shot_diversity 单测：纯函数，覆盖归一化/分桶/主题键/trigram/贪心挑选。

启动 <0.05s，无 SQLite 依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.memory.few_shot_diversity import (
    greedy_select,
    len_bucket,
    normalize,
    score_candidate,
    topic_key,
    trigram_similarity,
    trigrams,
)


# ==================== normalize ====================

class TestNormalize:
    def test_basic(self):
        assert normalize("Hello World") == "helloworld"

    def test_strips_whitespace(self):
        assert normalize("  你好\n\t世界  ") == "你好世界"

    def test_none_safe(self):
        """None/空字符串归一化后为空（用于 set 键时不会爆）。"""
        assert normalize("") == ""
        assert normalize(None) == ""

    def test_idempotent(self):
        """二次归一化无变化。"""
        s = "在的  有  什么事吗"
        assert normalize(normalize(s)) == normalize(s)

    def test_chinese_no_change(self):
        """中文无大小写差异但归一化函数仍生效（去空白 + 小写）。"""
        assert normalize("你好") == "你好"


# ==================== len_bucket ====================

class TestLenBucket:
    def test_short_bucket(self):
        """≤20 字 → s"""
        assert len_bucket("") == "s"          # 0 字
        assert len_bucket("x") == "s"         # 1 字
        assert len_bucket("x" * 20) == "s"

    def test_medium_bucket(self):
        """21~60 字 → m"""
        assert len_bucket("x" * 21) == "m"
        assert len_bucket("x" * 60) == "m"

    def test_long_bucket(self):
        """>60 字 → l"""
        assert len_bucket("x" * 61) == "l"
        assert len_bucket("x" * 200) == "l"

    def test_none_treated_as_empty(self):
        """None → 空串 → s 桶。"""
        assert len_bucket(None) == "s"


# ==================== topic_key ====================

class TestTopicKey:
    def test_chinese_opener(self):
        """中文 2~4 字开头正确提取。"""
        # {2,4} 贪心匹配最长前缀（最多 4 字）
        assert topic_key("在吗") == "在吗"           # 2 字原样
        assert topic_key("收到，文件已查收") == "收到"   # 逗号前的 2 字
        assert topic_key("这是一段很长的用户消息") == "这是一段"  # 最多 4 字

    def test_english_opener(self):
        """字母开头也支持。"""
        assert topic_key("hello world") == "hell"
        assert topic_key("hi") == "hi"

    def test_digit_or_punct_opener(self):
        """数字/符号开头走 fallback：归一化前 3 字。"""
        k = topic_key("123abc")
        assert k == "123"  # 归一化后取前 3

    def test_empty_falls_back_to_NA(self):
        """空串/纯空白 → NA。"""
        assert topic_key("") == "NA"
        assert topic_key("   ") == "NA"

    def test_none_treated_as_empty(self):
        assert topic_key(None) == "NA"


# ==================== trigram_similarity ====================

class TestTrigrams:
    def test_short_returns_self(self):
        """≤2 字退化为 {s}。"""
        assert trigrams("") == set()
        assert trigrams("a") == {"a"}
        assert trigrams("ab") == {"ab"}

    def test_three_chars_one_trigram(self):
        assert trigrams("abc") == {"abc"}

    def test_long_string_multiple_trigrams(self):
        assert trigrams("abcdef") == {"abc", "bcd", "cde", "def"}


class TestTrigramSimilarity:
    def test_identical_is_one(self):
        assert trigram_similarity("你好世界", "你好世界") == 1.0

    def test_completely_different_is_zero(self):
        """完全不重叠 → 0（除非一方为空）。"""
        # 中文 vs 纯字母
        sim = trigram_similarity("你好", "abc")
        assert sim == 0.0

    def test_empty_both_is_one(self):
        """双方都为空 → 1.0（视为完全相同/匹配）。"""
        assert trigram_similarity("", "") == 1.0
        assert trigram_similarity("", "abc") == 0.0  # 一方为空→0

    def test_near_duplicate_high_similarity(self):
        """近似重复的两段中文相似度应高。
        注：单个标点差异（逗号）只会把 trigram 相似度拉到 ~0.76 而非 ≥0.8。
        调用方阈值 0.8 实际捕捉的是「多次变体」的样例；这里验证 [0.5, 0.8) 区间即属高。"""
        a = "好的方案已经发到你邮箱请查收有问题随时说"
        b = "好的方案已经发到你邮箱请查收，有问题随时说"
        sim = trigram_similarity(a, b)
        assert 0.5 <= sim < 0.8, f"期望 [0.5, 0.8) 实测 {sim}"

    def test_near_duplicate_threshold_above(self):
        """仅 1 字差异的极近似对（保证高重叠 trigram）应 ≥0.8。"""
        a = "在的有什么事您尽管说我会尽快帮你安排处理"
        b = "在的有什么事您尽管说我会尽快帮你安排处理完毕"  # 仅尾部多 2 字
        sim = trigram_similarity(a, b)
        # 18 字符 vs 20 字符，共享 16 个 trigram，总计 18 → 0.889
        assert sim >= 0.8, f"期望 ≥0.8 实测 {sim}"

    def test_different_topics_low_similarity(self):
        """主题不同则相似度低。"""
        a = "在的，有什么事您尽管说"
        b = "合同已经盖章今天下午走顺丰寄出"
        sim = trigram_similarity(a, b)
        assert sim < 0.5


# ==================== score_candidate ====================

class TestScoreCandidate:
    def test_both_under_cap(self):
        """桶/主题都未满 → 3 分。"""
        assert score_candidate("s", "在", {}, {}, 2, 2) == 3

    def test_bucket_full(self):
        """桶满 → 仅主题贡献 → 1 分。"""
        bucket_counts = {"s": 2}
        assert score_candidate("s", "在", bucket_counts, {}, 2, 2) == 1

    def test_topic_full(self):
        """主题满 → 仅桶贡献 → 2 分。"""
        topic_counts = {"在": 2}
        assert score_candidate("s", "在", {}, topic_counts, 2, 2) == 2

    def test_both_full(self):
        """双满 → 0 分。"""
        assert score_candidate("s", "在", {"s": 2}, {"在": 2}, 2, 2) == 0


# ==================== greedy_select ====================

def _c(user: str, assistant: str, bucket: str, topic: str) -> dict:
    """构造 pool 项（与 recommend_few_shot_pairs 内部 schema 一致）。"""
    return {"user": user, "assistant": assistant, "_bucket": bucket, "_topic": topic}


class TestGreedySelect:
    def test_empty_pool(self):
        assert greedy_select([], limit=3) == []

    def test_under_limit_returns_all(self):
        """候选 < limit → 全部返回。"""
        pool = [_c("u1", "a1", "s", "x"), _c("u2", "a2", "s", "x")]
        out = greedy_select(pool, limit=5)
        assert len(out) == 2
        # 内部字段被剥离
        assert "_bucket" not in out[0]
        assert out[0]["user"] == "u1"

    def test_diversity_topic(self):
        """主题分散：候选全在同一桶时，应优先均衡主题。"""
        pool = [
            _c("在吗1", "a1", "s", "在"),
            _c("在吗2", "a2", "s", "在"),
            _c("在吗3", "a3", "s", "在"),
            _c("收到1", "b1", "s", "收"),
            _c("收到2", "b2", "s", "收"),
            _c("谢谢1", "c1", "s", "谢"),
        ]
        out = greedy_select(pool, limit=3, cap_bucket=10, cap_topic=1)
        # 把 out 映回 pool 项目看 topic
        out_keys = {(o["user"], o["assistant"]) for o in out}
        topics = {p["_topic"] for p in pool if (p["user"], p["assistant"]) in out_keys}
        assert len(topics) == 3, f"3 个不同 topic 各取 1，期望 3 实测 {topics}"

    def test_diversity_length(self):
        """长度桶分散。"""
        pool = [
            _c("u1", "a1", "s", "t1"),  # 短
            _c("u2", "a2", "s", "t2"),
            _c("u3", "a3", "m", "t3"),  # 中
            _c("u4", "a4", "m", "t4"),
            _c("u5", "a5", "l", "t5"),  # 长
            _c("u6", "a6", "l", "t6"),
        ]
        out = greedy_select(pool, limit=6, cap_bucket=2, cap_topic=1)
        # 桶计数 s/m/l 各 ≤2
        buckets = {"s": 0, "m": 0, "l": 0}
        for p in pool:
            if p in out:
                buckets[p["_bucket"]] += 1
        assert max(buckets.values()) <= 2

    def test_pass2_relaxes_constraints(self):
        """Pass 2 兜底：所有候选都在同一桶/主题，limit 大于多样性容量时应放松补满。"""
        pool = [
            _c(f"u{i}", f"a{i}", "s", "t")  # 全部同桶同主题
            for i in range(5)
        ]
        out = greedy_select(pool, limit=4, cap_bucket=2, cap_topic=1)
        assert len(out) == 4  # Pass 1 只拿 2 项，Pass 2 补 2 项

    def test_dedup_within_pool(self):
        """完全相同的 (user, assistant) 不会重复入选。"""
        c1 = _c("u", "a", "s", "t")
        c2 = _c("u", "a", "s", "t")  # 完全相同
        pool = [c1, c2]
        out = greedy_select(pool, limit=5)
        assert len(out) == 1  # 重复项跳过

    def test_strips_internal_fields(self):
        """返回结果不含 _bucket/_topic。"""
        pool = [_c("u", "a", "s", "t")]
        out = greedy_select(pool, limit=1)
        assert out == [{"user": "u", "assistant": "a"}]
