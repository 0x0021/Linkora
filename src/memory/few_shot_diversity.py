"""Few-shot 样例多样性挑选的纯函数工具集。

原 `recommend_few_shot_pairs` 把这些辅助函数作为方法内闭包，单测只能起 SQLiteStore。
拆出后能用纯单元测试覆盖：归一化、长度分桶、主题键、trigram 相似度、贪心挑选逻辑。
"""

from __future__ import annotations

import re
from typing import Optional

# 与原方法内正则一致，避免重复编译
_RE_NORMALIZE_WS = re.compile(r"\s+")
_RE_OPENER = re.compile(r"^([\u4e00-\u9fa5A-Za-z]{2,4})")


def normalize(s: str) -> str:
    """归一化：去空白 + 小写。用于样例排除集键、相似度比较。"""
    return _RE_NORMALIZE_WS.sub("", (s or "")).lower()


def len_bucket(reply: str) -> str:
    """回复长度分桶：s（≤20）/ m（≤60）/ l（其余）。"""
    n = len(reply or "")
    return "s" if n <= 20 else ("m" if n <= 60 else "l")


def topic_key(user: str) -> str:
    """用户侧主题键：取 2~4 字汉字/字母开头；空时回退到归一化前 3 字。"""
    t = (user or "").strip()
    m = _RE_OPENER.match(t)
    return m.group(1) if m else (normalize(t)[:3] or "NA")


def trigrams(s: str) -> set:
    """字符串字符 trigram 集合（按归一化后的字符）。短串退化为 {s}。"""
    s = normalize(s)
    if len(s) <= 2:
        return {s} if s else set()
    return {s[i:i + 3] for i in range(len(s) - 2)}


def trigram_similarity(a: str, b: str) -> float:
    """trigram Jaccard 相似度 ∈ [0, 1]。零向量退化为 1.0（同为空）。"""
    A, B = trigrams(a), trigrams(b)
    if not A and not B:
        return 1.0
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def score_candidate(
    bucket: str,
    topic: str,
    bucket_counts: dict,
    topic_counts: dict,
    cap_bucket: int,
    cap_topic: int,
) -> int:
    """多样性打分：桶未满 +2，主题未满 +1。满桶/满主题项优先被丢弃。"""
    s = 0
    if bucket_counts.get(bucket, 0) < cap_bucket:
        s += 2
    if topic_counts.get(topic, 0) < cap_topic:
        s += 1
    return s


def greedy_select(
    pool: list[dict],
    limit: int,
    cap_bucket: int = 2,
    cap_topic: int = 2,
) -> list[dict]:
    """带多样性约束的贪心挑选。

    每个 pool 项必须含 _bucket / _topic 字段；返回时去掉这两个内部字段。
    两轮：
    - Pass 1：按 score_candidate 排序，丢弃「桶/主题都已满且还有更稀疏候选」的项
    - Pass 2：仍不足 limit 时按原顺序补满（pool 是按出现顺序排的近似 FIFO）

    返回值：不含 _bucket/_topic 字段的干净 dict（仅 user/assistant）。
    """
    if not pool:
        return []
    bucket_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    results: list[dict] = []
    used_keys: set = set()
    remaining = pool[:]

    def _strip_internal(c: dict) -> dict:
        return {"user": c["user"], "assistant": c["assistant"]}

    while remaining and len(results) < limit:
        remaining.sort(
            key=lambda c: score_candidate(
                c["_bucket"], c["_topic"],
                bucket_counts, topic_counts,
                cap_bucket, cap_topic,
            ),
            reverse=True,
        )
        picked = remaining[0]
        b, tk = picked["_bucket"], picked["_topic"]
        over_cap = (
            bucket_counts.get(b, 0) >= cap_bucket
            or topic_counts.get(tk, 0) >= cap_topic
        )
        # 若已超桶/主题上限，且仍有未达上限的可选项，则丢弃该项
        if over_cap and any(
            bucket_counts.get(c["_bucket"], 0) < cap_bucket
            or topic_counts.get(c["_topic"], 0) < cap_topic
            for c in remaining
        ):
            remaining.pop(0)
            continue
        remaining.pop(0)
        k = (picked["user"], picked["assistant"])
        if k in used_keys:
            continue
        used_keys.add(k)
        results.append(_strip_internal(picked))
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
        topic_counts[tk] = topic_counts.get(tk, 0) + 1

    if len(results) < limit:
        for c in pool:
            if len(results) >= limit:
                break
            k = (c["user"], c["assistant"])
            if k in used_keys:
                continue
            used_keys.add(k)
            results.append(_strip_internal(c))

    return results