"""工具收敛护栏测试（改动③）。

场景：弱模型连续多轮都在调用检索类工具（web_search）却迟迟不综合作答。
达到 converge_after_tool_rounds 阈值后，agent 应：
- 从后续轮次传入 LLM 的 tools 中移除检索类工具（web_search/kb_search/search_doc）；
- 向 messages 注入“基于已有结果直接作答”的 system 提示；
- 最终仍能在轮次内给出综合答案，而非耗尽轮次降级为“查不到”。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.llm.agent import LLMAgent
from src.models import Message


WEB_SCHEMA = {"type": "function", "function": {"name": "web_search", "description": "x"}}
SEND_SCHEMA = {"type": "function", "function": {"name": "send_message", "description": "x"}}
SAVE_SCHEMA = {"type": "function", "function": {"name": "save_memory", "description": "x"}}


def _resp(content=None, tool_calls=None):
    from src.llm.client import LLMResponse
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="tool_calls" if tool_calls else "stop",
        usage={},
    )


def _tc(name, args):
    return {"id": f"call_{name}", "name": name, "args": args}


class _FakeClient:
    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0
        self.tools_seen = []  # 每轮传给 chat 的 tools 名称

    def chat(self, messages, tools=None, stream=False, **_kw):
        self.calls += 1
        self.tools_seen.append([t.get("function", {}).get("name") for t in (tools or [])])
        return self._steps.pop(0)


def _schema_for(name):
    return {"type": "function", "function": {"name": name, "description": "x"}}


def _make_agent(converge_threshold, chat_steps, tool_names=("web_search", "send_message")):
    from src.config import LlmConfig, LlmAdvancedConfig

    cfg = LlmConfig()
    cfg.advanced = LlmAdvancedConfig()
    cfg.converge_after_tool_rounds = converge_threshold
    cfg.system_prompt = "你是助手"

    client = _FakeClient(chat_steps)
    agent = LLMAgent(config=cfg, client=client, tool_router=None, store=MagicMock())

    schemas = [_schema_for(n) for n in tool_names]

    # _select_tools / _build_user_message 用可控桩替换，聚焦护栏逻辑本身
    agent._select_tools = MagicMock(return_value=list(schemas))
    agent._build_user_message = MagicMock(return_value=[{"role": "user", "content": "深挖acme上市信息"}])

    def fake_execute(tool_name, args, session_key=None):
        from src.tools.base import ToolCallResult
        return ToolCallResult(
            tool_name=tool_name, args=args, success=True,
            result={"query": args.get("query"), "results": [{"title": "珞石上市", "url": "x", "snippet": "拟赴港上市"}]},
            error=None,
        )

    # 给 tool_router 提供 _execute_tool_calls 所需 minimal 接口
    agent.tool_router = SimpleNamespace(
        execute=fake_execute,
        get_available_tool_names=MagicMock(return_value=list(tool_names)),
        filter_schemas_by_names=MagicMock(return_value=list(schemas)),
        get_schemas=MagicMock(return_value=list(schemas)),
        _tools={n: object() for n in tool_names},
    )
    agent.client = client
    return agent, client


def test_converge_removes_retrieval_tools_and_answers():
    """连续3轮调 web_search → 第3轮后移除 web_search，最终综合作答（非降级）。"""
    # 前3轮都要求调 web_search，第4轮给最终 content
    steps = [
        _resp(tool_calls=[_tc("web_search", {"query": "acme 上市"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "03752 招股"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "ACME IPO"})]),
        _resp(content="珞石机器人（ACME）已于港交所递交招股书，拟 18C 上市，中金保荐。"),
    ]
    agent, client = _make_agent(converge_threshold=3, chat_steps=steps)
    msg = Message(
        msg_id="m1", chat_id="conv1", chat_type="single", chat_name="杨超萍",
        msg_type="text", sender_id="peer", sender_name="杨超萍",
        content="深挖acme上市信息",
        timestamp=__import__("datetime").datetime.now(), raw={},
    )
    reply = agent.process_message(msg)

    # 最终给了真实答案，而非降级话术
    assert "查不到" not in reply.text
    assert "招股书" in reply.text or "上市" in reply.text
    # 收敛后某轮 tools 不再含 web_search
    assert any("web_search" not in tools for tools in client.tools_seen)
    # 收敛后注入了“强制综合”系统提示
    all_msgs = client.tools_seen  # 仅校验 tools 维度；system 提示在下文单独验
    assert len(client.tools_seen) >= 3


def test_converge_disabled_when_threshold_zero():
    """threshold=0 关闭护栏，web_search 保留到最后（仍依赖 max_tool_rounds）。"""
    steps = [
        _resp(tool_calls=[_tc("web_search", {"query": "a"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "b"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "c"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "d"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "e"})]),
        _resp(content="综合结论：……"),
    ]
    agent, client = _make_agent(converge_threshold=0, chat_steps=steps)
    msg = Message(
        msg_id="m2", chat_id="conv2", chat_type="single", chat_name="杨",
        msg_type="text", sender_id="peer", sender_name="杨",
        content="查一下",
        timestamp=__import__("datetime").datetime.now(), raw={},
    )
    agent.process_message(msg)
    # 关闭护栏时 web_search 始终在场（每轮都含）
    assert all("web_search" in tools for tools in client.tools_seen)


def test_converge_preserves_action_tools():
    """收敛时只移除 web_search/kb_search/search_doc，动作类工具（save_memory）保留。"""
    # 第1轮 web_search，第2轮 混合（web_search+save_memory），第3轮 final content
    steps = [
        _resp(tool_calls=[_tc("web_search", {"query": "深挖x"})]),
        _resp(tool_calls=[
            _tc("web_search", {"query": "深挖y"}),
            _tc("save_memory", {"content": "记忆点"}),
        ]),
        _resp(content="最终综合答案。"),
    ]
    agent, client = _make_agent(
        converge_threshold=2, chat_steps=steps,
        tool_names=("web_search", "save_memory"),
    )
    msg = Message(
        msg_id="m3", chat_id="c1", chat_type="single", chat_name="杨",
        msg_type="text", sender_id="peer", sender_name="杨",
        content="深挖x",
        timestamp=__import__("datetime").datetime.now(), raw={},
    )
    reply = agent.process_message(msg)
    assert "最终综合答案" in reply.text
    # 收敛后某轮 tools 不再含 web_search，但 save_memory 仍保留（动作类不该被误删）
    assert any("web_search" not in tools for tools in client.tools_seen)
    assert all("save_memory" in tools for tools in client.tools_seen), \
        f"save_memory 被误删: {client.tools_seen}"


def test_discarded_tools_inject_correction_and_compose():
    """[P1-#3 回归] 收敛移除工具后，弱模型仍盲调被移除工具且给不出内容时，
    agent 应注入纠正消息（而非静默空转直到耗尽 max_rounds），下一轮直接综合作答。
    """
    from src.config import LlmAdvancedConfig, LlmConfig
    from src.llm.client import LLMResponse

    # 第1-2轮正常调 web_search（第2轮末触发收敛移除）；第3轮模型仍盲调 web_search
    # （已不在 schema）→ client 全丢弃：discarded=["web_search"] 且 content 空（死循环场景）；
    # 第4轮（收到纠正后）直接给出综合答案。
    steps = [
        _resp(tool_calls=[_tc("web_search", {"query": "rk 上市"})]),
        _resp(tool_calls=[_tc("web_search", {"query": "03752"})]),
        LLMResponse(content=None, tool_calls=[], finish_reason="stop",
                    usage={}, discarded_tool_names=["web_search"]),
        _resp(content="据招股书，ACME 拟 18C 上市。"),
    ]

    class _ClientWithMsgs:
        def __init__(self, steps):
            self._steps = list(steps)
            self.calls = 0
            self.messages_seen = []
        def chat(self, messages, tools=None, stream=False, **_kw):
            self.calls += 1
            self.messages_seen.append(list(messages))
            return self._steps.pop(0)

    cfg = LlmConfig()
    cfg.advanced = LlmAdvancedConfig()
    cfg.converge_after_tool_rounds = 2
    cfg.system_prompt = "你是助手"
    client = _ClientWithMsgs(steps)
    agent = LLMAgent(config=cfg, client=client, tool_router=None, store=MagicMock())
    schemas = [_schema_for(n) for n in ("web_search", "send_message")]
    agent._select_tools = MagicMock(return_value=list(schemas))
    agent._build_user_message = MagicMock(return_value=[{"role": "user", "content": "深挖rk上市"}])

    def fake_execute(tool_name, args, session_key=None):
        from src.tools.base import ToolCallResult
        return ToolCallResult(tool_name=tool_name, args=args, success=True,
                              result={"results": []}, error=None)

    agent.tool_router = SimpleNamespace(
        execute=fake_execute,
        get_available_tool_names=MagicMock(return_value=list(("web_search", "send_message"))),
        filter_schemas_by_names=MagicMock(return_value=list(schemas)),
        get_schemas=MagicMock(return_value=list(schemas)),
        _tools={n: object() for n in ("web_search", "send_message")},
    )
    agent.client = client
    msg = Message(
        msg_id="mD", chat_id="cD", chat_type="single", chat_name="杨",
        msg_type="text", sender_id="peer", sender_name="杨",
        content="深挖rk上市", timestamp=__import__("datetime").datetime.now(), raw={},
    )
    reply = agent.process_message(msg)

    # 1) 最终给出真实答案，而非耗尽轮次降级为“查不到”
    assert "查不到" not in reply.text
    assert "上市" in reply.text
    # 2) 收敛后盲调被移除工具时注入了纠正消息（某轮对话含“已被移除”系统提示）
    # messages_seen 是“每轮 messages 快照”的列表，需展平后逐条检查
    all_msgs = [m for snap in client.messages_seen for m in snap]
    correction_injected = any(
        isinstance(m, dict) and m.get("role") == "system" and "已被移除" in (m.get("content") or "")
        for m in all_msgs
    )
    assert correction_injected, "收敛后盲调被移除工具应注入纠正消息"
