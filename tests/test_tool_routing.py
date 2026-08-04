"""工具按需路由（tool_routing_mode）测试。

验证『智能混合』策略：明确意图用 intent_keywords 精准暴露相关工具（零额外 LLM 调用），
关键词无法确定时回退『基础+意图工具』并排除检索类（避免弱模型陷入换关键词循环）。
同时覆盖 all / keyword / 旧开关兼容。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.config import LlmConfig, SkillsConfig, ToolsConfig
from src.llm.agent import LLMAgent
from src.skills.loader import Skill
from src.skills.router import SkillMatch
from src.tools.base import BaseTool, ToolRouter
from src.tools.web_search import WebSearchTool


class _FakeTool(BaseTool):
    def __init__(self, name: str, keywords: list[str]):
        self.name = name
        self.intent_keywords = keywords
        self.description = f"工具 {name} 的描述"
        self.parameters = {"type": "object", "properties": {}}

    def execute(self, args):
        return "ok"


_AVAILABLE = ["send_message", "get_weather", "web_search", "kb_search"]


def _make_agent(mode: str | None = None, **cfg_kwargs) -> LLMAgent:
    if mode is not None:
        cfg_kwargs["tool_routing_mode"] = mode
    cfg = ToolsConfig(available=list(_AVAILABLE), **cfg_kwargs)
    router = ToolRouter(cfg)
    router.register(_FakeTool("send_message", []))
    router.register(_FakeTool("get_weather", ["天气", "带伞"]))
    router.register(_FakeTool("web_search", ["搜索"]))
    router.register(_FakeTool("kb_search", ["文档", "知识库"]))
    return LLMAgent(config=LlmConfig(), client=None, tool_router=router)


def _names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def test_smart_explicit_intent_returns_subset():
    """明确意图（含『天气/带伞』）→ 只暴露 get_weather + 基础工具，不含无关工具。"""
    agent = _make_agent("smart")
    names = _names(agent._select_tools("北京天气怎么样，要不要带伞"))
    assert names == {"send_message", "get_weather"}
    assert "web_search" not in names
    assert "kb_search" not in names


def test_smart_ambiguous_excludes_retrieval_tools():
    """模糊/闲聊（无命中关键词）→ 基础+意图工具兜底，排除库内检索类（kb_search/search_doc，
    已由 RAG 自动注入覆盖，暴露反诱发弱模型『换关键词反复搜』循环）；
    web_search 保留在兜底集——它是实时/事实类问题的唯一外部事实源，
    排除它会迫使模型凭训练记忆编造（防幻觉修复，2026-07）。"""
    agent = _make_agent("smart")
    agent._last_kb_hit = True  # 模拟 RAG 正常命中状态
    names = _names(agent._select_tools("哈哈哈哈今天心情不错"))
    # _BASE_TOOL_NAMES={send_message,save_memory,recall_memory} + 全部 intent 工具
    # - 库内检索类 {kb_search,search_doc} = {send_message, get_weather, web_search}
    # （save_memory/recall_memory 未注册，故 filter_schemas_by_names 过滤后只剩已注册部分）
    assert names == {"send_message", "get_weather", "web_search"}
    assert "web_search" in names
    assert "kb_search" not in names


def test_smart_kb_search_retained_when_no_rag_hit():
    """RAG 未命中时（_last_kb_hit=False），保留 kb_search 作为兜底检索通道。

    修复 2026-07-31：若一律排除 kb_search，LLM 在 RAG 空结果时被提示"无知识则调 kb_search"
    但工具被屏蔽，形成死锁 → 退化到先前上下文答非所问。
    """
    agent = _make_agent("smart")
    agent._last_kb_hit = False  # 模拟 RAG 未命中
    names = _names(agent._select_tools("哈哈哈哈今天心情不错"))
    assert names == {"send_message", "get_weather", "web_search", "kb_search"}
    assert "kb_search" in names


def test_all_mode_always_full():
    """all 模式：无论什么消息都全量暴露。"""
    agent = _make_agent("all")
    names = _names(agent._select_tools("随便一句话"))
    assert names == set(_AVAILABLE)


def test_keyword_hit_returns_matched():
    """keyword 模式：命中关键词则暴露对应工具（不触发 FALLBACK）。"""
    agent = _make_agent("keyword")
    names = _names(agent._select_tools("帮我搜索一下最新新闻"))
    assert "web_search" in names
    assert "kb_search" not in names


def test_keyword_no_hit_returns_fallback():
    """keyword 模式：无命中 → 回退 FALLBACK（已注册部分）。"""
    agent = _make_agent("keyword")
    names = _names(agent._select_tools("随便聊聊"))
    # FALLBACK = send_message/save_memory/recall_memory/web_search/get_weather
    # 其中 save_memory/recall_memory 未注册，故只剩已注册的
    assert names == {"send_message", "web_search", "get_weather"}


def test_base_tools_always_present():
    """基础工具 send_message 始终暴露。"""
    for mode in ("smart", "all", "keyword"):
        agent = _make_agent(mode)
        assert "send_message" in _names(agent._select_tools("测试消息"))


def test_legacy_expose_all_true_maps_to_all():
    """向后兼容：仅设旧 expose_all_tools=True（未设 tool_routing_mode）→ 等价 all。"""
    agent = _make_agent(expose_all_tools=True)  # 不设 tool_routing_mode
    names = _names(agent._select_tools("模糊消息无关键词"))
    assert names == set(_AVAILABLE)


def test_legacy_expose_all_false_maps_to_keyword():
    """向后兼容：仅设旧 expose_all_tools=False → 等价 keyword（模糊时回退 FALLBACK）。"""
    agent = _make_agent(expose_all_tools=False)
    names = _names(agent._select_tools("模糊消息无关键词"))
    assert names == {"send_message", "web_search", "get_weather"}


def test_smart_hits_real_web_search_for_stock_query():
    """真实 WebSearchTool：smart 模式对『股票/上市』类查询精准命中 web_search，不全量。"""
    cfg = ToolsConfig(
        available=["send_message", "web_search", "get_weather"],
        tool_routing_mode="smart",
    )
    router = ToolRouter(cfg)
    router.register(_FakeTool("send_message", []))
    router.register(WebSearchTool())
    router.register(_FakeTool("get_weather", ["天气"]))
    agent = LLMAgent(config=LlmConfig(), client=None, tool_router=router)
    names = _names(agent._select_tools("ACME 机器人股票上市情况怎么样"))
    assert names == {"send_message", "web_search"}


def test_smart_rag_grounded_suppresses_web_search_tool():
    """RAG 高置信命中（best_score>=0.65）时，smart 模式应抑制 web_search 工具，
    优先用知识库回答（修复：明确意图且 RAG 有知识库时仍去联网搜索、白耗 60s 超时）。"""
    cfg = ToolsConfig(
        available=["send_message", "web_search", "get_weather"],
        tool_routing_mode="smart",
    )
    router = ToolRouter(cfg)
    router.register(_FakeTool("send_message", []))
    router.register(WebSearchTool())  # 真实工具，intent_categories=[domain.web_search]
    router.register(_FakeTool("get_weather", ["天气"]))
    agent = LLMAgent(config=LlmConfig(), client=None, tool_router=router)
    # 模拟 RAG 已注入 0.672 命中（与线上 VPN 场景一致）
    agent._last_kb_hit = True
    agent._last_kb_best_score = 0.672
    agent._last_kb_query_intent = True
    names = _names(agent._select_tools("公司的 VPN 怎么配置"))
    assert "web_search" not in names
    assert "send_message" in names  # 基础工具仍保留


def test_smart_weak_rag_hit_keeps_web_search():
    """RAG 仅弱命中（best_score<0.65）时，保留 web_search 作为外部事实源兜底，不误伤。"""
    cfg = ToolsConfig(
        available=["send_message", "web_search", "get_weather"],
        tool_routing_mode="smart",
    )
    router = ToolRouter(cfg)
    router.register(_FakeTool("send_message", []))
    router.register(WebSearchTool())
    router.register(_FakeTool("get_weather", ["天气"]))
    agent = LLMAgent(config=LlmConfig(), client=None, tool_router=router)
    agent._last_kb_hit = True
    agent._last_kb_best_score = 0.40  # 弱命中，低于接地阈值
    agent._last_kb_query_intent = True
    names = _names(agent._select_tools("随便一个问题"))
    assert "web_search" in names


def test_activate_skills_suppresses_web_skill_when_rag_grounded():
    """RAG 高置信命中时，_activate_skills 应抑制 web 搜索类技能（domain.web_search），
    并同步 skill_router 线程局部状态，使下游工具路由不再视为已激活。"""
    agent = _make_agent("smart")
    agent._last_kb_hit = True
    agent._last_kb_best_score = 0.672
    agent._last_kb_query_intent = True
    agent.skills_config = SkillsConfig(enabled=True)
    # 伪造一个 web 搜索技能 + SkillRouter
    web_skill = Skill(name="web-composite-search", description="web", body="",
                      intent_categories=["domain.web_search"])
    fake_manager = MagicMock()
    fake_manager.get.return_value = web_skill
    fake_router = MagicMock()
    fake_router._manager = fake_manager
    fake_router._tl = MagicMock()
    agent.skill_router = fake_router
    match = SkillMatch(name="web-composite-search", score=0.6, prompt="去联网搜索",
                      source="intent", weight=0.6, order=0)
    fake_router.route_combo.return_value = [match]

    activated = agent._activate_skills(
        MagicMock(content="公司的 VPN 怎么配置"), [], None)

    assert activated == []  # web 技能被抑制
    # 下游状态同步：last_matches 清空，避免 select_tools 仍按已激活处理
    assert fake_router._tl.last_matches == []
    assert fake_router._tl.last_match is None
