"""Tools Utils 模块单元测试 — 覆盖 safe_int, safe_float, _clean_text, split_text。"""
from __future__ import annotations


from src.tools.utils import _semantic_tail, split_text
from src.tools.utils import safe_float
from src.tools.utils import safe_int


# ============================================================================
# safe_int
# ============================================================================
class TestSafeInt:
    def test_pure_int(self):
        assert safe_int(42, -1) == 42

    def test_string_int(self):
        assert safe_int("5", -1) == 5

    def test_float_string(self):
        assert safe_int("3.7", -1) == 3

    def test_float(self):
        assert safe_int(3.7, -1) == 3

    def test_none(self):
        assert safe_int(None, -1) == -1

    def test_empty_string(self):
        assert safe_int("", -1) == -1

    def test_garbage_string(self):
        assert safe_int("五条", -1) == -1

    def test_whitespace(self):
        assert safe_int("  10  ", -1) == 10

    def test_zero(self):
        assert safe_int(0, -1) == 0


# ============================================================================
# safe_float
# ============================================================================
class TestSafeFloat:
    def test_pure_float(self):
        assert safe_float(0.5, -1.0) == 0.5

    def test_string_float(self):
        assert safe_float("0.3", -1.0) == 0.3

    def test_int_input(self):
        assert safe_float(3, -1.0) == 3.0

    def test_none(self):
        assert safe_float(None, -1.0) == -1.0

    def test_empty_string(self):
        assert safe_float("", -1.0) == -1.0

    def test_garbage(self):
        assert safe_float("abc", -1.0) == -1.0

    def test_trailing_text(self):
        assert safe_float("0.3以上", -1.0) == -1.0


# ============================================================================
# split_text
# ============================================================================
class TestSplitText:
    def test_empty_text(self):
        assert split_text("") == []

    def test_short_text_single_chunk(self):
        result = split_text("hello world")
        assert len(result) == 1
        assert result[0] == "hello world"

    def test_html_tags_removed(self):
        result = split_text("<p>Hello <b>World</b></p>")
        assert "<" not in result[0]
        assert "Hello World" in result[0]

    def test_markdown_headers_removed(self):
        result = split_text("# Title\ncontent")
        assert "#" not in result[0]

    def test_markdown_bold_removed(self):
        result = split_text("**bold** text __underline__")
        chunk = result[0]
        assert "**" not in chunk
        assert "__" not in chunk

    def test_markdown_code_removed(self):
        result = split_text("`code` here ```block``` done")
        chunk = result[0]
        # inline code `` is removed; triple-backtick blocks are removed
        assert "`code`" not in chunk
        # the cleaned result retains "here done"
        assert "here" in chunk
        assert "done" in chunk

    def test_markdown_link_text_retained(self):
        result = split_text("[GitHub](https://github.com)")
        assert "GitHub" in result[0]

    def test_multiple_paragraphs_split(self):
        # _clean_text collapses whitespace, so we need sentence-level splitting
        text = ("第一段内容。" + "A" * 250 + "。" + "第二段内容。" )
        result = split_text(text, max_len=150)
        assert len(result) >= 2

    def test_sentence_splitting(self):
        text = "第一句。" + "A" * 600 + "。" + "第二句。"
        result = split_text(text, max_len=200)
        assert len(result) >= 2

    def test_overlap(self):
        # 多个短句打包成块 → 重叠窗口内才有真实边界可用
        text = "".join(f"第{i}句内容。" for i in range(1, 41))
        result = split_text(text, max_len=60, overlap=20)
        assert len(result) > 1
        # 第二块开头应是第一块尾部「语义对齐」的重叠（切口落在句子边界）
        tail = _semantic_tail(result[0], 20)
        assert tail
        assert result[1].startswith(tail)
        # 不应在词语中间硬切出碎片：重叠的每个片段都应是完整句子
        assert all(s.endswith("。") for s in tail.split("\n") if s)

    def test_overlap_capped_at_configured_overlap(self):
        """旧 bug：重叠上限误用 hard_max 派生值（≈整块前文重复拼入下一块）。
        修复后重叠严格 ≤ 配置的 overlap，块长不再随重叠滚雪球增长。"""
        text = "".join(f"第{i}句内容。" for i in range(1, 61))  # 每句 6 字
        result = split_text(text, max_len=60, overlap=20)
        assert len(result) > 2
        # 旧实现下 chunk 长度会滚雪球到 100+ / 180+；新实现每块 ≤ max_len+overlap 富余
        assert max(len(c) for c in result) <= 60 + 20 + 15

    def test_semantic_tail_snaps_to_boundary(self):
        # 窗口在词语中间截断时，丢弃开头残句、对齐到边界
        prev = "三、添加打印机。3.4 选择添加方式\n点击下一步。"
        # 窗口恰好切在 "选择添加方式" 中间 → 应回退到句号/换行边界
        tail = _semantic_tail(prev, 12)
        assert tail == "点击下一步。"

    def test_semantic_tail_no_boundary_returns_empty(self):
        # 窗口内无任何边界符（无标点长串）→ 返回空，宁可不带重叠也不注入碎片
        assert _semantic_tail("A" * 100, 20) == ""
        assert _semantic_tail("", 20) == ""
        assert _semantic_tail("abc", 0) == ""

    def test_short_paras_accumulated_then_long_para_triggers_flush(self):
        """短段落累积到软上限即 flush；溢出后的段落独立成块。"""
        text = "段落A\n\n段落B\n\n段落C\n\n段落D"
        result = split_text(text, max_len=12)
        # 块内用单个 \n 连接：A+B+C=11 ≤12；再加 D 超限 → flush
        assert len(result) >= 2
        assert "段落D" in result[-1]
