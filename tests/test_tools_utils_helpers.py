"""Tools utils 辅助函数（arg_str / list_result）单元测试。"""
from __future__ import annotations

from src.tools.utils import arg_str, list_result


class TestArgStr:
    def test_present(self):
        assert arg_str({"a": "x"}, "a") == "x"

    def test_strips(self):
        assert arg_str({"a": "  x  "}, "a") == "x"

    def test_missing_returns_default(self):
        assert arg_str({}, "a", "def") == "def"

    def test_none_returns_default(self):
        assert arg_str({"a": None}, "a", "def") == "def"

    def test_explicit_null(self):
        # LLM 显式传 null 应回退默认，而非 "None"
        assert arg_str({"a": None}, "a", "") == ""


class TestListResult:
    def test_list(self):
        out = list_result([1, 2, 3], 10, source="bing")
        assert out == {"source": "bing", "count": 3, "items": [1, 2, 3]}

    def test_limit(self):
        out = list_result([1, 2, 3, 4], 2)
        assert out["count"] == 4
        assert out["items"] == [1, 2]

    def test_non_list_input(self):
        out = list_result(None, 5)
        assert out["count"] == 0
        assert out["items"] == []

    def test_extra_kwargs(self):
        out = list_result(["a"], 5, query="q", merged_sources=["bing"])
        assert out["query"] == "q"
        assert out["merged_sources"] == ["bing"]
