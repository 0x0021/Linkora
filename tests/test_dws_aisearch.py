"""企业语义检索（aisearch）适配器与工具层测试。

关键回归点（来自 dws v1.0.63 实测）：
- 返回体是 ``{"result":[...], "searchEvidence":{...}, "success":true}``——``result``
  在**顶层**且是数组
- ``_searchEvidence`` 是完整性证据，**必须**透传：
  ``originalContentRead: false`` = 只是摘要片段、未读原文；
  ``queryMatch`` 的 ``no_text_evidence``/``partial_text_evidence`` = 语义候选非确认匹配
- ``aisearch person`` 在当前企业恒返回空（应用未开通），故未实现
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.dws_adapter import DwsAdapter
from src.tools.aisearch import (
    SearchEnterpriseBehaviorTool,
    SearchEnterpriseContentTool,
)


@pytest.fixture
def adapter():
    return DwsAdapter(cli_path="dws", dry_run=False)


def _resp(result: list) -> MagicMock:
    import json
    return MagicMock(returncode=0,
                     stdout=json.dumps({
                         "arguments": [], "errorCode": None, "errorMsg": None,
                         "result": result, "searchEvidence": {}, "success": True,
                     }, ensure_ascii=False),
                     stderr="")


_ITEM = {
    "title": "示例日程标题",
    "snippet": "示例摘要内容",
    "sourceType": "calendar",
    "sourceName": "日程",
    "date": "2026-10-19 09:15:00",
    "actor": "100001",
    "url": "dingtalk://example",
    "_searchEvidence": {
        "contentKind": "search_snippet",
        "originalContentRead": False,
        "queryMatch": "not_requested",
        "timeEvidence": {"withinRequestedRange": True, "status": "verified"},
    },
}


# ============ 返回体归一 ============

class TestAisearchItems:
    def test_result_array_at_top_level(self, adapter):
        items = adapter._aisearch_items({"result": [_ITEM], "success": True})
        assert len(items) == 1
        assert items[0]["title"] == "示例日程标题"

    def test_plain_list_accepted(self, adapter):
        assert adapter._aisearch_items([_ITEM]) == [_ITEM]

    @pytest.mark.parametrize("bad", [None, "str", 42, {}])
    def test_invalid_shapes_return_empty(self, adapter, bad):
        assert adapter._aisearch_items(bad) == []

    def test_nested_items_key_accepted(self, adapter):
        items = adapter._aisearch_items({"result": {"items": [_ITEM]}})
        assert len(items) == 1


class TestAisearchNormalize:
    def test_evidence_translated_to_snake_case(self, adapter):
        out = adapter._normalize_aisearch_items([_ITEM])[0]
        assert out["source_type"] == "calendar"
        assert out["evidence"]["content_kind"] == "search_snippet"
        assert out["evidence"]["original_content_read"] is False
        assert out["evidence"]["query_match"] == "not_requested"
        assert out["evidence"]["within_requested_range"] is True

    def test_non_dict_item_skipped(self, adapter):
        assert adapter._normalize_aisearch_items(["坏数据", None]) == []

    def test_missing_evidence_yields_empty_evidence(self, adapter):
        out = adapter._normalize_aisearch_items([{"title": "无证据"}])[0]
        assert out["evidence"]["content_kind"] == ""
        assert out["evidence"]["original_content_read"] is False


class TestAisearchTypeNormalization:
    def test_valid_types_kept(self, adapter):
        assert adapter._aisearch_normalize_types("im,mail") == "im,mail"

    def test_invalid_types_dropped(self, adapter):
        assert adapter._aisearch_normalize_types("im,not_a_type") == "im"

    def test_all_invalid_falls_back_to_all(self, adapter):
        assert adapter._aisearch_normalize_types("bogus") == "all"

    def test_all_removed_when_specific_present(self, adapter):
        """all 与具体类型并存语义含混，只留具体类型。"""
        assert adapter._aisearch_normalize_types("all,im") == "im"

    def test_empty_falls_back_to_all(self, adapter):
        assert adapter._aisearch_normalize_types("") == "all"


# ============ 命令形态 ============

class TestAisearchCommands:
    def test_enterprise_command_shape(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _resp([_ITEM])
            out = adapter.aisearch_enterprise(queries="方案", types="document",
                                              time_range="本周")
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["dws", "aisearch", "enterprise"]
        assert cmd[cmd.index("--queries") + 1] == "方案"
        assert cmd[cmd.index("--types") + 1] == "document"
        assert cmd[cmd.index("--time-range") + 1] == "本周"
        assert out[0]["title"] == "示例日程标题"

    def test_enterprise_omits_empty_slots(self, adapter):
        """queries/time_range 为空时不应传空串参数。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _resp([])
            adapter.aisearch_enterprise()
        cmd = mock_run.call_args[0][0]
        assert "--queries" not in cmd
        assert "--time-range" not in cmd
        assert cmd[cmd.index("--types") + 1] == "all"

    def test_behavior_command_shape(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _resp([])
            adapter.aisearch_behavior(behavior_type="send", direction="我->某人",
                                      chat_scope="某群")
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["dws", "aisearch", "behavior"]
        assert cmd[cmd.index("--behavior-type") + 1] == "send"
        assert cmd[cmd.index("--direction") + 1] == "我->某人"
        assert cmd[cmd.index("--chat-scope") + 1] == "某群"

    def test_behavior_type_all_not_passed(self, adapter):
        """all 是默认值，传上去无意义且可能与具体值语义冲突。"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _resp([])
            adapter.aisearch_behavior(behavior_type="all")
        cmd = mock_run.call_args[0][0]
        assert "--behavior-type" not in cmd

    def test_invalid_behavior_type_dropped(self, adapter):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _resp([])
            adapter.aisearch_behavior(behavior_type="bogus")
        cmd = mock_run.call_args[0][0]
        assert "--behavior-type" not in cmd

    def test_cli_failure_returns_empty_not_raise(self, adapter):
        from src.dws_adapter import DwsError
        with patch.object(adapter, "run", side_effect=DwsError("boom")):
            assert adapter.aisearch_enterprise() == []
            assert adapter.aisearch_behavior() == []

    def test_person_not_implemented(self, adapter):
        """aisearch person 在当前企业恒返回空，刻意不实现（避免死工具）。"""
        assert not hasattr(adapter, "aisearch_person")


# ============ 工具层 ============

class _FakeAdapter:
    def __init__(self, items=None):
        self._items = items if items is not None else []

    def aisearch_enterprise(self, queries="", *, types="all", time_range=""):
        return self._items

    def aisearch_behavior(self, queries="", *, types="all", time_range="",
                          behavior_type="all", direction="", chat_scope=""):
        return self._items


class TestSearchEnterpriseContentTool:
    def test_evidence_made_human_readable(self):
        fake = _FakeAdapter([{
            "title": "t", "snippet": "s", "source_type": "calendar",
            "source_name": "日程", "date": "2026-10-19", "actor": "1", "url": "u",
            "evidence": {"content_kind": "search_snippet",
                         "original_content_read": False,
                         "query_match": "no_text_evidence",
                         "within_requested_range": True},
        }])
        out = SearchEnterpriseContentTool(fake).execute({"types": "all"})
        ev = out["items"][0]["evidence"]
        assert ev["is_snippet_only"] is True
        assert ev["is_semantic_candidate"] is True
        assert out["items"][0]["has_url"] is True

    def test_snippet_only_hint_when_all_are_snippets(self):
        fake = _FakeAdapter([{
            "title": "t", "snippet": "s", "source_type": "im",
            "evidence": {"original_content_read": False, "query_match": "not_requested"},
        }])
        out = SearchEnterpriseContentTool(fake).execute({})
        assert "摘要片段" in out["hint"]

    def test_no_hint_when_original_content_read(self):
        fake = _FakeAdapter([{
            "title": "t", "snippet": "s", "source_type": "im",
            "evidence": {"original_content_read": True, "query_match": "not_requested"},
        }])
        out = SearchEnterpriseContentTool(fake).execute({})
        assert "摘要片段" not in out["hint"]

    def test_empty_result_gives_hint(self):
        out = SearchEnterpriseContentTool(_FakeAdapter()).execute({})
        assert out["count"] == 0
        assert out["hint"]

    def test_long_snippet_truncated(self):
        fake = _FakeAdapter([{
            "title": "t", "snippet": "x" * 5000, "source_type": "im",
            "evidence": {"original_content_read": True},
        }])
        out = SearchEnterpriseContentTool(fake).execute({})
        assert len(out["items"][0]["snippet"]) == 1200

    def test_adapter_failure_returns_error_dict(self):
        from src.im_adapter.errors import IMAdapterError

        class _Boom(_FakeAdapter):
            def aisearch_enterprise(self, *a, **k):
                raise IMAdapterError("boom")

        out = SearchEnterpriseContentTool(_Boom()).execute({})
        assert "error" in out


class TestSearchEnterpriseBehaviorTool:
    def test_returns_count_and_items(self):
        fake = _FakeAdapter([{"title": "t", "snippet": "s", "source_type": "im",
                              "date": "2026-10-22", "url": "u",
                              "evidence": {"original_content_read": False}}])
        out = SearchEnterpriseBehaviorTool(fake).execute({"behavior_type": "send"})
        assert out["count"] == 1
        assert out["behavior_type"] == "send"

    def test_empty_result_points_to_search_messages(self):
        """空结果应引导到能枚举完整消息集合的 search_messages。"""
        out = SearchEnterpriseBehaviorTool(_FakeAdapter()).execute({})
        assert "search_messages" in out["hint"]

    def test_adapter_failure_returns_error_dict(self):
        from src.im_adapter.errors import IMAdapterError

        class _Boom(_FakeAdapter):
            def aisearch_behavior(self, *a, **k):
                raise IMAdapterError("boom")

        out = SearchEnterpriseBehaviorTool(_Boom()).execute({})
        assert "error" in out
