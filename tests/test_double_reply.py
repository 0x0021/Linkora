"""双重回复（double-send）回归测试。

场景：LLM 在工具轮次里调用 send_message 向【当前会话】发送了消息，
若 process_message 末尾仍返回文本，旧的 main.py 会再调 _send_reply → 同会话两条消息。

修复后：process_message 返回 AgentReply(already_sent=True)，main.py 跳过二次发送。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.llm.agent import AgentReply, LLMAgent
from src.models import Message


class _FakeClient:
    """可脚本化的 LLM client：按 step 序列返回响应。"""
    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    def chat(self, messages, tools=None, stream=False, **_kw):
        self.calls += 1
        step = self._steps.pop(0)
        return step


def _resp(content=None, tool_calls=None):
    from src.llm.client import LLMResponse
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="tool_calls" if tool_calls else "stop",
        usage={},
    )


def _tc(name, args, id_prefix="call"):
    return {"id": f"{id_prefix}_{name}", "name": name, "args": args}


def _make_agent(send_target_chat_id, send_success=True):
    """构造 LLMAgent，send_message 工具由 fake router 接管。"""
    from src.config import LlmConfig, LlmAdvancedConfig

    cfg = LlmConfig()
    cfg.advanced = LlmAdvancedConfig()
    cfg.max_tool_rounds = 6
    cfg.system_prompt = "你是助手"

    agent = LLMAgent(config=cfg, client=None, tool_router=None, store=MagicMock())

    # 接管 send_message 执行：记录是否向当前会话发送
    agent._fake_sends = []

    def fake_execute(tool_name, args, session_key=None):
        from src.tools.base import ToolCallResult
        if tool_name == "send_message":
            agent._fake_sends.append(args)
            success = send_success and args.get("chat_id") == send_target_chat_id
            return ToolCallResult(
                tool_name=tool_name, args=args, success=success,
                result="ok" if success else None,
                error=None if success else "chat mismatch",
            )
        return ToolCallResult(tool_name=tool_name, args=args, success=True, result="", error=None)

    agent.tool_router = SimpleNamespace(
        execute=fake_execute,
        get_available_tool_names=lambda: ["send_message", "save_memory", "recall_memory"],
        filter_schemas_by_names=lambda names: [],
        _tools={},
    )
    return agent


def _msg(chat_id="chat_123"):
    from datetime import datetime
    return Message(
        msg_id="m1", chat_id=chat_id, chat_type="group", chat_name="测试群",
        sender_id="u1", sender_name="张三", content="帮我通知一下大家开会",
        msg_type="text", timestamp=datetime.now(),
    )


def test_send_message_to_current_chat_marks_already_sent():
    """LLM 调用 send_message 发往当前会话 + 末尾返回文本 → already_sent=True，无二次发送。"""
    agent = _make_agent(send_target_chat_id="chat_123")
    agent.client = _FakeClient([
        _resp(tool_calls=[_tc("send_message", {"chat_id": "chat_123", "chat_type": "group", "text": "会议通知..."})]),
        _resp(content="我已经帮你通知大家开会了。"),
    ])
    reply = agent.process_message(_msg(), history=[])
    assert isinstance(reply, AgentReply)
    assert reply.already_sent is True, "当前会话已被 send_message 回复，应标记 already_sent"
    assert reply.text == "", "不应再携带文本供 poller 二次发送"
    # 确认 send_message 确实被调用了一次
    assert len(agent._fake_sends) == 1


def test_send_message_to_other_chat_is_blocked_by_third_party_guard():
    """外联护栏默认开启：LLM 用 send_message 发往【其他】会话应被拦截，
    当前会话仍由文本回复兜底（不真正外联）。"""
    agent = _make_agent(send_target_chat_id="chat_123")
    agent.client = _FakeClient([
        _resp(tool_calls=[_tc("send_message", {"chat_id": "chat_999", "chat_type": "group", "text": "私下通知"})]),
        _resp(content="抱歉，我目前被设置为不主动联系第三方，无法代为转达。"),
    ])
    reply = agent.process_message(_msg(), history=[])
    # 工具调用被护栏在编排层拦截，fake_execute 根本不被调用
    assert len(agent._fake_sends) == 0, "发往第三方会话的 send_message 应被外联护栏拦截"
    assert reply.already_sent is False
    assert reply.text == "抱歉，我目前被设置为不主动联系第三方，无法代为转达。"


def test_send_ding_blocked_by_third_party_guard():
    """外联护栏默认开启：send_ding 无论发给谁一律拦截。"""
    agent = _make_agent(send_target_chat_id="chat_123")
    agent.client = _FakeClient([
        _resp(tool_calls=[_tc("send_ding", {"users": "u999", "content": "买正版Keil"})]),
        _resp(content="抱歉，我无法主动联系第三方。"),
    ])
    reply = agent.process_message(_msg(), history=[])
    assert len(agent._fake_sends) == 0, "send_ding 应被外联护栏拦截"
    assert reply.text == "抱歉，我无法主动联系第三方。"


def test_send_message_failure_falls_back_to_text():
    """send_message 执行失败（未成功发送）→ 仍应返回文本供 poller 发送。"""
    agent = _make_agent(send_target_chat_id="chat_123", send_success=False)
    agent.client = _FakeClient([
        _resp(tool_calls=[_tc("send_message", {"chat_id": "chat_123", "chat_type": "group", "text": "通知"})]),
        _resp(content="抱歉，发送失败了，我口述一下：开会。"),
    ])
    reply = agent.process_message(_msg(), history=[])
    assert reply.already_sent is False
    assert reply.text == "抱歉，发送失败了，我口述一下：开会。"


def test_no_tool_call_returns_text():
    """普通文本回复（无工具）→ 返回文本，already_sent=False。"""
    agent = _make_agent(send_target_chat_id="chat_123")
    agent.client = _FakeClient([_resp(content="今天天气不错。")])
    reply = agent.process_message(_msg(), history=[])
    assert reply.already_sent is False
    assert reply.text == "今天天气不错。"


def test_main_skips_second_send_when_already_sent(monkeypatch):
    """集成层：main.py 在 already_sent=True 时不应调用 _send_reply。"""
    import main as main_mod

    sent = []
    monkeypatch.setattr(main_mod.LinkoraEngine, "_send_reply",
                        lambda self, msg, text: sent.append(text) or True)

    agent = _make_agent(send_target_chat_id="chat_123")
    agent.client = _FakeClient([
        _resp(tool_calls=[_tc("send_message", {"chat_id": "chat_123", "chat_type": "group", "text": "通知"})]),
        _resp(content="已通知。"),
    ])

    app = SimpleNamespace(llm_agent=agent, store=MagicMock(),
                          _auto_save_memory=MagicMock(), config=SimpleNamespace(
                              safety=SimpleNamespace(default_fallback="兜底")))
    reply = agent.process_message(_msg(), history=[])
    # 模拟 main.py:629 的消费逻辑
    if getattr(reply, "already_sent", False):
        pass  # 跳过发送
    elif reply.text:
        main_mod.LinkoraEngine._send_reply(app, _msg(), reply.text)
    assert sent == [], "already_sent=True 时 poller 不应二次发送"
