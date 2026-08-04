"""流式回复中途失败残留半成品修复回归测试（M-1）。

验证 src.llm.agent.LLMAgent._handle_stream_response 的异常分支：
- 异常时优先调用 im_adapter.chat_message_recall 撤回占位消息
- 适配器不支持撤回（返回 False）时，降级为覆盖一条“已停止”纠正消息
- 适配器支持撤回（返回 True）时不发送“已停止”降级文案
"""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _make_agent(recall_return):
    from src.llm.agent import LLMAgent
    from src.llm.client import LLMStreamChunk

    agent = LLMAgent.__new__(LLMAgent)
    agent.im_adapter = MagicMock()
    agent.im_adapter.chat_message_send.return_value = {"msgId": "m1"}
    agent.im_adapter.chat_message_recall.return_value = recall_return
    agent._enforce_brevity = lambda x: x
    agent.logger = logging.getLogger("test_stream_recall")
    return agent, LLMStreamChunk


def _bad_stream(chunk_cls):
    yield chunk_cls(content="半截内容", tool_calls=[], finish_reason=None, is_done=False)
    raise RuntimeError("LLM 断流")


def test_stream_failure_recalls_then_downgrades_to_notice():
    agent, Chunk = _make_agent(recall_return=False)
    msg = SimpleNamespace(chat_id="c1", sender_id="u1")

    with pytest.raises(RuntimeError):
        list(agent._handle_stream_response(_bad_stream(Chunk), msg))

    agent.im_adapter.chat_message_recall.assert_called_once()
    # 降级：最后一次 update 应覆盖为“已停止”纠正文案
    last = agent.im_adapter.chat_message_update.call_args_list[-1]
    assert "已停止" in last.kwargs["text"]


def test_stream_failure_when_recall_supported_skips_downgrade_text():
    agent, Chunk = _make_agent(recall_return=True)
    msg = SimpleNamespace(chat_id="c1", sender_id="u1")

    with pytest.raises(RuntimeError):
        list(agent._handle_stream_response(_bad_stream(Chunk), msg))

    agent.im_adapter.chat_message_recall.assert_called_once()
    # 撤回成功时不应出现“已停止”降级文案
    for call in agent.im_adapter.chat_message_update.call_args_list:
        assert "已停止" not in call.kwargs["text"]
