"""抽象意图分类体系（src/intent）测试。

验证：
- 默认意图类别（处置层 + 行动层）齐全、层次正确
- classify_disposition 与旧 _detect_intent 行为等价（business/social 子型、长度阈值、词边界）
- match_action_categories / TOOL_ACTION_MAP 抽象行动意图映射
- agent 工具选择新增『抽象行动意图』命中路径（无需具体场景词也能精准暴露）
- config.intent_filter 关键词覆盖（向后兼容）
"""
from __future__ import annotations

from src.config import LlmConfig, RulesConfig, ToolsConfig
from src.intent import (
    IntentRegistry,
    LAYER_ACTION,
    LAYER_DISPOSITION,
    TOOL_ACTION_MAP,
    match_keyword,
)
from src.llm.agent import LLMAgent
from src.rule_engine import RuleEngine
from src.tools.base import BaseTool, ToolRouter


# ---------------------------------------------------------------------------
# 1. 默认类别结构
# ---------------------------------------------------------------------------

def test_default_disposition_categories():
    reg = IntentRegistry()
    disp = [c for c in reg.all() if c.layer == LAYER_DISPOSITION]
    ids = {c.id for c in disp}
    assert "business" in ids
    assert {"social.gratitude", "social.acknowledge", "social.closing", "social.polite"} <= ids
    # 社交子型都指向 social 父节点
    for sid in ("social.gratitude", "social.acknowledge", "social.closing", "social.polite"):
        assert reg.get(sid).parent == "social"


def test_default_action_categories():
    reg = IntentRegistry()
    act = [c.id for c in reg.all() if c.layer == LAYER_ACTION]
    assert act == ["action.query", "action.execute", "action.analyze",
                   "action.communicate", "action.media",
                   "action.monitor", "action.subscribe"]


def test_action_categories_have_definitions_and_triggers():
    reg = IntentRegistry()
    for c in reg.all():
        assert c.definition.strip(), f"{c.id} 缺少语义边界定义"
        assert c.trigger.strip(), f"{c.id} 缺少触发条件"


# ---------------------------------------------------------------------------
# 2. 处置层判定（等价旧 _detect_intent）
# ---------------------------------------------------------------------------

def test_classify_business_by_keyword():
    reg = IntentRegistry()
    assert reg.classify_disposition("北京天气怎么样").disposition == "business"
    assert reg.classify_disposition("帮我查一下审批进度").disposition == "business"
    assert reg.classify_disposition("").disposition == "business"  # 空消息


def test_classify_gratitude_short():
    reg = IntentRegistry()
    r = reg.classify_disposition("谢谢")
    assert r.disposition == "social" and r.subtype == "thank_you"


def test_classify_acknowledge_word_boundary():
    reg = IntentRegistry()
    # 纯确认短消息
    assert reg.classify_disposition("OK").subtype == "acknowledge"
    assert reg.classify_disposition("收到").subtype == "acknowledge"
    # 词边界防护：ACME 不应误匹配 OK
    assert match_keyword("OK", "ACME", "acme") is False
    # 含业务关键词则归 business（即便含 OK）
    assert reg.classify_disposition("ACME 股票上市情况").disposition == "business"


def test_classify_acknowledge_long_degrades_to_business():
    reg = IntentRegistry()
    # OK + 收到，长度 > 10，且无业务关键词 → 因超长降级为 business
    long_ack = "OK" + "收到" * 6  # 2 + 12 = 14 字符
    assert len(long_ack) > 10
    assert reg.classify_disposition(long_ack).disposition == "business"


def test_classify_closing_and_polite():
    reg = IntentRegistry()
    assert reg.classify_disposition("再见拜拜").subtype == "closing"
    assert reg.classify_disposition("你好").subtype == "polite"
    # 礼貌但含业务词（怎么/处理）且变长 → business
    assert reg.classify_disposition("你好请问一下这个是怎么处理的呢").disposition == "business"


def test_classify_default_is_business():
    reg = IntentRegistry()
    # 无业务词、无社交词 → 默认 business（不跳过）
    assert reg.classify_disposition("今天心情不错").disposition == "business"


def test_rule_engine_delegates_to_registry():
    engine = RuleEngine(RulesConfig())
    intent, _desc, _conf = engine._detect_intent("谢谢老板")
    assert intent == "thank_you"
    intent, _desc, _conf = engine._detect_intent("北京明天气温多少")
    assert intent == "business"


# ---------------------------------------------------------------------------
# 3. 行动层匹配 + 工具映射
# ---------------------------------------------------------------------------

def test_match_action_categories():
    reg = IntentRegistry()
    assert reg.match_action_categories("明天北京天气怎么样") == ["action.query"]
    cats = reg.match_action_categories("帮我发个消息告诉他")
    assert "action.execute" in cats and "action.communicate" in cats
    assert reg.match_action_categories("总结一下今天的工作") == ["action.analyze"]


def test_tool_action_map_coverage():
    assert TOOL_ACTION_MAP["web_search"] == ["action.query"]
    assert set(TOOL_ACTION_MAP["send_message"]) == {"action.execute", "action.communicate"}
    assert set(TOOL_ACTION_MAP["upload_image"]) == {"action.execute", "action.media"}
    # 所有已注册工具名都应在映射中（保证文档/路由一致）
    known = {
        "send_message", "save_memory", "recall_memory", "web_search", "get_weather",
        "kb_search", "search_doc", "get_doc_content", "search_contact",
        "get_calendar_events",
        "get_attendance", "get_my_profile", "list_orgs", "get_current_org",
        "system_status", "message_stats", "keyword_rules", "config_manage",
        "get_unread", "get_conversation_info", "search_messages",
        "create_todo", "send_ding", "upload_image",
    }
    assert known <= set(TOOL_ACTION_MAP.keys())


# ---------------------------------------------------------------------------
# 4. agent 工具选择：新增抽象行动意图命中路径
# ---------------------------------------------------------------------------

class _FakeTool(BaseTool):
    def __init__(self, name: str, keywords: list[str] | None = None):
        self.name = name
        self.intent_keywords = keywords or []
        self.description = f"工具 {name}"
        self.parameters = {"type": "object", "properties": {}}

    def execute(self, args):
        return "ok"


def _agent_with(tools: dict[str, list[str]], mode: str = "smart") -> LLMAgent:
    cfg = ToolsConfig(available=list(tools.keys()), tool_routing_mode=mode)
    router = ToolRouter(cfg)
    for name, kws in tools.items():
        router.register(_FakeTool(name, kws))
    return LLMAgent(config=LlmConfig(), client=None, tool_router=router)


def test_agent_matches_tool_by_abstract_category_without_keywords():
    """工具只靠 TOOL_ACTION_MAP 声明的抽象意图命中，无需自身列具体场景词。"""
    # web_search 在 TOOL_ACTION_MAP 中声明 action.query；这里给它空 intent_keywords
    agent = _agent_with({"send_message": [], "web_search": [], "upload_image": []}, "smart")
    names = {t["function"]["name"] for t in agent._select_tools("查一下北京天气")}
    assert "web_search" in names          # 经 action.query 证据词命中
    assert "upload_image" not in names    # 无媒体意图


def test_agent_category_and_legacy_keywords_both_work():
    """既有具体场景词（legacy）仍生效，与抽象意图路径并存。"""
    agent = _agent_with({"send_message": [], "get_weather": ["气温"], "web_search": []}, "smart")
    names = {t["function"]["name"] for t in agent._select_tools("今天气温多少度")}
    assert "get_weather" in names  # 经 legacy intent_keywords 命中
    names2 = {t["function"]["name"] for t in agent._select_tools("搜索一下最新新闻")}
    assert "web_search" in names2  # 经 action.query 命中（web_search 在 map 中）


# ---------------------------------------------------------------------------
# 5. config 覆盖（向后兼容）
# ---------------------------------------------------------------------------

def test_config_override_business_keywords():
    reg = IntentRegistry()
    # 合并机制（Phase 0 修复）：自定义 business_keywords 追加到默认证据词表（非整段替换）
    reg.apply_intent_filter({"business_keywords": ["量子计算", "区块链"]})
    after = reg.get("business").evidence_keywords
    # 新词被追加（原有词仍在）
    assert "量子计算" in after and "区块链" in after
    assert "问题" in after  # 默认业务词保留
    # 新词作为业务信号生效：含『量子计算』且无线社交词 → business
    assert reg.classify_disposition("聊聊量子计算前沿").disposition == "business"


def test_config_override_thankyou_keywords():
    reg = IntentRegistry()
    reg.apply_intent_filter({"thank_you": ["多谢款待"], "pure_thank_max_length": 20})
    r = reg.classify_disposition("多谢款待")
    assert r.disposition == "social" and r.subtype == "thank_you"


# ============ 工具清单两源校验（M1 契约） ============

def test_validate_coverage_legal_subset_passes():
    """available 是 TOOL_ACTION_MAP 的真子集（运维刻意禁用部分工具）应放行。"""
    from src.intent import validate_tool_action_coverage, TOOL_ACTION_MAP
    subset = list(TOOL_ACTION_MAP.keys())[:-1]  # 少一个，模拟 create_todo 被关
    # 不应抛异常
    validate_tool_action_coverage(subset)


def test_validate_coverage_out_of_map_raises():
    """available 含无意图映射的工具应抛 ValueError（smart 路由盲区）。"""
    from src.intent import validate_tool_action_coverage, TOOL_ACTION_MAP
    bad = list(TOOL_ACTION_MAP.keys()) + ["nonexistent_tool"]
    import pytest
    with pytest.raises(ValueError):
        validate_tool_action_coverage(bad)


def test_default_whitelist_covered_by_tool_action_map():
    """回归测试：config.tools.available 默认值必须是 TOOL_ACTION_MAP 的子集。

    曾因给 ToolsConfig.available / config.yaml 加 wiki / oa_approval 工具，
    却漏加 TOOL_ACTION_MAP 映射，导致 bot 启动即 ValueError 崩溃（smart 路由盲区）。
    本测试在 CI 直接拦下此类漂移，避免再靠线上启动才暴露。
    """
    from src.intent import validate_tool_action_coverage
    from src.config import ToolsConfig
    # 不抛异常即通过（available ⊆ TOOL_ACTION_MAP）
    validate_tool_action_coverage(ToolsConfig().available)


# ============ 覆盖剩余 5 行（344 / 382 / 387 / 395 / 467） ============

def test_register_category():
    """line 344: 注册/覆盖意图类别。"""
    from src.intent import IntentCategory
    reg = IntentRegistry()
    custom = IntentCategory(
        id="test.custom", name="自定义", parent=None,
        layer="action", short_label="custom",
        evidence_keywords=["测试"], definition="自定义测试类别",
        trigger="含测试关键词"
    )
    reg.register(custom)
    assert reg.get("test.custom") is custom


def test_apply_intent_filter_invalid_thank_threshold():
    """line 382: pure_thank_max_length 为非法值时 except 路径，不抛异常。"""
    reg = IntentRegistry()
    # 先重置关键词以避免其他测试修改 DEFAULT_INTENTS 共享引用导致的副作用
    reg.get("social.gratitude").evidence_keywords = ["谢谢", "感谢"]
    reg.apply_intent_filter({"pure_thank_max_length": "not_a_number"})
    assert reg.classify_disposition("谢谢").subtype == "thank_you"


def test_apply_intent_filter_invalid_ack_threshold():
    """line 387: pure_ack_max_length 为非法值时 except 路径，不抛异常。"""
    reg = IntentRegistry()
    reg.get("social.acknowledge").evidence_keywords = ["收到", "OK"]
    reg.apply_intent_filter({"pure_ack_max_length": "not_a_number"})
    assert reg.classify_disposition("OK").subtype == "acknowledge"


def test_category_matches_nonexistent_category():
    """line 395: category_matches 查询不存在的类别返回 False。"""
    reg = IntentRegistry()
    assert reg.category_matches("nonexistent.id", "任何内容", "任何内容") is False


def test_category_matches_category_without_keywords():
    """line 395: 有类别但无 evidence_keywords 时返回 False。"""
    from src.intent import IntentCategory
    reg = IntentRegistry()
    empty = IntentCategory(
        id="test.empty", name="空证据词", parent=None,
        layer="action", short_label="empty",
        evidence_keywords=[], definition="无证据词类别",
        trigger="无法触发"
    )
    reg.register(empty)
    assert reg.category_matches("test.empty", "任何内容", "任何内容") is False


def test_classify_fallback_to_business_line_467():
    """line 467: 内容不匹配任何业务词也不匹配社交词，最终回退到 business。"""
    reg = IntentRegistry()
    r = reg.classify_disposition("今天心情不错")
    assert r.disposition == "business"
    # 验证是 467 路径：内容不是空、已启用、无 business 命中、无 social 命中
    assert "业务消息" in r.reason
