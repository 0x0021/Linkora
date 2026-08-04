"""意图关键词匹配逻辑。

提供 match_keyword 函数用于意图分类时的证据词匹配，
以及 _SOCIAL_PRIORITY 用于社交子型的判定优先级。
"""
from __future__ import annotations

import functools
import re


# ---------------------------------------------------------------------------
# 关键词匹配（词边界防护，迁入自 rule_engine._match_intent_keyword）
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def _compile_keyword_pattern(kw: str) -> re.Pattern:
    """编译短英文关键词的正则模式（带缓存），避免每次重新编译。"""
    return re.compile(r"\b" + re.escape(kw) + r"\b")


def match_keyword(kw: str, content: str, content_lower: str) -> bool:
    """意图关键词匹配（防短英文子串误匹配）。

    对纯 ASCII 且长度 ≤ 3 的关键词（如 OK/ok/Hi/hi）使用单词边界匹配，
    避免 "OK" 误匹配 "ACME"、"Hi" 误匹配 "China" 等场景。
    中文关键词和长英文词仍用普通子串匹配。
    """
    # 防御性类型检查（config.yaml 中未加引号的纯数字如 88 会被 YAML 解析为 int）
    if not isinstance(kw, str):
        return False
    # 短纯英文关键词 → 词边界匹配（\b 在 Unicode 下对中文也安全）
    if len(kw) <= 3 and kw.isascii() and kw.isalpha():
        pattern = _compile_keyword_pattern(kw)
        return bool(pattern.search(content)) or bool(pattern.search(content_lower))
    # 默认：普通子串匹配
    return kw in content or kw.lower() in content_lower


# ---------------------------------------------------------------------------
# social 子型在处置判定中的优先级。
# ---------------------------------------------------------------------------

# social 子型在处置判定中的优先级。
# 顺序约束：
#  - polite（问候）须早于 acknowledge，否则 "你好" 会因含单字"好"（确认词）被误判为确认收到而跳过。
#  - compliment 须早于 acknowledge，否则 "可以啊" 会被误判为确认收到；compliment 与 acknowledge
#    各命中 1 词时，compliment 优先归为赞许更贴合语义。
#  - 其余新子型（smalltalk/emotion）置于 acknowledge 之后、closing 之前，避免抢占既有判定。
_SOCIAL_PRIORITY = [
    "social.gratitude", "social.polite", "social.compliment",
    "social.acknowledge", "social.smalltalk", "social.emotion", "social.closing",
]
