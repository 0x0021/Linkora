"""消息格式自动分类：判断文本应以纯文本（text）还是 Markdown（markdown）发送。

背景与约束
----------
钉钉 ``dws`` 的 ``chat message send``（底层 ``send_personal_message``）对文本类内容
**始终**以 ``msgType: "markdown"`` 发送，CLI 并不存在独立的 text 线缆类型——
``--msg-type`` 仅用于富媒体（image/file/audio/video/location/profile），纯文本 / Markdown
共用默认分支。因此本项目里「短消息用 text、结构化用 markdown」通过**内容**体现：

- 分类为 ``text``    → 内容保持纯文本（在 markdown msgType 下渲染为纯文本气泡），不做 markdown 归一化；
- 分类为 ``markdown`` → 保留 markdown 结构，发送前按平台能力做表格等兼容归一化。

本模块只负责「语义决策」（输出 ``text`` / ``markdown``），由各适配器映射到具体线缆类型，
因此对未来支持原生 text 类型的平台（如飞书 / 企微）同样适用，不必改写分类逻辑。
"""
from __future__ import annotations

import re

# ---- 块级 / 行首结构标记 -------------------------------------------------
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")          # # 标题
_UNORDERED_RE = re.compile(r"^\s{0,3}[-*+]\s+\S")        # - 列表项
_ORDERED_RE = re.compile(r"^\s{0,3}\d{1,9}[.)]\s+\S")    # 1. 步骤
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?\S")           # > 引用
_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")  # --- / *** / ___
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")             # 围栏代码块
_TASK_LIST_RE = re.compile(r"^\s{0,3}[-*+]\s+\[[ xX]\]\s+\S")  # - [ ] 任务

# ---- 行内标记 -----------------------------------------------------------
_BOLD_RE = re.compile(r"(\*\*[^*\n]+?\*\*|__[^_\n]+?__)")   # **粗体** / __粗体__
_INLINE_CODE_RE = re.compile(r"`[^`\n]+?`")                  # `行内代码`
_LINK_RE = re.compile(r"\[[^\]\n]+\]\([^\s)]+\)")           # [文字](url)
# 斜体：成对单 *，内部非空白、非纯数字，避免把「3*4=12」「a * b * c」误判
_ITALIC_RE = re.compile(r"(?<!\*)\*[^*\s][^*\n]*?[^*\s]\*(?!\*)")

# 表格分隔行单元格：仅由 - 与对齐 : 组成
_SEP_CELL_RE = re.compile(r"^:?-{1,}:?$")


def _looks_like_table(lines: list[str]) -> bool:
    """检测是否存在 GFM 表格（表头行含 |，且紧接一行分隔行 |---|）。"""
    n = len(lines)
    for i in range(n - 1):
        line = lines[i]
        if "|" not in line:
            continue
        nxt = lines[i + 1].strip()
        if not (nxt.startswith("|") or "|" in nxt):
            continue
        sep = nxt.strip()
        if sep.startswith("|"):
            sep = sep[1:]
        if sep.endswith("|"):
            sep = sep[:-1]
        cells = [c.strip() for c in sep.split("|") if c.strip() != ""]
        if cells and all(_SEP_CELL_RE.match(c) is not None for c in cells):
            return True
    return False


def classify_message_format(text: str, *, min_markers: int = 1) -> str:
    """判断文本应作为 ``text`` 还是 ``markdown`` 发送。

    规则（结构优先）：
    1. 命中任一**块级 / 行首**结构标记（标题、列表、引用、分隔线、代码块、任务列表、
       表格）→ 直接判 ``markdown``；
    2. 否则统计**行内**标记（粗体 / 行内代码 / 链接 / 斜体）命中数，达到
       ``min_markers`` 阈值即判 ``markdown``；
    3. 其余（含空文本）→ ``text``。

    ``min_markers`` 控制「仅靠行内格式」时的灵敏度，默认 1（出现任意行内格式即 markdown）。
    该值 <= 0 时行内标记永不触发 markdown（极保守，所有无块级结构的内容都按 text 处理）。
    """
    if not text or not text.strip():
        return "text"

    lines = text.split("\n")

    # 1) 块级 / 行首结构标记
    strong = 0
    for line in lines:
        if _FENCE_RE.match(line):
            strong += 1
            continue
        if _HR_RE.match(line):
            strong += 1
            continue
        if _HEADER_RE.match(line):
            strong += 1
            continue
        if _TASK_LIST_RE.match(line):
            strong += 1
            continue
        if _UNORDERED_RE.match(line):
            strong += 1
            continue
        if _ORDERED_RE.match(line):
            strong += 1
            continue
        if _BLOCKQUOTE_RE.match(line):
            strong += 1
            continue
    if _looks_like_table(lines):
        strong += 1

    if strong >= 1:
        return "markdown"

    # 2) 行内标记（min_markers <= 0 时永不触发）
    if min_markers > 0:
        inline = 0
        inline += len(_BOLD_RE.findall(text))
        inline += len(_INLINE_CODE_RE.findall(text))
        inline += len(_LINK_RE.findall(text))
        inline += len(_ITALIC_RE.findall(text))
        if inline >= min_markers:
            return "markdown"

    return "text"
