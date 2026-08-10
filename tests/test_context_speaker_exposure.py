"""上下文发言人暴露 + 对话收尾指令 测试。

2026-08 线上事故根因：历史消息被吞掉发言人姓名、不同人合并成一条无署名文本，
LLM 看不到「谁说了什么」，也就无法判断「老数据先不用了」是话题闭环，
于是对方说完后仍追问工号手机号。

修复思路（按用户要求，不堆正则、交给 LLM 自行判断）：
1. 历史与当前消息都暴露发言人姓名（prompt_builder + message_wrap）。
2. 不同发言人的消息绝不跨人合并。
3. system prompt 用自然语言告知「对方说完了就收尾，不要追问」。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.prompt_builder import PromptBuilder
from src.llm.system_prompt import build_system_prompt_core


class _Msg:
    def __init__(self, sender, role, content):
        self.sender_name = sender
        self.role = role
        self.content = content


class _Agent:
    user_name = "主人"
    platform_id = "dingtalk"
    user_dept = "研发部"
    user_title = "工程师"
    org_name = "公司"
    _max_input_tokens = 99999          # 不触发截断，保证历史完整进入
    _rag_auto_inject = False
    _rag_intent_only = False
    config = SimpleNamespace(
        system_prompt="你是{user_name}的{platform}数字分身。",
        advanced=SimpleNamespace(
            max_chars_daily_chat=200,
            max_chars_tech_issue=100,
        ),
    )

    def _build_system_prompt(self, **kw):
        return "system prompt body"

    def _apply_history_tiering(self, history):
        return history

    def _truncate_long_message(self, c):
        return c


def _patch(monkeypatch):
    import src.llm.prompt_builder as pbmod

    monkeypatch.setattr(
        pbmod, "inject_rag_knowledge",
        lambda **kwargs: (kwargs.get("system_content"), SimpleNamespace(
            best_score=None, injected=False, intent_ok=False,
            citations=[], rag_block=None, relevant_knowledge="")),
    )
    # 隔离外部消息包装，仅验证历史归一化本身
    monkeypatch.setattr(
        pbmod, "wrap_incoming_message",
        lambda message, truncate_fn=None: message.content,
    )


def _history_user_contents(messages):
    """提取历史段（排除末尾当前消息）里 role==user 的 content。"""
    # messages = [system, *history, guard_system, (rag?), incoming_user]
    # 末尾一定是当前消息（无发言人前缀），历史在它之前
    incoming = messages[-1]
    assert incoming["role"] == "user"
    return [m["content"] for m in messages[1:-1] if m["role"] == "user"]


def test_history_keeps_speakers_separate(monkeypatch):
    """小雅、主人两人发言必须分两条，且各带姓名前缀，绝不合并。"""
    _patch(monkeypatch)
    agent = _Agent()
    pb = PromptBuilder(agent)

    history = [
        _Msg("小雅", "user", "改完了。老数据不用挪吧"),
        _Msg("主人", "user", "老数据先不用了"),
    ]
    incoming = _Msg("主人", "user", "老数据先不用了")

    messages = pb.build_user_message(incoming, history)
    contents = _history_user_contents(messages)

    # 两条历史消息都在，且分别带发言人前缀
    assert any(c.startswith("小雅：") for c in contents), contents
    assert any(c.startswith("主人：") for c in contents), contents

    # 关键：禁止跨人合并 —— 不存在一条同时含两人姓名前缀的消息
    assert not any("小雅：" in c and "主人：" in c for c in contents), contents


def test_owner_messages_labeled_as_user_not_merged_with_others(monkeypatch):
    """多人会话中，owner 自己的发言（role=user, sender=owner）也带姓名，
    且与其他 user 不因同为 user role 而被合并。"""
    _patch(monkeypatch)
    agent = _Agent()
    agent.user_name = "主人"
    pb = PromptBuilder(agent)

    history = [
        _Msg("小雅", "user", "帮我看下报表"),
        _Msg("主人", "user", "稍等我看下"),  # owner 自己
        _Msg("王五", "user", "好的"),
    ]
    incoming = _Msg("小雅", "user", "在吗")

    messages = pb.build_user_message(incoming, history)
    contents = _history_user_contents(messages)

    assert len([c for c in contents if c.startswith("小雅：")]) == 1
    assert len([c for c in contents if c.startswith("主人：")]) == 1
    assert len([c for c in contents if c.startswith("王五：")]) == 1
    # 三人各不相同，必然三条独立消息
    assert len(contents) == 3


def test_system_prompt_has_closure_guidance():
    """system prompt 须以自然语言告知「对方说完了就收尾、不要追问」。"""
    agent = MagicMock()
    agent.user_name = "主人"
    agent.platform_id = "dingtalk"
    agent.user_dept = "研发部"
    agent.user_title = "工程师"
    agent.org_name = "公司"
    agent.config.system_prompt = "你是{user_name}的{platform}数字分身。"
    adv = SimpleNamespace(max_chars_daily_chat=200, max_chars_tech_issue=100)
    agent.config.advanced = adv

    prompt = build_system_prompt_core(agent, sender_name="小雅")

    assert "对话收尾" in prompt, "system prompt 应含对话收尾指令"
    assert "不用了" in prompt, "收尾指令应列举『不用了』等完结表达"
    assert "工号" in prompt, "收尾指令应明确『不要索要工号/手机号』"
