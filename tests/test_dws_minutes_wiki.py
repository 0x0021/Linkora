"""DWS Adapter · minutes(AI 听记) / wiki(知识库) 读取方法单元测试。

覆盖 dws_adapter.DwsAdapter 新增的 minutes 与 wiki 封装：
- minutes_list / minutes_get_summary / todos / transcription / info
- wiki_space_list / wiki_space_search / wiki_node_list / wiki_node_search
验证命令前缀、必填 flag 透传，以及 _get_result 解析与失败兜底。
（dws CLI 调用经 MagicMock 拦截，不真正执行子进程。）
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.dws_adapter import DwsAdapter


def _make_adapter(run_return):
    adapter = DwsAdapter(cli_path="dws", timeout=5)
    adapter.run = MagicMock(return_value=run_return)
    return adapter


class TestMinutesMethods:
    def test_minutes_list_default_scope_mine(self):
        adapter = _make_adapter({"result": [{"id": "m1"}]})
        res = adapter.minutes_list()
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["minutes", "list", "mine"]
        assert "--limit" in args
        assert res == [{"id": "m1"}]

    def test_minutes_list_all_with_filters(self):
        adapter = _make_adapter({"result": []})
        adapter.minutes_list(scope="all", query="周会",
                             start="2026-01-01T00:00:00+08:00",
                             end="2026-01-31T23:59:59+08:00", limit=20, cursor="tok")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["minutes", "list", "all"]
        assert "--query" in args and "周会" in args
        assert "--start" in args and "--end" in args and "--cursor" in args and "tok" in args

    def test_minutes_list_invalid_scope_falls_back_to_mine(self):
        adapter = _make_adapter({"result": []})
        adapter.minutes_list(scope="bogus")
        assert adapter.run.call_args[0][0][2] == "mine"

    def test_minutes_get_summary_passes_id(self):
        adapter = _make_adapter({"result": {"summary": "x"}})
        r = adapter.minutes_get_summary("uuid-1")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["minutes", "get", "summary"]
        assert "--id" in args and "uuid-1" in args
        assert r == {"summary": "x"}

    def test_minutes_get_todos_and_transcription(self):
        adapter = _make_adapter({"result": []})
        adapter.minutes_get_todos("u1")
        assert adapter.run.call_args[0][0][:3] == ["minutes", "get", "todos"]
        adapter.minutes_get_transcription("u1")
        assert adapter.run.call_args[0][0][:3] == ["minutes", "get", "transcription"]

    def test_minutes_get_none_on_error(self):
        adapter = _make_adapter(None)
        adapter.run.side_effect = RuntimeError("boom")
        assert adapter.minutes_get_info("u1") is None


class TestWikiMethods:
    def test_wiki_space_list_default_type(self):
        adapter = _make_adapter({"result": [{"name": "KB"}]})
        res = adapter.wiki_space_list()
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["wiki", "space", "list"]
        assert "--type" in args and "orgWikiSpace" in args
        assert res == [{"name": "KB"}]

    def test_wiki_space_search_requires_query(self):
        adapter = _make_adapter({"result": []})
        adapter.wiki_space_search("产品文档", space_type="orgWikiSpace", limit=10)
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["wiki", "space", "search"]
        assert "--query" in args and "产品文档" in args
        assert "--type" in args

    def test_wiki_node_list_requires_workspace(self):
        adapter = _make_adapter({"result": []})
        adapter.wiki_node_list("ws-1", folder="f-2", limit=50, cursor="c")
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["wiki", "node", "list"]
        assert "--workspace" in args and "ws-1" in args
        assert "--folder" in args and "f-2" in args
        assert "--cursor" in args and "c" in args

    def test_wiki_node_search_requires_workspace_and_query(self):
        adapter = _make_adapter({"result": []})
        adapter.wiki_node_search("ws-1", "合同", extensions="pdf,adoc", limit=20)
        args = adapter.run.call_args[0][0]
        assert args[:3] == ["wiki", "node", "search"]
        assert "--workspace" in args and "ws-1" in args
        assert "--query" in args and "合同" in args
        assert "--extensions" in args and "pdf,adoc" in args

    def test_wiki_methods_empty_on_non_list(self):
        adapter = _make_adapter({"result": {"x": 1}})
        assert adapter.wiki_space_list() == []
        assert adapter.wiki_node_list("ws") == []
