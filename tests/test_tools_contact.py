"""搜索联系人工具测试。"""
from unittest.mock import MagicMock

from src.tools.contact import SearchContactTool


class TestSearchContact:
    def test_search_success(self):
        dws = MagicMock()
        dws.contact_user_search.return_value = [
            {"name": "张三", "title": "工程师", "userId": "u001", "nick": "三三"},
            {"name": "李四", "title": "经理", "userId": "u002", "nick": ""},
        ]
        tool = SearchContactTool(dws)
        res = tool.execute({"keyword": "张"})
        assert res["count"] == 2
        assert res["contacts"][0]["name"] == "张三"
        assert res["contacts"][0]["user_id"] == "u001"
        assert res["contacts"][1]["nick"] == ""

    def test_missing_keyword(self):
        dws = MagicMock()
        tool = SearchContactTool(dws)
        res = tool.execute({})
        assert "error" in res

    def test_empty_keyword(self):
        dws = MagicMock()
        tool = SearchContactTool(dws)
        res = tool.execute({"keyword": ""})
        assert "error" in res

    def test_no_results(self):
        dws = MagicMock()
        dws.contact_user_search.return_value = []
        tool = SearchContactTool(dws)
        res = tool.execute({"keyword": "Nobody"})
        assert res["count"] == 0
        assert res["contacts"] == []
