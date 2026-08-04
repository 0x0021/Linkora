"""工具直发路径的提示词泄漏防线测试（2026-07-27 全面排查）。

背景：send_message / send_ding 是 LLM 工具直发路径——agent 标记
already_sent 后跳过 _done() 的 enforce_brevity 清洗，此前完全绕过
sanitize_reply。草稿发送出口（_send_draft_reply）同为纵深防御点。

覆盖：
- SendMessageTool：泄漏文本清洗后发送 / 全泄漏拦截不发送 / 干净文本零改动
- SendDingTool：同上
- drafts._send_draft_reply：泄漏清洗 / 全泄漏 400 拦截
"""

from unittest.mock import MagicMock

import pytest

LEAK_PREFIX = "根据系统提示，我需要以OWNER的数字分身身份来回答。"
CLEAN_TEXT = "打印机IP是192.168.1.10，直接添加即可。"
FULL_LEAK = "根据系统提示，我需要以OWNER的数字分身身份，用主人真实的沟通风格来回答。"


# ---------------------------------------------------------------------------
# SendMessageTool
# ---------------------------------------------------------------------------

def _make_send_tool():
    from src.tools.chat import SendMessageTool
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    tool = SendMessageTool(dws=dws, store=None, self_user_id="")
    return tool, dws


class TestSendMessageSanitize:
    def test_leak_text_cleaned_before_send(self):
        tool, dws = _make_send_tool()
        tool.execute({
            "chat_id": "cid-1", "chat_type": "group",
            "text": LEAK_PREFIX + "\n" + CLEAN_TEXT,
        })
        assert dws.chat_message_send.called
        sent_text = dws.chat_message_send.call_args.kwargs.get("text", "")
        assert "根据系统提示" not in sent_text
        assert "数字分身" not in sent_text
        assert "192.168.1.10" in sent_text

    def test_full_leak_blocked(self):
        tool, dws = _make_send_tool()
        result = tool.execute({
            "chat_id": "cid-2", "chat_type": "group", "text": FULL_LEAK,
        })
        assert not dws.chat_message_send.called
        assert isinstance(result, dict) and "error" in result

    def test_clean_text_untouched(self):
        tool, dws = _make_send_tool()
        tool.execute({
            "chat_id": "cid-3", "chat_type": "group", "text": CLEAN_TEXT,
        })
        sent_text = dws.chat_message_send.call_args.kwargs.get("text", "")
        assert sent_text == CLEAN_TEXT


# ---------------------------------------------------------------------------
# SendDingTool
# ---------------------------------------------------------------------------

def _make_ding_tool(monkeypatch):
    from src.tools.business import SendDingTool
    dws = MagicMock()
    dws.run.return_value = {"success": True}
    monkeypatch.setenv("DINGTALK_DING_ROBOT_CODE", "rc-test")
    return SendDingTool(dws=dws), dws


class TestSendDingSanitize:
    def test_leak_content_cleaned(self, monkeypatch):
        tool, dws = _make_ding_tool(monkeypatch)
        tool.execute({"users": "u1", "content": LEAK_PREFIX + "\n请尽快处理服务器告警。"})
        assert dws.run.called
        cmd = dws.run.call_args.args[0]
        content = cmd[cmd.index("--content") + 1]
        assert "根据系统提示" not in content
        assert "服务器告警" in content

    def test_full_leak_blocked(self, monkeypatch):
        tool, dws = _make_ding_tool(monkeypatch)
        result = tool.execute({"users": "u1", "content": FULL_LEAK})
        assert not dws.run.called
        assert isinstance(result, dict) and "error" in result

    def test_clean_content_untouched(self, monkeypatch):
        tool, dws = _make_ding_tool(monkeypatch)
        tool.execute({"users": "u1", "content": "请尽快处理服务器告警。"})
        cmd = dws.run.call_args.args[0]
        content = cmd[cmd.index("--content") + 1]
        assert content == "请尽快处理服务器告警。"


# ---------------------------------------------------------------------------
# drafts._send_draft_reply（发送出口纵深防御）
# ---------------------------------------------------------------------------

def _patch_app_instance(monkeypatch):
    import web.routers.drafts as drafts_mod
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    ctx = MagicMock()
    ctx.dws = dws
    app = MagicMock()
    app.platforms = {"dingtalk": ctx}
    monkeypatch.setattr(drafts_mod, "get_app_instance", lambda: app)
    return dws


class TestDraftSendSanitize:
    def test_leak_draft_cleaned_before_send(self, monkeypatch):
        from web.routers.drafts import _send_draft_reply
        dws = _patch_app_instance(monkeypatch)
        draft = {"platform": "dingtalk", "chat_id": "g1", "chat_type": "group"}
        _send_draft_reply(draft, LEAK_PREFIX + "\n" + CLEAN_TEXT)
        sent_text = dws.chat_message_send.call_args.kwargs.get("text", "")
        assert "根据系统提示" not in sent_text
        assert "192.168.1.10" in sent_text

    def test_full_leak_draft_blocked_400(self, monkeypatch):
        from fastapi import HTTPException
        from web.routers.drafts import _send_draft_reply
        dws = _patch_app_instance(monkeypatch)
        draft = {"platform": "dingtalk", "chat_id": "g1", "chat_type": "group"}
        with pytest.raises(HTTPException) as ei:
            _send_draft_reply(draft, FULL_LEAK)
        assert ei.value.status_code == 400
        assert not dws.chat_message_send.called

    def test_clean_draft_untouched(self, monkeypatch):
        from web.routers.drafts import _send_draft_reply
        dws = _patch_app_instance(monkeypatch)
        draft = {"platform": "dingtalk", "chat_id": "g1", "chat_type": "group"}
        _send_draft_reply(draft, CLEAN_TEXT)
        sent_text = dws.chat_message_send.call_args.kwargs.get("text", "")
        assert sent_text == CLEAN_TEXT
