"""Tools Base 模块单元测试 — 覆盖 RateLimiter, ToolRouter, BaseTool, ToolCallResult。"""
from __future__ import annotations

import time

from src.config import ToolsConfig
from src.tools.base import BaseTool, RateLimiter, ToolCallResult, ToolRouter


# ============================================================================
# ToolCallResult
# ============================================================================
class TestToolCallResult:
    def test_defaults(self):
        r = ToolCallResult(tool_name="t", args={}, success=True, result="ok")
        assert r.error is None
        assert r.duration_ms == 0

    def test_error_fields(self):
        r = ToolCallResult(tool_name="t", args={"a": 1}, success=False, result=None,
                           error="boom", duration_ms=150)
        assert r.error == "boom"
        assert r.duration_ms == 150


# ============================================================================
# BaseTool
# ============================================================================
class _DummyTool(BaseTool):
    name = "dummy"
    description = "A dummy tool"
    parameters = {"type": "object", "properties": {}}
    display_name = "测试工具"
    short_description = "用于单元测试的假工具"

    def execute(self, args):
        return {"result": args.get("x", 0) * 2}


class TestBaseTool:
    def test_to_openai_schema(self):
        t = _DummyTool()
        s = t.to_openai_schema()
        assert s["type"] == "function"
        assert s["function"]["name"] == "dummy"
        assert s["function"]["description"] == "A dummy tool"

    def test_get_info_with_display_fields(self):
        t = _DummyTool()
        info = t.get_info()
        assert info["name"] == "dummy"
        assert info["display_name"] == "测试工具"
        assert info["short_description"] == "用于单元测试的假工具"

    def test_get_info_fallback_when_no_display(self):
        class _MinTool(BaseTool):
            name = "min"
            description = "desc"
            parameters = {}
            def execute(self, args): pass
        t = _MinTool()
        info = t.get_info()
        assert info["display_name"] == "min"  # fallback to name

    def test_safe_execute_passthrough_on_success(self):
        """safe_execute 正常路径直接透传 execute 结果。"""
        t = _DummyTool()
        assert t.safe_execute({"x": 21}) == {"result": 42}

    def test_safe_execute_normalizes_uncaught_exception(self):
        """未捕获异常被规范化为 {error}（错误协议：返回而非抛出）。"""

        class _BoomTool(BaseTool):
            name = "boom"
            description = "desc"
            parameters = {}
            def execute(self, args):
                raise RuntimeError("底层炸了")
        t = _BoomTool()
        out = t.safe_execute({})
        assert isinstance(out, dict) and out.get("error")
        assert "底层炸了" in out["error"]

    def test_safe_execute_respects_tool_own_error_handling(self):
        """工具自身已 try/except 时 safe_execute 不重复包裹（先捕获者优先）。"""

        class _HandledTool(BaseTool):
            name = "handled"
            description = "desc"
            parameters = {}
            def execute(self, args):
                try:
                    raise ValueError("工具内已处理")
                except ValueError as e:
                    return {"error": f"自定义错误: {e}"}

        t = _HandledTool()
        out = t.safe_execute({})
        assert out == {"error": "自定义错误: 工具内已处理"}  # 保持工具原始返回

    def test_intent_keywords_default_empty(self):
        class _NoIntent(BaseTool):
            name = "nointent"
            description = "x"
            parameters = {}
            def execute(self, args): pass
        assert _NoIntent().intent_keywords == []


# ============================================================================
# RateLimiter
# ============================================================================
class TestRateLimiter:
    def test_first_call_passes(self):
        rl = RateLimiter()
        assert rl.check("tool_a", 10) is True

    def test_within_limit_passes(self):
        rl = RateLimiter()
        for _ in range(5):
            assert rl.check("tool_a", 10) is True

    def test_exceeds_global_limit(self):
        rl = RateLimiter()
        # fill up to limit
        for _ in range(3):
            assert rl.check("tool_b", 3) is True
        # next should fail
        assert rl.check("tool_b", 3) is False

    def test_session_limit_for_send_message(self):
        rl = RateLimiter()
        for _ in range(3):
            assert rl.check("send_message", 100, chat_id="chat1") is True
        # 4th within same session → fail
        assert rl.check("send_message", 100, chat_id="chat1") is False

    def test_session_limit_not_applied_to_other_tools(self):
        rl = RateLimiter()
        for _ in range(10):
            assert rl.check("other_tool", 100, chat_id="chat1") is True

    def test_old_calls_expire(self):
        rl = RateLimiter()
        # manually insert an old call
        rl._calls["tool_c"] = [time.time() - 3700]
        assert rl.check("tool_c", 1) is True  # old call expired, should pass


# ============================================================================
# ToolRouter
# ============================================================================
def _make_config(**kw) -> ToolsConfig:
    defaults = {"enabled": True, "available": ["dummy"], "rate_limit": {}}
    defaults.update(kw)
    return ToolsConfig(**defaults)


class TestToolRouter:
    def test_register_and_unregister(self):
        cfg = _make_config()
        router = ToolRouter(cfg)
        t = _DummyTool()
        router.register(t)
        assert "dummy" in router._tools
        router.unregister("dummy")
        assert "dummy" not in router._tools

    def test_get_schemas_enabled(self):
        cfg = _make_config()
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        schemas = router.get_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "dummy"

    def test_get_schemas_disabled(self):
        cfg = _make_config(enabled=False)
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        assert router.get_schemas() == []

    def test_get_all_info(self):
        cfg = _make_config()
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        infos = router.get_all_info()
        assert len(infos) == 1
        assert infos[0]["name"] == "dummy"

    def test_get_available_tool_names_enabled(self):
        cfg = _make_config(available=["dummy", "ghost"])
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        names = router.get_available_tool_names()
        assert names == ["dummy"]  # ghost not registered

    def test_get_available_tool_names_disabled(self):
        cfg = _make_config(enabled=False)
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        assert router.get_available_tool_names() == []

    def test_filter_schemas_by_names(self):
        cfg = _make_config(available=["dummy"])
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        schemas = router.filter_schemas_by_names(["dummy"])
        assert len(schemas) == 1

    def test_filter_schemas_disabled(self):
        cfg = _make_config(enabled=False)
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        assert router.filter_schemas_by_names(["dummy"]) == []

    # --- execute ---
    def test_execute_success(self):
        cfg = _make_config()
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        r = router.execute("dummy", {"x": 5})
        assert r.success is True
        assert r.result == {"result": 10}
        assert r.duration_ms >= 0

    def test_execute_not_in_whitelist(self):
        cfg = _make_config(available=["other"])
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        r = router.execute("dummy", {})
        assert r.success is False
        assert "not in whitelist" in r.error

    def test_execute_not_registered(self):
        cfg = _make_config(available=["ghost"])
        router = ToolRouter(cfg)
        r = router.execute("ghost", {})
        assert r.success is False
        assert "not registered" in r.error

    def test_execute_rate_limited(self):
        cfg = _make_config(rate_limit={"dummy": {"per_hour": 1}})
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        # first call passes
        r1 = router.execute("dummy", {"x": 1})
        assert r1.success is True
        # second call rate-limited
        r2 = router.execute("dummy", {"x": 2})
        assert r2.success is False
        assert "Rate limit exceeded" in r2.error

    def test_execute_tool_exception(self):
        class _FailingTool(BaseTool):
            name = "failer"
            description = "fails"
            parameters = {}
            def execute(self, args):
                raise RuntimeError("模拟崩溃")
        cfg = _make_config(available=["failer"])
        router = ToolRouter(cfg)
        router.register(_FailingTool())
        r = router.execute("failer", {})
        assert r.success is False
        assert "模拟崩溃" in r.error
        assert r.duration_ms >= 0


class TestToolAvailabilityAudit:
    """F2/F5 收口：受控可审计的工具放行 + 白名单漂移排除技能工具。"""

    def test_mark_available_defaults_to_whitelist_source(self):
        cfg = _make_config()
        router = ToolRouter(cfg)
        router.mark_available("extra_tool")
        assert "extra_tool" in router._available
        assert router._availability_sources["extra_tool"] == "whitelist"

    def test_mark_available_skill_source_tracked(self):
        cfg = _make_config()
        router = ToolRouter(cfg)
        router.mark_available("skill_weather", source="skill")
        assert router._availability_sources["skill_weather"] == "skill"
        assert router.get_skill_sourced_tools() == {"skill_weather"}

    def test_get_skill_sourced_tools_only_returns_skill(self):
        cfg = _make_config(available=["dummy"])
        router = ToolRouter(cfg)
        router.mark_available("skill_a", source="skill")
        router.mark_available("skill_b", source="skill")
        router.mark_available("builtin_extra")  # default whitelist source
        assert router.get_skill_sourced_tools() == {"skill_a", "skill_b"}

    def test_compute_whitelist_drift_excludes_skill_tools(self):
        cfg = _make_config(available=["dummy"])
        router = ToolRouter(cfg)
        router.register(_DummyTool())          # 内建，在白名单
        router.register(_GhostTool())          # 内建，不在白名单 → 应报缺失
        router.mark_available("skill_x", source="skill")  # 技能，有意绕过白名单
        router.register(_SkillProxyTool())     # 对应 skill_x 的注册体
        # 白名单故意不含 ghost / skill_x
        drift = router.compute_whitelist_drift(whitelist={"dummy"})
        # ghost 是真实缺失的内建工具 → 仍在缺失告警
        assert "ghost" in drift["missing_in_whitelist"]
        # skill_x 是有意绕过白名单的技能工具 → 从缺失告警排除
        assert "skill_x" not in drift["missing_in_whitelist"]
        # 但保留在 skill_auto_wrapped 可见性字段
        assert drift["skill_auto_wrapped"] == ["skill_x"]
        assert drift["registered_count"] == 3
        assert drift["whitelist_count"] == 1

    def test_compute_whitelist_drift_stale_entries(self):
        cfg = _make_config(available=["dummy", "nonexistent_tool"])
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        drift = router.compute_whitelist_drift(whitelist={"dummy", "nonexistent_tool"})
        assert drift["stale_in_whitelist"] == ["nonexistent_tool"]


class _GhostTool(BaseTool):
    name = "ghost"
    description = "未列入白名单的内建工具"
    parameters = {}
    def execute(self, args): pass


class _SkillProxyTool(BaseTool):
    name = "skill_x"
    description = "技能包装工具"
    parameters = {}
    def execute(self, args): pass
