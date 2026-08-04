"""Tools Utils 模块单元测试 — 覆盖 safe_int, safe_float, _clean_text, split_text。"""
from __future__ import annotations


from src.tools.utils import split_text
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
        # use sentences to force multi-chunk with overlap
        a = "开头。" + "A" * 300 + "。"
        b = "B" * 300 + "。结尾。"
        result = split_text(a + b, max_len=200, overlap=20)
        assert len(result) > 1
        # second chunk should contain overlap from first
        first_end = result[0][-20:]
        second_start = result[1][:len(first_end)]
        assert first_end == second_start

    def test_short_paras_accumulated_then_long_para_triggers_flush(self):
        """line 91/103: 积累短段落后溢出 → flush + 新段落独立开始。"""
        text = "段落A\n\n段落B\n\n段落C\n\n段落D"
        result = split_text(text, max_len=18)
        # 段落A+B+C 累积到溢出，触发 flush(line 91)後段落D单独(line 103)
        assert len(result) >= 2
        # 第一个 chunk 含 A+B+C，第二个含 D
        assert "段落D" in result[-1]
