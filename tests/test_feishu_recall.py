"""FeishuCliAdapter.chat_message_recall 真实撤回回归测试。"""
from unittest.mock import MagicMock

from src.im_adapter.feishu import FeishuCliAdapter


def _make_adapter() -> FeishuCliAdapter:
    return FeishuCliAdapter()


def test_recall_success_returns_true():
    a = _make_adapter()
    a.run = MagicMock(return_value={"code": 0})
    assert a.chat_message_recall(message_id="om_abc123") is True
    called = a.run.call_args[0][0]
    assert called[:3] == ["im", "messages", "delete"]
    assert "--message-id" in called and "om_abc123" in called
    assert "--yes" in called          # 仅用于 bot 自清理自己占位消息
    assert "--as" in called and "bot" in called
    # 真实执行（非 dry-run）
    assert a.run.call_args[1].get("force_no_dry_run") is True


def test_recall_failure_returns_false():
    a = _make_adapter()
    a.run = MagicMock(side_effect=RuntimeError("CLI 退出 1"))
    assert a.chat_message_recall(message_id="om_xyz") is False


def test_recall_exception_never_propagates():
    a = _make_adapter()
    a.run = MagicMock(side_effect=Exception("boom"))
    # 不应抛出，必须由调用方降级为覆盖式「已停止」文案
    assert a.chat_message_recall(message_id="om_1") is False
