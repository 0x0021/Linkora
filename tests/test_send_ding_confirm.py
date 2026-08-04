"""SendDingTool 强提醒(sms/call)二次确认门控测试。

覆盖：
- app 类型不需要确认（needs_confirm 返回 False）
- sms / call 类型需要确认（needs_confirm 返回 True）
- build_confirmation_preview 生成可读预览，且不触发任何 dws 调用
"""
import pytest


@pytest.fixture
def tool():
    from unittest.mock import MagicMock
    from src.tools.business import SendDingTool
    return SendDingTool(MagicMock())


def test_app_does_not_require_confirm(tool):
    assert tool.needs_confirm({"type": "app", "users": "u1", "content": "hi"}) is False
    # 缺省 type 视为 app，同样放行
    assert tool.needs_confirm({"users": "u1", "content": "hi"}) is False


def test_sms_requires_confirm(tool):
    assert tool.needs_confirm({"type": "sms", "users": "u1", "content": "hi"}) is True


def test_call_requires_confirm(tool):
    assert tool.needs_confirm({"type": "call", "users": "u1", "content": "hi"}) is True


def test_preview_is_readonly_and_readable(tool):
    """预览必须只读：不得触发任何 dws.run 调用。"""
    preview = tool.build_confirmation_preview({
        "users": "u1,u2", "type": "sms", "content": "紧急：系统将于今晚维护",
    })
    assert "短信" in preview
    assert "u1,u2" in preview
    assert "紧急" in preview
    assert "确认" in preview
    tool.dws.run.assert_not_called()


def test_preview_handles_empty_users(tool):
    preview = tool.build_confirmation_preview({"type": "call", "content": "x"})
    assert "电话" in preview
    assert "确认" in preview
