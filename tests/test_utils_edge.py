"""utils.py 边界用例补充。"""
from src.tools.utils import _clean_text, split_text


class TestCleanTextEdge:
    def test_empty(self):
        assert _clean_text("") == ""


class TestSplitLongTextEdge:
    def test_single_paragraph_exceeds_max(self):
        long_text = "以段落为单位。包含了长文本。超过了最大长度限制。" * 20
        chunks = split_text(long_text, max_len=60)
        assert len(chunks) >= 1

    def test_short_text(self):
        chunks = split_text("短文本。", max_len=500)
        assert len(chunks) == 1
