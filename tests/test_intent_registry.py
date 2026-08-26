"""IntentRegistry 边界用例（P3-7 加固）。

聚焦注册表纯逻辑：覆盖注册、缺失查询、工具↔行动意图反查、未注册类别跳过、
关键词合并、词边界匹配。不触发任何 IO/网络。
"""
from __future__ import annotations

from src.intent.registry import IntentRegistry, TOOL_ACTION_MAP, default_registry
from src.intent.types import IntentCategory


def _cat(cid: str, keywords=None) -> IntentCategory:
    return IntentCategory(
        id=cid,
        name=cid,
        layer="action",
        definition="测试类别",
        trigger="测试触发",
        evidence_keywords=keywords or [],
        max_length=80,
    )


def test_default_registry_loaded():
    assert len(default_registry.all()) > 0


def test_get_missing_returns_none():
    reg = IntentRegistry()
    assert reg.get("__not_exist__") is None


def test_register_overrides_existing():
    reg = IntentRegistry()
    first = _cat("x.override", ["a"])
    second = _cat("x.override", ["b", "c"])
    reg.register(first)
    reg.register(second)
    assert reg.get("x.override").evidence_keywords == ["b", "c"]


def test_tool_action_categories_missing_tool_empty():
    assert TOOL_ACTION_MAP.get("__no_such_tool__", []) == []
    # 运行时查询缺失工具也应返回空列表而非抛错
    reg = IntentRegistry()
    assert reg.tool_action_categories("__no_such_tool__") == []


def test_tools_for_action_category_reverse_lookup():
    tools = default_registry.tools_for_action_category("action.execute")
    assert "send_message" in tools


def test_keywords_for_categories_skips_unregistered():
    reg = IntentRegistry()
    # 含未注册类别不应抛错，已注册类别的关键词正常返回
    kws = reg.keywords_for_categories(["action.query", "__ghost__"])
    assert isinstance(kws, list)
    # action.query 下的工具关键词应非空
    assert len(kws) > 0


def test_apply_intent_filter_noop_when_empty():
    reg = IntentRegistry()
    before = len(reg.all())
    reg.apply_intent_filter({})
    assert len(reg.all()) == before  # 空过滤不改变类别数


def test_category_matches_word_boundary():
    reg = IntentRegistry()
    reg.register(_cat("test.boundary", ["日志"]))
    assert reg.category_matches("test.boundary", "请给我日志", "请给我日志") is True
    # 词边界：证据词「会议记录」不在「会议纪要」中，不应误命中
    reg.register(_cat("test.boundary2", ["会议记录"]))
    assert reg.category_matches("test.boundary2", "会议纪要", "会议纪要") is False
