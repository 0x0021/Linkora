"""Phase 2：RAG 引文溯源 + 置信度产品化 测试。

覆盖：
- Citation dataclass 基本字段
- style.retrieve_relevant_knowledge 侧信道暂存结构化引文 + 保持 2 元组返回契约
- rag_inject 仅在 kb_grounded 时透传 citations
- LinkoraEngine._append_citation_footer 的开关/群聊/阈值分级/多条/异常回退分支
- AgentReply.citations / best_chunk 默认值 + _mk_reply 透传（经 thread-local）
"""

from __future__ import annotations

from types import SimpleNamespace

import main as main_mod
from src.llm.style import Citation
from src.llm.rag_inject import inject_rag_knowledge


# ----------------------------- Citation 基本 -----------------------------

def test_citation_fields():
    c = Citation(source="VPN手册", score=0.88, snippet="配置步骤...", doc_id="7")
    assert c.source == "VPN手册"
    assert c.score == 0.88
    assert c.snippet == "配置步骤..."
    assert c.doc_id == "7"


def test_citation_doc_id_optional():
    c = Citation(source="A", score=0.5, snippet="x")
    assert c.doc_id is None


# --------------------- style 侧信道暂存引文 + 2 元组契约 ---------------------

class _FakeKbTool:
    def __init__(self, results):
        self._results = results

    def search(self, **kwargs):
        return {"success": True, "results": self._results}


class _FakeAgentForStyle:
    """最小 agent：驱动 style.retrieve_relevant_knowledge。"""

    _rag_min_similarity = 0.5
    _rag_max_results = 2
    _rag_max_content_chars = 800

    def __init__(self, results):
        self.tool_router = SimpleNamespace(_tools={"kb_search": _FakeKbTool(results)})


def test_retrieve_stashes_citations_and_keeps_2tuple():
    from src.llm import style
    results = [
        {"content": "VPN 配置步骤：第一步...", "source": "VPN手册", "score": 0.9, "id": 7},
        {"content": "补充说明文档内容", "source": "补充文档", "score": 0.6, "id": 8},
    ]
    agent = _FakeAgentForStyle(results)
    ret = style.retrieve_relevant_knowledge(agent, "VPN 配置")
    # 契约：仍返回 2 元组 (text, best_score)
    assert isinstance(ret, tuple) and len(ret) == 2
    text, best = ret
    assert "【相关知识】" in text
    assert best == 0.9
    # 侧信道：结构化引文被暂存到 agent 上（仅保留通过展示阈值的最高分条目）。
    cites = agent._last_kb_citations_raw
    assert len(cites) >= 1  # _MAX_DISPLAY 不再硬编码 1，改为读配置上限 4
    assert cites[0].source == "VPN手册"
    assert cites[0].score == 0.9
    assert cites[0].doc_id == "7"
    assert cites[0].snippet  # 非空片段


# --------------------- rag_inject 仅 grounded 时透传引文 ---------------------

class _FakeEmbedding:
    def embed(self, text):
        return [0.1, 0.2]


class _FakeAgentForInject:
    def __init__(self, *, intent_ok, kb_text, best, citations):
        self._intent_ok = intent_ok
        self._kb = (kb_text, best)
        self._last_kb_citations_raw = citations
        self._emb_client = _FakeEmbedding()

    def _get_embedding_client(self):
        return self._emb_client

    def _is_document_query(self, query, query_embedding=None):
        return self._intent_ok

    def _retrieve_relevant_knowledge(self, query, query_embedding=None):
        return self._kb


def test_rag_inject_carries_citations_when_grounded():
    cites = [Citation(source="VPN手册", score=0.9, snippet="步骤")]
    agent = _FakeAgentForInject(
        intent_ok=True, kb_text="【相关知识】\n1. VPN手册（90%）", best=0.9, citations=cites,
    )
    _, result = inject_rag_knowledge(
        query="VPN 怎么配置", system_content="", agent=agent,
        rag_auto_inject=True, rag_intent_only=True,
    )
    assert result.injected is True
    assert result.citations == cites


def test_rag_inject_drops_citations_when_intent_miss():
    """意图未命中且分数未达置信阈值 → 不注入 → 引文清空。"""
    cites = [Citation(source="VPN手册", score=0.49, snippet="步骤")]
    agent = _FakeAgentForInject(
        intent_ok=False, kb_text="【相关知识】\n1. VPN手册（49%）", best=0.49, citations=cites,
    )
    _, result = inject_rag_knowledge(
        query="今天天气怎么样", system_content="", agent=agent,
        rag_auto_inject=True, rag_intent_only=True,
    )
    assert result.injected is False
    assert result.citations == []


def test_rag_inject_disabled_has_empty_citations():
    cites = [Citation(source="X", score=0.9, snippet="y")]
    agent = _FakeAgentForInject(
        intent_ok=True, kb_text="【相关知识】x", best=0.9, citations=cites,
    )
    _, result = inject_rag_knowledge(
        query="VPN 怎么配置", system_content="", agent=agent,
        rag_auto_inject=False, rag_intent_only=True,
    )
    assert result.injected is False
    assert result.citations == []


# --------------------- _append_citation_footer 分支 ---------------------

def _adv(**over):
    base = dict(
        citation_enabled=True, citation_in_group=False,
        citation_high_threshold=0.75, citation_low_threshold=0.5, citation_max_items=2,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _app(adv):
    return SimpleNamespace(config=SimpleNamespace(llm=SimpleNamespace(advanced=adv)))


def _reply(citations):
    return SimpleNamespace(text="回复正文", citations=citations)


def _msg(chat_type="single"):
    return SimpleNamespace(chat_type=chat_type)


def _footer(app, text, reply, message):
    return main_mod.LinkoraEngine._append_citation_footer(app, text, reply, message)


def test_footer_disabled_returns_original():
    app = _app(_adv(citation_enabled=False))
    reply = _reply([Citation("VPN手册", 0.9, "步骤")])
    out = _footer(app, "正文", reply, _msg())
    assert out == "正文"


def test_footer_high_confidence_uses_yiju():
    app = _app(_adv())
    reply = _reply([Citation("VPN手册", 0.88, "步骤")])
    out = _footer(app, "关于VPN手册的配置方法如下", reply, _msg())
    # 默认隐藏内部分数（相关度XX%）——避免把相似度指标泄露给最终用户
    assert "—— 依据：《VPN手册》" in out
    assert "相关度" not in out
    assert "供参考" not in out


def test_footer_shows_score_when_enabled():
    """citation_show_score=True 时页脚保留（相关度XX%）。"""
    app = _app(_adv(citation_show_score=True))
    reply = _reply([Citation("VPN手册", 0.88, "步骤")])
    out = _footer(app, "关于VPN手册的配置方法如下", reply, _msg())
    assert "—— 依据：《VPN手册》（相关度88%）" in out


def test_footer_mid_confidence_uses_cankao():
    app = _app(_adv())
    reply = _reply([Citation("VPN手册", 0.62, "步骤")])
    out = _footer(app, "参考VPN手册中的步骤说明", reply, _msg())
    # 默认隐藏内部分数
    assert "—— 参考来源：《VPN手册》" in out
    assert "相关度" not in out
    assert "供参考" not in out


def test_footer_mid_confidence_shows_score_when_enabled():
    app = _app(_adv(citation_show_score=True))
    reply = _reply([Citation("VPN手册", 0.62, "步骤")])
    out = _footer(app, "参考VPN手册中的步骤说明", reply, _msg())
    assert "—— 参考来源：《VPN手册》（相关度62%）" in out


def test_footer_low_confidence_no_footer():
    app = _app(_adv())
    reply = _reply([Citation("VPN手册", 0.40, "步骤")])
    out = _footer(app, "正文", reply, _msg())
    assert out == "正文"


def test_footer_group_gated_off_by_default():
    app = _app(_adv(citation_in_group=False))
    reply = _reply([Citation("VPN手册", 0.9, "步骤")])
    out = _footer(app, "正文", reply, _msg(chat_type="group"))
    assert out == "正文"


def test_footer_group_enabled_when_configured():
    app = _app(_adv(citation_in_group=True))
    reply = _reply([Citation("VPN手册", 0.9, "步骤")])
    out = _footer(app, "VPN手册相关配置", reply, _msg(chat_type="group"))
    assert "依据" in out


def test_footer_multiple_items_capped():
    app = _app(_adv(citation_max_items=2))
    reply = _reply([
        Citation("API文档", 0.9, "接口说明"),
        Citation("部署手册", 0.8, "部署步骤"),
        Citation("运维指南", 0.7, "运维流程"),
    ])
    out = _footer(app, "根据API文档和部署手册进行配置", reply, _msg())
    assert "《API文档》" in out and "《部署手册》" in out
    assert "《运维指南》" not in out  # 超出 max_items 被截断


def test_footer_no_citations_returns_original():
    app = _app(_adv())
    out = _footer(app, "正文", _reply([]), _msg())
    assert out == "正文"


def test_footer_exception_falls_back():
    """异常（如 config 缺字段）应回退无页脚，绝不抛出。"""
    broken = SimpleNamespace(config=SimpleNamespace(llm=SimpleNamespace(advanced=None)))
    out = _footer(broken, "正文", _reply([Citation("A", 0.9, "a")]), _msg())
    assert out == "正文"


def test_footer_dedup_self_generated_citation():
    """LLM 已在正文末尾自生成引文时，追加官方页脚前需去重，避免双引文。"""
    app = _app(_adv())
    reply = _reply([Citation("Windows共享访问教程", 0.62, "检查网络")])
    text = "如果连不上，检查网络是否通内网 —— 参考来源：《Windows共享访问教程》"
    out = _footer(app, text, reply, _msg())
    assert out.count("参考来源") == 1
    assert out.count("《Windows共享访问教程》") == 1
    assert "—— 参考来源：《Windows共享访问教程》" in out


def test_footer_dedup_repeated_self_generated_citation():
    """LLM 重复自生成多段引文时，应全部剥离后再追加官方页脚。"""
    app = _app(_adv())
    reply = _reply([Citation("Windows共享访问教程", 0.62, "检查网络")])
    text = "正文—— 参考来源：《Windows共享访问教程》—— 参考来源：《Windows共享访问教程》"
    out = _footer(app, text, reply, _msg())
    assert out.count("参考来源") == 1
    assert out.count("《Windows共享访问教程》") == 1
    assert out.startswith("正文")


# --------------------- 语义相关性过滤（修复引用不相关 + 滥用追加） ---------------------

def test_footer_unrelated_citation_rejected():
    """回复讲股价，引文是 CRM 文档 → 语义无关 → 不追加页脚（问题1修复）。"""
    app = _app(_adv())
    reply_text = (
        "珞石机器人（03752.HK）今天的股价信息：\n"
        "当前价：51.8 港元 | 涨跌幅：+11.16%\n"
        "成交量：36.22 万手"
    )
    # source「珞石CRM对接问题」与股价回复无任何关键词重叠
    reply = _reply([Citation("珞石CRM对接问题", 3.0, "CRM系统与珞石机器人对接的技术方案")])
    out = _footer(app, reply_text, reply, _msg())
    assert out == reply_text  # 无页脚
    assert "依据" not in out and "参考" not in out


def test_footer_related_citation_kept():
    """回复明确提到了引文 source 名称 → 保留页脚（默认隐藏内部分数）。"""
    app = _app(_adv())
    reply_text = "根据《VPN手册》的配置步骤，你需要先设置服务器地址。"
    reply = _reply([Citation("VPN手册", 0.88, "配置步骤：第一步打开设置...")])
    out = _footer(app, reply_text, reply, _msg())
    assert "—— 依据：《VPN手册》" in out
    assert "相关度" not in out


def test_footer_related_citation_kept_shows_score():
    """citation_show_score=True 时保留（相关度XX%）。"""
    app = _app(_adv(citation_show_score=True))
    reply_text = "根据《VPN手册》的配置步骤，你需要先设置服务器地址。"
    reply = _reply([Citation("VPN手册", 0.88, "配置步骤：第一步打开设置...")])
    out = _footer(app, reply_text, reply, _msg())
    assert "—— 依据：《VPN手册》（相关度88%）" in out


def test_footer_snippet_overlap_kept():
    """回复虽未出现 source 全名，但 snippet 有 ≥2 个术语重叠 → 保留。"""
    app = _app(_adv())
    reply_text = "珞石机器人今天的股价是51.8港元，涨幅超过11%。"
    # source「珞石股价日报」不含在回复中，但 snippet 含 珞石/股价/港元 三个术语
    reply = _reply([Citation("珞石股价日报", 0.82,
                              "珞石机器人今日收盘价51.8港元，涨幅11.16%")])
    out = _footer(app, reply_text, reply, _msg())
    assert "依据" in out or "参考" in out


def test_footer_no_reference_in_reply_no_footer():
    """回复完全没提任何引文内容 → 全部剔除 → 不追加页脚（问题2修复）。"""
    app = _app(_adv())
    reply_text = "好的，我已收到你的消息，稍后处理。"
    reply = _reply([
        Citation("内部技术文档", 0.9, "API接口设计规范"),
        Citation("运维手册", 0.8, "服务器部署流程"),
    ])
    out = _footer(app, reply_text, reply, _msg())
    assert out == reply_text  # 无页脚


def test_footer_abnormal_score_filtered():
    """score > 1.0（如截图中的 300%=3.0）视为异常值，直接排除。"""
    app = _app(_adv(citation_low_threshold=0.1))  # 低阈值让异常分也能过阈值
    reply_text = "随便回复一下"
    reply = _reply([Citation("异常文档", 3.0, "异常高分内容")])
    out = _footer(app, reply_text, reply, _msg())
    assert out == reply_text  # score=3.0 > 1.0 被排除 → 无候选 → 无页脚


def test_citation_relevant_to_reply_edge_cases():
    """_citation_relevant_to_reply 边界情况。"""
    from src.platform.runtime import _citation_relevant_to_reply
    # 空 reply
    assert _citation_relevant_to_reply(Citation("A", 0.9, "x"), "") is False
    # 空 source
    assert _citation_relevant_to_reply(Citation("", 0.9, "x"), "some text") is False
    # None 属性
    assert _citation_relevant_to_reply(SimpleNamespace(source=None, snippet=None), "text") is False
    # 单字中文术语不匹配（需≥2字）
    c = Citation("A", 0.9, "的是")
    assert _citation_relevant_to_reply(c, "这是测试") is False  # 「的是」各单字不构成有义术语
    # 英文术语需≥3字母
    c2 = Citation("AB", 0.9, "xy")
    assert _citation_relevant_to_reply(c2, "ab xy test") is False  # AB/xy 太短
    c3 = Citation("API", 0.9, "restful call")
    assert _citation_relevant_to_reply(c3, "The API is good") is True  # API ≥3字母且命中


# --------------------- AgentReply 默认值 ---------------------

def test_agent_reply_citation_defaults():
    from src.llm.agent import AgentReply
    r = AgentReply(text="hi")
    assert r.citations == []
    assert r.best_chunk is None
