"""钉钉文档工具测试。"""
from unittest.mock import MagicMock

from src.tools.doc import GetDocContentTool, SearchDocTool


class TestSearchDoc:
    def test_search_success(self):
        dws = MagicMock()
        dws.doc_search.return_value = [
            {"name": "周报.docx", "nodeId": "n1", "nodeType": "FILE",
             "extension": "docx", "docUrl": "https://a.com/d/n1",
             "creatorUid": "u1"},
        ]
        tool = SearchDocTool(dws)
        res = tool.execute({"query": "周报"})
        assert res["count"] == 1
        doc = res["documents"][0]
        assert doc["name"] == "周报.docx"
        assert doc["node_id"] == "n1"
        assert doc["url"] == "https://a.com/d/n1"

    def test_missing_query(self):
        tool = SearchDocTool(MagicMock())
        res = tool.execute({})
        assert "error" in res

    def test_empty_query(self):
        tool = SearchDocTool(MagicMock())
        res = tool.execute({"query": ""})
        assert "error" in res

    def test_with_page_size(self):
        dws = MagicMock()
        dws.doc_search.return_value = []
        tool = SearchDocTool(dws)
        tool.execute({"query": "doc", "page_size": 20})
        dws.doc_search.assert_called_once_with("doc", page_size=20)

    def test_default_page_size(self):
        dws = MagicMock()
        dws.doc_search.return_value = []
        tool = SearchDocTool(dws)
        tool.execute({"query": "doc"})
        dws.doc_search.assert_called_once_with("doc", page_size=10)

    def test_empty_results(self):
        dws = MagicMock()
        dws.doc_search.return_value = []
        tool = SearchDocTool(dws)
        res = tool.execute({"query": "nothing"})
        assert res["count"] == 0
        assert res["documents"] == []


class TestGetDocContent:
    def test_success_dict_result_content(self):
        dws = MagicMock()
        dws.doc_read.return_value = {
            "result": {"content": "# 文档标题\n\n正文内容。"}
        }
        tool = GetDocContentTool(dws)
        res = tool.execute({"node_id": "n1"})
        assert res["node_id"] == "n1"
        assert "# 文档标题" in res["content"]
        assert res["length"] > 0

    def test_success_dict_result_markdown(self):
        dws = MagicMock()
        dws.doc_read.return_value = {
            "result": {"markdown": "**粗体**"}
        }
        tool = GetDocContentTool(dws)
        res = tool.execute({"node_id": "n2"})
        assert "**粗体**" in res["content"]

    def test_success_result_is_string(self):
        dws = MagicMock()
        dws.doc_read.return_value = {"result": "纯文本内容"}
        tool = GetDocContentTool(dws)
        res = tool.execute({"node_id": "n3"})
        assert res["content"] == "纯文本内容"

    def test_no_result_fallback_to_str(self):
        dws = MagicMock()
        dws.doc_read.return_value = {"result": {"other": "value"}}
        tool = GetDocContentTool(dws)
        res = tool.execute({"node_id": "n4"})
        assert "other" in res["content"]

    def test_missing_node_id(self):
        tool = GetDocContentTool(MagicMock())
        res = tool.execute({})
        assert "error" in res

    def test_empty_node_id(self):
        tool = GetDocContentTool(MagicMock())
        res = tool.execute({"node_id": ""})
        assert "error" in res

    def test_truncation(self):
        """超过 8000 字截断。"""
        dws = MagicMock()
        long_text = "X" * 9000
        dws.doc_read.return_value = {"result": {"content": long_text}}
        tool = GetDocContentTool(dws)
        res = tool.execute({"node_id": "n5"})
        assert len(res["content"]) == 8000 + len("\n\n... (内容已截断)")
        assert "已截断" in res["content"]

    def test_non_dict_data(self):
        dws = MagicMock()
        dws.doc_read.return_value = "直接字符串"
        tool = GetDocContentTool(dws)
        res = tool.execute({"node_id": "n6"})
        assert res["node_id"] == "n6"
