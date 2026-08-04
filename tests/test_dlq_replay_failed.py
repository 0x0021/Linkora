"""DLQ replay_dead_letter 失败态测试。

护栏 P0-3 伴生修复：重放失败时必须把状态标为 failed 并记录原因，
避免管理台反复点重放反复跑 handler（虽然幂等但体验差、无状态）。
"""
from __future__ import annotations

from unittest.mock import MagicMock


from main import LinkoraEngine


def _make_app_with_mock_replay_handler(monkeypatch, *, raise_exc: Exception):
    """构造一个 LinkoraEngine 实例，patch _handle_message_impl 让其抛指定异常。"""
    # 避免真实启动副作用
    monkeypatch.setattr("main.load_config", lambda *a, **kw: MagicMock())
    app = LinkoraEngine.__new__(LinkoraEngine)
    # 给最小可用的 store mock
    app.store = MagicMock()
    app.store._draft_repo.get_dead_letter.return_value = {
        "id": 42,
        "msg_id": "m_replay_42",
        "chat_id": "C1",
        "chat_name": "测试群",
        "sender_id": "U1",
        "sender_name": "测试人",
        "content": "hi",
        "msg_type": "text",
        "raw": {"chat_type": "group"},
        "status": "pending",
    }
    # handler 抛异常
    app._handle_message_impl = MagicMock(side_effect=raise_exc)
    return app


def test_replay_failure_marks_status_failed(monkeypatch):
    """重放失败 → resolve_dead_letter(status='failed', note=...) 必须被调用。"""
    app = _make_app_with_mock_replay_handler(
        monkeypatch, raise_exc=RuntimeError("模拟 handler 崩溃"),
    )
    r = app.replay_dead_letter(42)
    assert r["success"] is False
    assert r["status"] == "failed"
    assert "handler 崩溃" in r["error"]
    # store 应被调为标 failed
    app.store._draft_repo.resolve_dead_letter.assert_called_once()
    args, kwargs = app.store._draft_repo.resolve_dead_letter.call_args
    assert kwargs["status"] == "failed" or (len(args) >= 2 and args[1] == "failed")
    assert "失败" in kwargs.get("note", "")


def test_replay_success_returns_true(monkeypatch):
    """重放成功 → status='replayed'，返回 success=True。"""
    monkeypatch.setattr("main.load_config", lambda *a, **kw: MagicMock())
    app = LinkoraEngine.__new__(LinkoraEngine)
    app.store = MagicMock()
    app.store._draft_repo.get_dead_letter.return_value = {
        "id": 7,
        "msg_id": "m7",
        "chat_id": "C1",
        "chat_name": "g",
        "sender_id": "U1",
        "sender_name": "u",
        "content": "hi",
        "msg_type": "text",
        "raw": {"chat_type": "group"},
        "status": "pending",
    }
    app._handle_message_impl = MagicMock(return_value=None)
    r = app.replay_dead_letter(7)
    assert r["success"] is True
    app.store._draft_repo.resolve_dead_letter.assert_called_once()
    args, kwargs = app.store._draft_repo.resolve_dead_letter.call_args
    assert kwargs["status"] == "replayed" or (len(args) >= 2 and args[1] == "replayed")


def test_replay_not_pending_rejected(monkeypatch):
    """非 pending 状态的 DLQ 不允许重放（避免双重重放/已失败的不再尝试）。"""
    monkeypatch.setattr("main.load_config", lambda *a, **kw: MagicMock())
    app = LinkoraEngine.__new__(LinkoraEngine)
    app.store = MagicMock()
    app.store._draft_repo.get_dead_letter.return_value = {
        "id": 1, "msg_id": "m1", "chat_id": "C1", "chat_name": "g",
        "sender_id": "U", "sender_name": "u", "content": "x", "msg_type": "text",
        "raw": {}, "status": "replayed",  # 已重放过
    }
    r = app.replay_dead_letter(1)
    assert r["success"] is False
    assert "replayed" in r["error"]
    app.store._draft_repo.resolve_dead_letter.assert_not_called()
