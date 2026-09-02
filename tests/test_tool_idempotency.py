"""副作用工具幂等护栏回归测试。

验证 ToolRouter 对 at-least-once 重放场景的防护：
- 窗口内重复调用副作用工具 → 跳过真实执行，返回首次成功结果（不双发）。
- 首次执行失败 → 不记录 → 可正常重试（失败语义保留）。
- 不同参数 → 视为不同调用，各自执行。
"""
from __future__ import annotations

from src.config import ToolsConfig
from src.tools.base import BaseTool, ToolRouter
from src.tools.idempotency import SIDE_EFFECT_TOOLS, SideEffectIdempotencyGuard


class _CallCountingTool(BaseTool):
    """记录 execute 被真实调用的次数（绕过 safe_execute 的兜底）。"""

    def __init__(self, name: str, behaviour: str = "ok"):
        self.name = name
        self.intent_keywords: list[str] = []
        self.description = f"{name} 测试工具"
        self.parameters = {"type": "object", "properties": {}}
        self.calls = 0
        self.behaviour = behaviour  # "ok" | "fail"

    def execute(self, args: dict):
        self.calls += 1
        if self.behaviour == "fail":
            return {"error": "模拟执行失败"}
        return {"echo": args}


def _router_for(name: str) -> tuple[ToolRouter, _CallCountingTool]:
    cfg = ToolsConfig(available=[name])
    router = ToolRouter(cfg)
    tool = _CallCountingTool(name)
    router.register(tool)
    return router, tool


def test_side_effect_replay_suppressed():
    """同一副作用工具、窗口内相同参数重放 → 只真正执行一次。"""
    assert "send_message" in SIDE_EFFECT_TOOLS
    router, tool = _router_for("send_message")
    args = {"chat_id": "cid1", "text": "重复内容"}

    r1 = router.execute("send_message", args, session_key="cid1")
    assert r1.success is True
    assert tool.calls == 1

    r2 = router.execute("send_message", args, session_key="cid1")
    assert r2.success is True
    # 重放未真正执行，返回首次结果
    assert tool.calls == 1
    assert r2.result == {"echo": args}


def test_failed_first_execution_retries():
    """首次执行失败不记录幂等 → 重放可正常重试（失败语义保留）。"""
    cfg = ToolsConfig(available=["create_todo"])
    router = ToolRouter(cfg)
    tool = _CallCountingTool("create_todo", behaviour="fail")
    router.register(tool)
    args = {"title": "待办A"}

    r1 = router.execute("create_todo", args, session_key="s1")
    assert r1.success is False
    assert tool.calls == 1

    r2 = router.execute("create_todo", args, session_key="s1")
    # 失败未记录 → 允许重试，真正再次执行
    assert r2.success is False
    assert tool.calls == 2


def test_different_args_execute_separately():
    """不同参数的副作用调用视为不同调用，各自执行。"""
    router, tool = _router_for("save_memory")
    router.execute("save_memory", {"content": "A"}, session_key="s1")
    router.execute("save_memory", {"content": "B"}, session_key="s1")
    assert tool.calls == 2


def test_non_side_effect_always_executes():
    """非副作用工具不受护栏影响，每次都执行。"""
    name = "get_weather"  # 不在 SIDE_EFFECT_TOOLS
    assert name not in SIDE_EFFECT_TOOLS
    router, tool = _router_for(name)
    router.execute(name, {"city": "北京"}, session_key="s1")
    router.execute(name, {"city": "北京"}, session_key="s1")
    assert tool.calls == 2


def test_guard_purges_expired_entries():
    """超过 TTL 的条目被清理，重放不再被拦。"""
    guard = SideEffectIdempotencyGuard(ttl_seconds=1)
    key_args = {"x": 1}
    assert guard.get("send_message", "s", key_args) == (False, None)
    guard.record_success("send_message", "s", key_args, {"ok": True})
    assert guard.get("send_message", "s", key_args) == (True, {"ok": True})
    import time
    time.sleep(1.1)
    # 过期后应不再命中（purge 在 get 内触发）
    assert guard.get("send_message", "s", key_args) == (False, None)
