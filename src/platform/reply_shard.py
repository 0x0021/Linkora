"""回复长度分片（F15）。

背景
----
钉钉 / 飞书 / 企业微信的文本与 markdown 消息都有单条长度上限（约 4096 字符，
不同平台与消息类型略有差异）。超限时平台的行为是**静默截断或直接拒绝**，
表现为「AI 回复被砍半」或「回复完全没发出去」，且 DWS 返回值不一定报错，
非常难排查。

本模块提供**纯函数**的分片能力：把超长回复切成若干片，每片都在限制内，
顺序发送时对用户呈现为连续的多条消息。

设计要点
--------
1. **保守统一上限**：三个平台在本项目里都走 markdown 通道（飞书在带 title 时
   发 ``--markdown``），实测上限均为 4096。取 4000 作为默认硬上限，留出续发
   标记与代码围栏补齐的余量。
2. **语义友好切分**：优先在「空行 > 换行 > 句末标点 > 空格」处断开，
   避免把一句话/一行 markdown 表格从中间劈开。只在窗口后 40% 内找断点，
   防止切出过短的碎片。
3. **代码围栏自愈**：如果切点落在 ``` 围栏内部，前一片自动补 ``` 收尾，
   后一片自动补回同样的 ``` 开头，保证每片单独渲染都不破版。
4. **续发标记**：多片时每片前缀 ``（i/n）``，让用户知道还有后续。

本模块不做任何 I/O、不依赖项目内其他模块，可独立单测。
"""

from __future__ import annotations

import math
import re

__all__ = [
    "REPLY_SHARD_LIMIT_DEFAULT",
    "shard_reply_text",
]

# 保守上限：平台实际约 4096，留 96 余量吸收平台侧的转义/包装开销
REPLY_SHARD_LIMIT_DEFAULT = 4000

# 断点优先级：句末标点（中英文）
_SENTENCE_ENDERS = "。！？；!?;."

# 断点最低位置比例：切点必须落在窗口的 60% 之后，否则宁可硬切，
# 避免出现「一片只有几十字、另一片撑满」的难看分布
_MIN_CUT_RATIO = 0.6

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")


def _find_cut(window: str, budget: int) -> int:
    """在 ``window``（长度 == budget）内找一个语义友好的切点，返回切点下标。

    返回值语义：``window[:cut]`` 为本片内容，剩余部分留给下一片。
    """
    floor_ = max(1, int(budget * _MIN_CUT_RATIO))

    # 1) 段落分隔（空行）——最理想
    for sep in ("\n\n", "\n"):
        idx = window.rfind(sep)
        if idx >= floor_:
            return idx + len(sep)

    # 2) 句末标点
    best = -1
    for ch in _SENTENCE_ENDERS:
        idx = window.rfind(ch)
        if idx > best:
            best = idx
    if best >= floor_:
        return best + 1

    # 3) 空格（英文长句兜底）
    idx = window.rfind(" ")
    if idx >= floor_:
        return idx + 1

    # 4) 硬切
    return budget


def _split_by_budget(text: str, budget: int) -> list[str]:
    """按 ``budget`` 字符预算把文本切成多段（不含任何标记与围栏修补）。"""
    pieces: list[str] = []
    rest = text
    while len(rest) > budget:
        cut = _find_cut(rest[:budget], budget)
        head = rest[:cut].rstrip()
        if not head:  # 极端情况（全是空白）：强制硬切，避免死循环
            head = rest[:budget]
            cut = budget
        pieces.append(head)
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        pieces.append(rest.rstrip())
    return pieces or [text]


def _unclosed_fence(chunk: str) -> str | None:
    """若 ``chunk`` 内存在未闭合的代码围栏，返回该围栏的开启行（如 ```python）。"""
    opener: str | None = None
    fence_char: str | None = None
    for line in chunk.split("\n"):
        m = _FENCE_RE.match(line)
        if not m:
            continue
        marker = m.group(1)
        if opener is None:
            opener = line.strip()
            fence_char = marker[0]
        elif fence_char and marker[0] == fence_char:
            opener = None
            fence_char = None
    return opener


def _balance_code_fences(pieces: list[str]) -> list[str]:
    """跨片修补代码围栏：前片补收尾、后片补开头，保证每片可独立渲染。"""
    out: list[str] = []
    carry: str | None = None
    for piece in pieces:
        body = f"{carry}\n{piece}" if carry else piece
        opener = _unclosed_fence(body)
        if opener:
            closer = "```" if opener.lstrip().startswith("`") else "~~~"
            body = f"{body.rstrip()}\n{closer}"
        carry = opener
        out.append(body)
    return out


def shard_reply_text(
    text: str,
    limit: int = REPLY_SHARD_LIMIT_DEFAULT,
    *,
    marker: bool = True,
) -> list[str]:
    """把超长回复切分为多片，每片长度均 ``<= limit``。

    Args:
        text: 待发送的回复正文（已过滤敏感词、已拼好引用脚注）。
        limit: 单片字符上限，默认 :data:`REPLY_SHARD_LIMIT_DEFAULT`。
        marker: 多片时是否给每片加 ``（i/n）`` 续发标记。

    Returns:
        分片列表。未超限时返回 ``[text]``（**原样不加任何标记**，
        保证绝大多数正常回复的行为与分片前完全一致）。
    """
    if not text:
        return [text]
    if limit <= 0 or len(text) <= limit:
        return [text]

    has_fence = "```" in text or "~~~" in text
    # 围栏自愈最多给每片补一行开头 + 一行收尾，预留固定余量
    fence_reserve = 48 if has_fence else 0

    n_guess = max(2, math.ceil(len(text) / limit))
    pieces: list[str] = []
    for _ in range(6):
        marker_len = len(f"（{n_guess}/{n_guess}）\n\n") if marker else 0
        budget = max(1, limit - marker_len - fence_reserve)
        pieces = _balance_code_fences(_split_by_budget(text, budget))
        if len(pieces) <= n_guess:
            break
        n_guess = len(pieces)

    if len(pieces) <= 1:
        return [text] if len(text) <= limit else pieces

    if not marker:
        return pieces

    total = len(pieces)
    return [f"（{i}/{total}）\n\n{p}" for i, p in enumerate(pieces, 1)]
