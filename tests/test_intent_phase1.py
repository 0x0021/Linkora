"""Phase 1 单一真源：domain.* 类别 + 关键词解析 + 配置覆盖 测试。"""
from __future__ import annotations

from src.intent import (
    IntentRegistry,
    LAYER_DOMAIN,
    default_registry,
)
from src.tools.base import BaseTool
from src.config import ToolsConfig
from src.tools.base import ToolRouter


def test_domain_categories_registered():
    """DEFAULT_INTENTS 包含若干 domain.* 类别（单一真源）。"""
    dom = [c for c in default_registry.all() if c.layer == LAYER_DOMAIN]
    assert len(dom) >= 10
    ids = {c.id for c in dom}
    assert "domain.weather" in ids
    assert "domain.web_search" in ids
    assert "domain.approval" in ids


def test_keywords_for_categories_resolves_source_of_truth():
    """关键词只维护在注册表，解析入口返回对应证据词。"""
    kws = default_registry.keywords_for_categories(["domain.weather"])
    assert "天气" in kws
    assert "带伞" in kws
    # 多类别合并去重
    both = default_registry.keywords_for_categories(["domain.weather", "domain.web_search"])
    assert "天气" in both and "搜索" in both


def test_keywords_for_categories_unknown_id_is_safe():
    """引用未注册类别 id 不抛异常，仅跳过并告警。"""
    kws = default_registry.keywords_for_categories(["domain.weather", "domain.nope"])
    assert "天气" in kws  # 有效类别仍解析
    assert "domain.nope" not in kws


def test_apply_intent_filter_domain_overrides_appends():
    """config.intent_filter.domain_overrides 追加域类别关键词（运维免改代码）。"""
    reg = IntentRegistry()
    before = set(reg.keywords_for_categories(["domain.weather"]))
    reg.apply_intent_filter({
        "domain_overrides": {"domain.weather": ["台风预警", "雾霾"]}
    })
    after = set(reg.keywords_for_categories(["domain.weather"]))
    assert "台风预警" in after and "雾霾" in after
    assert before.issubset(after)
    # 原有词不受影响
    assert "天气" in after


def test_validate_tool_intent_categories_warns_unknown():
    """未注册的意图类别触发告警（不抛异常，区别于 action 覆盖的硬失败）。"""
    reg = IntentRegistry()
    # 不应抛异常
    reg.validate_tool_intent_categories("my_tool", ["domain.weather", "domain.unknown"])
    # 已注册的不告警路径：直接调用不抛即可
    reg.validate_tool_intent_categories("my_tool", ["domain.weather"])


class _CatTool(BaseTool):
    def __init__(self, name, categories=None, keywords=None):
        self.name = name
        self.intent_categories = categories or []
        self.intent_keywords = keywords or []
        self.description = name
        self.parameters = {"type": "object", "properties": {}}

    def execute(self, args):
        return "ok"


def test_base_tool_effective_resolves_categories():
    """BaseTool.effective_intent_keywords：声明类别→解析；否则回退字面。"""
    t = _CatTool("get_weather", categories=["domain.weather"])
    assert "天气" in t.effective_intent_keywords
    # 无类别时回退字面
    t2 = _CatTool("legacy", keywords=["快照"])
    assert t2.effective_intent_keywords == ["快照"]


def test_tool_router_effective_used_in_matching():
    """ToolRouter 经 effective_intent_keywords 匹配（smart 精准暴露）。"""
    from src.llm.agent import LLMAgent
    from src.config import LlmConfig
    cfg = ToolsConfig(available=["send_message", "get_weather"], tool_routing_mode="smart")
    router = ToolRouter(cfg)
    router.register(_CatTool("send_message"))
    router.register(_CatTool("get_weather", categories=["domain.weather"]))
    agent = LLMAgent(config=LlmConfig(), client=None, tool_router=router)
    names = {t["function"]["name"] for t in agent._select_tools("今天天气怎么样")}
    assert "get_weather" in names
