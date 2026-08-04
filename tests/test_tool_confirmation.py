"""ToolRouter 执行前二次确认（require_confirm）门控回归测试。

覆盖：首次调用拦截写操作仅返回 confirm_required（不执行副作用）；
携有效令牌才真实执行；错误/过期令牌被拒；按会话隔离；
非确认工具直执行；预览生成异常兜底。
"""
from __future__ import annotations

from src.tools.base import (
    BaseTool,
    PendingConfirmation,
    ToolRouter,
)
from src.config import ToolsConfig


class _FakeConfirmTool(BaseTool):
    name = "fake_confirm"
    description = "测试用确认工具"
    parameters = {"type": "object", "properties": {}}
    require_confirm = True
    executed: list[dict] = []

    def execute(self, args: dict) -> dict:
        _FakeConfirmTool.executed.append(dict(args))
        return {"ok": True, "echo": args.get("v")}

    def build_confirmation_preview(self, args: dict) -> str:
        return f"preview:{args.get('v')}"


class _FakePlainTool(BaseTool):
    name = "fake_plain"
    description = "测试用普通工具"
    parameters = {"type": "object", "properties": {}}
    executed: list[dict] = []

    def execute(self, args: dict) -> dict:
        _FakePlainTool.executed.append(dict(args))
        return {"ok": True}


class _CrashPreviewTool(BaseTool):
    name = "fake_crash"
    description = "预览会崩的工具"
    parameters = {"type": "object", "properties": {}}
    require_confirm = True

    def execute(self, args: dict) -> dict:
        return {"ok": True}

    def build_confirmation_preview(self, args: dict) -> str:
        raise RuntimeError("preview boom")


def _router(names):
    cfg = ToolsConfig(available=list(names), enabled=True, rate_limit={})
    r = ToolRouter(cfg)
    for t in (_FakeConfirmTool(), _FakePlainTool(), _CrashPreviewTool()):
        if t.name in names:
            r.register(t)
    return r


def test_require_confirm_blocks_first_call():
    _FakeConfirmTool.executed.clear()
    r = _router(["fake_confirm"])
    res = r.execute("fake_confirm", {"v": 1}, session_key="A")
    assert res.success is True
    assert res.result["status"] == "confirm_required"
    assert res.result["confirm_token"]
    assert res.result["preview"] == "preview:1"
    # 关键：首次调用不得执行任何写操作
    assert _FakeConfirmTool.executed == []


def test_valid_token_executes_original_args():
    _FakeConfirmTool.executed.clear()
    r = _router(["fake_confirm"])
    token = r.execute("fake_confirm", {"v": 1}, session_key="A").result["confirm_token"]
    # 确认调用携 token（且可能夹带无关参数 v=999），应执行「被确认时的原始参数」
    res = r.execute("fake_confirm", {"v": 999, "confirm_token": token}, session_key="A")
    assert res.success is True
    assert res.result == {"ok": True, "echo": 1}
    assert _FakeConfirmTool.executed == [{"v": 1}]


def test_wrong_token_rejected():
    _FakeConfirmTool.executed.clear()
    r = _router(["fake_confirm"])
    r.execute("fake_confirm", {"v": 1}, session_key="A")
    res = r.execute("fake_confirm", {"v": 1, "confirm_token": "deadbeef"}, session_key="A")
    assert res.success is False
    assert "无效或已过期" in res.error
    assert _FakeConfirmTool.executed == []


def test_expired_token_rejected():
    _FakeConfirmTool.executed.clear()
    r = _router(["fake_confirm"])
    import time
    r._confirmations["A"] = {
        "oldtok": PendingConfirmation(
            token="oldtok", tool_name="fake_confirm", args={"v": 1},
            preview="p", expires_at=time.time() - 1),
    }
    res = r.execute("fake_confirm", {"v": 1, "confirm_token": "oldtok"}, session_key="A")
    assert res.success is False
    assert "无效或已过期" in res.error
    assert _FakeConfirmTool.executed == []


def test_session_isolation():
    _FakeConfirmTool.executed.clear()
    r = _router(["fake_confirm"])
    token = r.execute("fake_confirm", {"v": 1}, session_key="A").result["confirm_token"]
    # 同一令牌在另一会话中无效
    res = r.execute("fake_confirm", {"v": 1, "confirm_token": token}, session_key="B")
    assert res.success is False
    # 原会话仍可用
    res2 = r.execute("fake_confirm", {"v": 1, "confirm_token": token}, session_key="A")
    assert res2.success is True
    assert _FakeConfirmTool.executed == [{"v": 1}]


def test_token_mismatch_tool_rejected():
    _FakeConfirmTool.executed.clear()
    r = _router(["fake_confirm", "fake_plain"])
    token = r.execute("fake_confirm", {"v": 1}, session_key="A").result["confirm_token"]
    # 用该令牌去调另一个工具 → 不匹配
    res = r.execute("fake_plain", {"confirm_token": token}, session_key="A")
    assert res.success is False


def test_plain_tool_executes_directly():
    _FakePlainTool.executed.clear()
    r = _router(["fake_plain"])
    res = r.execute("fake_plain", {"v": 7}, session_key="A")
    assert res.success is True
    assert _FakePlainTool.executed == [{"v": 7}]


def test_preview_crash_falls_back():
    r = _router(["fake_crash"])
    res = r.execute("fake_crash", {}, session_key="A")
    assert res.success is True
    assert res.result["status"] == "confirm_required"
    assert res.result["preview"].startswith("即将执行工具")
