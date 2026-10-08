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


class _RequiredTool(BaseTool):
    name = "req"
    description = "A tool with a required param"
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def execute(self, args):
        return {"echo": args.get("query")}


class TestToolRouterInputValidation:
    """P1-12：边界层入参校验，失败时返回清晰错误而非把内部异常泄漏给 LLM。"""

    def test_missing_required_param_rejected(self):
        cfg = _make_config(available=["req"])
        router = ToolRouter(cfg)
        router.register(_RequiredTool())
        r = router.execute("req", {})
        assert r.success is False
        assert "缺少必填参数" in r.error
        assert "query" in r.error

    def test_none_args_with_required_rejected(self):
        cfg = _make_config(available=["req"])
        router = ToolRouter(cfg)
        router.register(_RequiredTool())
        r = router.execute("req", None)
        assert r.success is False
        assert "缺少必填参数" in r.error

    def test_non_dict_args_rejected(self):
        cfg = _make_config(available=["req"])
        router = ToolRouter(cfg)
        router.register(_RequiredTool())
        r = router.execute("req", ["not", "a", "dict"])
        assert r.success is False
        assert "必须是 JSON 对象" in r.error

    def test_valid_args_passes_through(self):
        cfg = _make_config(available=["req"])
        router = ToolRouter(cfg)
        router.register(_RequiredTool())
        r = router.execute("req", {"query": "hi"})
        assert r.success is True
        assert r.result == {"echo": "hi"}

    def test_none_args_tolerated_when_no_required(self):
        """无必填参数的工具（如 dummy）应容忍 None 入参，后续按空对象处理。"""
        cfg = _make_config(available=["dummy"])
        router = ToolRouter(cfg)
        router.register(_DummyTool())
        r = router.execute("dummy", None)
        assert r.success is True


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


# ============================================================================
# 外层执行超时护栏（P1-11 回归）
# ============================================================================
class _HangTool(BaseTool):
    name = "hang"
    description = "卡死工具"
    parameters = {}
    def execute(self, args):
        time.sleep(30)  # 远超任何合理超时，用于触发护栏
        return "never"


class _SlowButOkTool(BaseTool):
    name = "slow_ok"
    description = "在超时内完成的工具"
    parameters = {}
    def execute(self, args):
        time.sleep(0.05)
        return {"result": "done"}


class TestToolRouterTimeout:
    def test_normal_execution_within_timeout(self):
        """启用超时护栏时，正常完成的工具仍返回正确结果（不误杀）。"""
        cfg = _make_config(available=["slow_ok"], max_tool_seconds=5.0)
        router = ToolRouter(cfg)
        router.register(_SlowButOkTool())
        res = router.execute("slow_ok", {})
        assert res.success is True
        assert res.result == {"result": "done"}

    def test_hanging_tool_times_out(self):
        """卡死工具超过 max_tool_seconds 被记为失败，错误文案明确。"""
        cfg = _make_config(available=["hang"], max_tool_seconds=0.3)
        router = ToolRouter(cfg)
        router.register(_HangTool())
        res = router.execute("hang", {})
        assert res.success is False
        assert res.error == "工具执行超时"

    def test_timeout_disabled_runs_without_guard(self):
        """max_tool_seconds<=0 时退回无护栏路径（不报错，正常执行）。"""
        cfg = _make_config(available=["slow_ok"], max_tool_seconds=0)
        router = ToolRouter(cfg)
        router.register(_SlowButOkTool())
        res = router.execute("slow_ok", {})
        assert res.success is True
