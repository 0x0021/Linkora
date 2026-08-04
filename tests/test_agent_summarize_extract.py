"""agent.py 中 extract_memories_from_conversation 和 summarize_conversation 的测试。

两方法都是「组装 prompt → 调 client.chat → 容错解析」模式。
核心覆盖：
- 空消息短路
- prompt 组装内容（role/截断长度）
- 正常 JSON 解析
- LLM 输出被 markdown 包裹的清理
- 异常路径（JSON 错、LLM 报错、空 content）
- 长度过滤
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from src.llm.agent import LLMAgent
from src.llm.client import LLMResponse
from src.models import Message


# ============ Fixtures ============

def _msg(content, role="user", sender="u", chat_id="c1"):
    return Message(
        msg_id=f"m_{role}_{content[:10]}",
        chat_id=chat_id,
        chat_type="group",
        chat_name="g",
        sender_id=sender,
        sender_name=sender,
        content=content,
        msg_type="text",
        timestamp=datetime.now(),
        role=role,
    )


def _make_agent(client):
    """构造一个最小可用的 LLMAgent。两个方法不依赖 tool_router/store，传 None 即可。"""
    config = MagicMock()
    config.system_prompt = ""
    return LLMAgent(
        config=config,
        client=client,
        tool_router=None,
        user_name="",
        user_dept="",
        org_name="",
        store=None,
    )


# ============ extract_memories_from_conversation ============

class TestExtractMemories:
    def test_empty_messages_returns_empty(self):
        agent = _make_agent(MagicMock())
        assert agent.extract_memories_from_conversation([]) == []

    def test_returns_memories_from_clean_json_array(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content='["用户叫张三", "负责后端开发", "邮箱 zhangsan@example.com"]',
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        msgs = [_msg("我是张三，负责后端开发。")]
        result = agent.extract_memories_from_conversation(msgs)
        assert result == ["用户叫张三", "负责后端开发", "邮箱 zhangsan@example.com"]

    def test_strips_markdown_fences(self):
        """LLM 偶尔返回 ```json ... ``` 包裹，要清理。

        遗留：过滤阈值 len >= 4 严格会误杀中文短记忆（'记忆A' = 3 字符被滤）。
        本测试用 ≥5 字符的样例绕开。实际阈值见 test_filters_short_strings。
        """
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content='```json\n["记忆条目A足够长", "记忆条目B足够长"]\n```',
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        result = agent.extract_memories_from_conversation([_msg("x")])
        assert result == ["记忆条目A足够长", "记忆条目B足够长"]

    def test_empty_array_returns_empty(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="[]",
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        assert agent.extract_memories_from_conversation([_msg("闲聊")]) == []

    def test_filters_short_strings(self):
        """【行为变更】阈值从 len>=3 改为 "≥5 + 非纯标点"。
        保留：5+ 字符且含字母/数字/中文。
        滤除：空/纯空白/1-4 字符/纯标点/纯寒暄。
        """
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            # 保留: 记忆条目A(5) / 项目用 Go(6)
            # 滤除: 记忆A(3) / 好的(2) / OK(2) / 好(1) / x(1) / 是(1) / 空 / 空白 / !!(2 标点)
            content='["记忆条目A", "项目用 Go", "记忆A", "好的", "OK", "好", "x", "是", "", "   ", "!!"]',
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        result = agent.extract_memories_from_conversation([_msg("x")])
        # 保留
        assert "记忆条目A" in result
        assert "项目用 Go" in result
        # 滤除
        for trash in ["记忆A", "好的", "OK", "好", "x", "是", "", "   ", "!!"]:
            assert trash not in result

    def test_chinese_short_memory_kept(self):
        """5+ 字符的中文记忆正常保留。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content='["记忆条目A", "记忆条目B"]',  # 5 字符中文
            tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        result = agent.extract_memories_from_conversation([_msg("x")])
        assert result == ["记忆条目A", "记忆条目B"]

    def test_invalid_json_returns_empty(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="这不是合法 JSON 内容",
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        # 解析失败应返回空，不抛
        assert agent.extract_memories_from_conversation([_msg("x")]) == []

    def test_non_list_json_returns_empty(self):
        """LLM 返回 JSON 但不是数组，应返回空。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content='{"memory": "not an array"}',
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        assert agent.extract_memories_from_conversation([_msg("x")]) == []

    def test_filters_non_string_elements(self):
        """数组中混入非字符串元素应被跳过。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content='["有效记忆条", 123, null, "另一条有效"]',
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        result = agent.extract_memories_from_conversation([_msg("x")])
        assert "有效记忆条" in result
        assert "另一条有效" in result
        assert len(result) == 2

    def test_client_exception_returns_empty(self):
        """LLM 调用异常时不能中断主流程，应返回空列表。"""
        client = MagicMock()
        client.chat.side_effect = RuntimeError("网络超时")
        agent = _make_agent(client)
        # 主流程不抛
        assert agent.extract_memories_from_conversation([_msg("x")]) == []

    def test_empty_content_returns_empty(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content=None,  # 兜底分支
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        assert agent.extract_memories_from_conversation([_msg("x")]) == []

    def test_only_uses_recent_8_messages(self):
        """传 10 条消息时，prompt 中只包含最近 8 条（保留更多上下文）。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="[]", tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        msgs = [_msg(f"msg{i}", role="user" if i % 2 == 0 else "assistant") for i in range(10)]
        agent.extract_memories_from_conversation(msgs)
        # 取 LLM 实际收到的 prompt
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        # 只应包含最后 8 条：msg2-msg9
        assert "msg2" in user_content
        assert "msg3" in user_content
        assert "msg4" in user_content
        assert "msg5" in user_content
        assert "msg6" in user_content
        assert "msg7" in user_content
        assert "msg8" in user_content
        assert "msg9" in user_content
        # 前 2 条不应在 prompt 中
        assert "msg0:" not in user_content
        assert "msg1:" not in user_content

    def test_truncates_content_to_300_chars(self):
        """单条消息 content 被截断到 300 字符。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="[]", tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        long_content = "x" * 500
        agent.extract_memories_from_conversation([_msg(long_content)])
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        # 截断后 prompt 里 300 个 x + 不会到 500
        assert user_content.count("x") == 300

    def test_role_label_in_prompt(self):
        """assistant 角色应标记为「我」，user 角色标记为「对方」。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="[]", tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        msgs = [
            _msg("我叫什么", role="user"),
            _msg("你叫张三", role="assistant"),
        ]
        agent.extract_memories_from_conversation(msgs)
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        assert "对方: 我叫什么" in user_content
        assert "我: 你叫张三" in user_content


# ============ _build_system_prompt_core 职位注入 ============

class TestSystemPromptTitle:
    """验证 user_title 被注入身份行，且空缺时省略（修复 bot 误编'转给IT'）。"""

    def _agent(self, user_title: str) -> LLMAgent:
        config = MagicMock()
        config.system_prompt = ""
        return LLMAgent(
            config=config, client=MagicMock(), tool_router=None,
            user_name="OWNER", user_dept="总裁办", org_name="公司",
            user_title=user_title, store=None,
        )

    def test_title_injected_when_present(self):
        prompt = self._agent("IT")._build_system_prompt_core()
        assert "身份:OWNER的数字分身。" in prompt
        assert "部门:总裁办。" in prompt
        assert "职位:IT。" in prompt
        assert "组织:公司。" in prompt

    def test_title_omitted_when_empty(self):
        prompt = self._agent("")._build_system_prompt_core()
        # 静态指令串「姓名/部门/职位/组织」含「职位」二字，故只能校验注入格式「职位:」缺失
        assert "职位:" not in prompt
        assert "身份:OWNER的数字分身。" in prompt


# ============ summarize_conversation ============

class TestSummarizeConversation:
    def test_empty_messages_returns_empty_string(self):
        agent = _make_agent(MagicMock())
        assert agent.summarize_conversation([]) == ""

    def test_returns_summary_text(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="【对话摘要】用户询问项目进度。",
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        result = agent.summarize_conversation([_msg("项目进度如何？")])
        assert "【对话摘要】" in result
        assert "项目进度" in result

    def test_strips_whitespace(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="  \n  【对话摘要】内容  \n  ",
            tool_calls=[],
            finish_reason="stop",
            usage={},
        )
        agent = _make_agent(client)
        result = agent.summarize_conversation([_msg("x")])
        assert result == "【对话摘要】内容"

    def test_client_exception_returns_empty(self):
        client = MagicMock()
        client.chat.side_effect = RuntimeError("API 报错")
        agent = _make_agent(client)
        # 不应向上抛
        assert agent.summarize_conversation([_msg("x")]) == ""

    def test_empty_content_returns_empty(self):
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content=None, tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        assert agent.summarize_conversation([_msg("x")]) == ""

    def test_max_messages_truncates(self):
        """max_messages > 0 且超过时,只取最后 max_messages 条。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="【对话摘要】ok",
            tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        msgs = [_msg(f"m{i}") for i in range(10)]
        agent.summarize_conversation(msgs, max_messages=3)
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        # 只应包含最后 3 条
        assert "m7" in user_content
        assert "m8" in user_content
        assert "m9" in user_content
        assert "m0:" not in user_content
        assert "m6:" not in user_content

    def test_max_messages_zero_means_unlimited(self):
        """max_messages=0 时不裁剪。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="【对话摘要】ok",
            tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        msgs = [_msg(f"m{i}") for i in range(20)]
        agent.summarize_conversation(msgs, max_messages=0)
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        # 应包含首尾
        assert "m0" in user_content
        assert "m19" in user_content

    def test_max_messages_larger_than_input_noop(self):
        """max_messages > len(messages) 不报错,原样使用全部。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="【对话摘要】ok",
            tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        msgs = [_msg("a"), _msg("b")]
        agent.summarize_conversation(msgs, max_messages=10)
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        assert "a" in user_content
        assert "b" in user_content

    def test_truncates_content_to_300_chars(self):
        """summarize 截断长度是 300（比 extract 的 150 长，因要保留更多上下文）。"""
        client = MagicMock()
        client.chat.return_value = LLMResponse(
            content="【对话摘要】x",
            tool_calls=[], finish_reason="stop", usage={},
        )
        agent = _make_agent(client)
        long_content = "y" * 500
        agent.summarize_conversation([_msg(long_content)])
        prompt_msgs = client.chat.call_args[0][0]
        user_content = prompt_msgs[-1]["content"]
        # 截断到 300
        assert user_content.count("y") == 300
