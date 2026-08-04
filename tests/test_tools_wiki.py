"""钉钉知识库（wiki）工具测试。"""
from unittest.mock import MagicMock

from src.tools.wiki import (
    WikiSpaceListTool,
    WikiSpaceSearchTool,
    WikiNodeListTool,
    WikiNodeSearchTool,
    _coerce_limit,
)


class TestCoerceLimit:
    def test_int(self):
        assert _coerce_limit(15, 20) == 15

    def test_zero_falls_back(self):
        assert _coerce_limit(0, 20) == 20

    def test_negative_falls_back(self):
        assert _coerce_limit(-5, 10) == 10

    def test_bad_type_falls_back(self):
        assert _coerce_limit("abc", 20) == 20

    def test_none_falls_back(self):
        assert _coerce_limit(None, 7) == 7


class TestWikiSpaceList:
    def test_success(self):
        dws = MagicMock()
        dws.wiki_space_list.return_value = [
            {"spaceId": "s1", "name": "组织知识库"},
            {"spaceId": "s2", "name": "我的库"},
        ]
        tool = WikiSpaceListTool(dws)
        res = tool.execute({"space_type": "myWikiSpace"})
        assert res["count"] == 2
        assert res["items"][0]["spaceId"] == "s1"
        _, kwargs = dws.wiki_space_list.call_args
        assert kwargs["space_type"] == "myWikiSpace"

    def test_default_space_type(self):
        dws = MagicMock()
        dws.wiki_space_list.return_value = []
        tool = WikiSpaceListTool(dws)
        tool.execute({"space_type": "bogus"})
        _, kwargs = dws.wiki_space_list.call_args
        assert kwargs["space_type"] == "orgWikiSpace"

    def test_empty(self):
        dws = MagicMock()
        dws.wiki_space_list.return_value = []
        tool = WikiSpaceListTool(dws)
        assert tool.execute({})["count"] == 0

    def test_non_list_result(self):
        dws = MagicMock()
        dws.wiki_space_list.return_value = {"err": "x"}
        tool = WikiSpaceListTool(dws)
        res = tool.execute({})
        assert res["count"] == 0
        assert res["items"] == []

    def test_exception(self):
        dws = MagicMock()
        dws.wiki_space_list.side_effect = RuntimeError("无权限")
        tool = WikiSpaceListTool(dws)
        res = tool.execute({})
        assert "error" in res
        assert "无权限" in res["error"]


class TestWikiSpaceSearch:
    def test_success(self):
        dws = MagicMock()
        dws.wiki_space_search.return_value = [{"spaceId": "s1", "name": "产品库"}]
        tool = WikiSpaceSearchTool(dws)
        res = tool.execute({"query": "产品"})
        assert res["count"] == 1
        assert res["query"] == "产品"
        _, kwargs = dws.wiki_space_search.call_args
        assert kwargs["query"] == "产品"

    def test_missing_query(self):
        dws = MagicMock()
        tool = WikiSpaceSearchTool(dws)
        res = tool.execute({})
        assert "error" in res

    def test_bad_space_type_normalized_to_none(self):
        dws = MagicMock()
        dws.wiki_space_search.return_value = []
        tool = WikiSpaceSearchTool(dws)
        tool.execute({"query": "x", "space_type": "nope"})
        _, kwargs = dws.wiki_space_search.call_args
        assert kwargs["space_type"] is None

    def test_exception(self):
        dws = MagicMock()
        dws.wiki_space_search.side_effect = RuntimeError("挂了")
        tool = WikiSpaceSearchTool(dws)
        res = tool.execute({"query": "x"})
        assert "error" in res and "挂了" in res["error"]


class TestWikiNodeList:
    def test_success(self):
        dws = MagicMock()
        dws.wiki_node_list.return_value = [{"nodeId": "n1", "title": "文档A"}]
        tool = WikiNodeListTool(dws)
        res = tool.execute({"workspace_id": "w1"})
        assert res["count"] == 1
        assert res["workspace_id"] == "w1"
        _, kwargs = dws.wiki_node_list.call_args
        assert kwargs["workspace_id"] == "w1"

    def test_missing_workspace(self):
        dws = MagicMock()
        tool = WikiNodeListTool(dws)
        res = tool.execute({})
        assert "error" in res

    def test_exception(self):
        dws = MagicMock()
        dws.wiki_node_list.side_effect = RuntimeError("无此空间")
        tool = WikiNodeListTool(dws)
        res = tool.execute({"workspace_id": "w1"})
        assert "error" in res and "无此空间" in res["error"]


class TestWikiNodeSearch:
    def test_success(self):
        dws = MagicMock()
        dws.wiki_node_search.return_value = [{"nodeId": "n1", "title": "季度规划"}]
        tool = WikiNodeSearchTool(dws)
        res = tool.execute({"workspace_id": "w1", "query": "季度"})
        assert res["count"] == 1
        assert res["query"] == "季度"
        _, kwargs = dws.wiki_node_search.call_args
        assert kwargs["workspace_id"] == "w1" and kwargs["query"] == "季度"

    def test_missing_workspace(self):
        dws = MagicMock()
        tool = WikiNodeSearchTool(dws)
        res = tool.execute({"query": "x"})
        assert "error" in res

    def test_missing_query(self):
        dws = MagicMock()
        tool = WikiNodeSearchTool(dws)
        res = tool.execute({"workspace_id": "w1"})
        assert "error" in res

    def test_exception(self):
        dws = MagicMock()
        dws.wiki_node_search.side_effect = RuntimeError("搜索失败")
        tool = WikiNodeSearchTool(dws)
        res = tool.execute({"workspace_id": "w1", "query": "x"})
        assert "error" in res and "搜索失败" in res["error"]


class TestPlatformAndIntent:
    def test_dingtalk_only(self):
        for cls in (WikiSpaceListTool, WikiSpaceSearchTool, WikiNodeListTool, WikiNodeSearchTool):
            assert cls.platforms == ["dingtalk"]
            assert cls.intent_categories == ["domain.wiki"]
