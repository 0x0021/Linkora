"""外联护栏单元测试：禁止 AI 主动联系第三方（send_ding / 跨会话 send_message）。

默认开启（ToolsConfig.block_outbound_to_third_party=True）。在工具编排层
（ToolOrchestrator.execute_tool_calls）单点拦截，无需依赖下层工具实现。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from src.config import ToolsConfig
from src.llm.tool_orchestrator import ToolOrchestrator
from src.models import Message
from src.tools.base import ToolCallResult


def _msg(chat_id: str = "chat_123") -> Message:
    return Message(
        msg_id="m1", chat_id=chat_id, chat_type="group", chat_name="测试群",
        sender_id="u1", sender_name="张三", content="帮我联系下王强买正版",
        msg_type="text", timestamp=datetime.now(),
    )


def _tc(name, args):
    return {"id": f"call_{name}", "name": name, "args": args}


def _make_orchestrator(block: bool, real_sends: list | None = None):
    """构造 ToolOrchestrator：agent.config 为 LlmConfig（无 tools），
    真正的 ToolsConfig 挂在 agent.tool_router.config 上（与运行期一致）。
    real_sends 收集被放行（未拦截）的工具调用参数。"""
    real_sends = real_sends if real_sends is not None else []

    def fake_execute(name, args, session_key=None):
        real_sends.append(args)
        return ToolCallResult(tool_name=name, args=args, success=True, result="ok")

    cfg = ToolsConfig()
    cfg.block_outbound_to_third_party = block
    fake_router = SimpleNamespace(
        config=cfg,
        execute=fake_execute,
        get_available_tool_names=lambda: ["send_message", "send_ding"],
        filter_schemas_by_names=lambda names: [],
        _tools={},
    )
    agent = SimpleNamespace(config=SimpleNamespace(tools=None), tool_router=fake_router)
    return ToolOrchestrator(agent), real_sends


def test_block_on_send_ding_is_intercepted():
    orch, sends = _make_orchestrator(block=True)
    results, _ = orch.execute_tool_calls(
        [_tc("send_ding", {"users": "u999", "content": "买正版Keil"})], _msg())
    assert len(sends) == 0, "send_ding 应被拦截，不真正外联"
    assert results[0]["content"].find('"success": false') != -1
    assert "禁止主动外联" in results[0]["content"]


def test_block_on_send_message_to_other_chat_is_intercepted():
    orch, sends = _make_orchestrator(block=True)
    results, _ = orch.execute_tool_calls(
        [_tc("send_message", {"chat_id": "chat_999", "chat_type": "single", "text": "私下通知"})],
        _msg(chat_id="chat_123"))
    assert len(sends) == 0, "发往其他会话的 send_message 应被拦截"
    assert "不是当前会话" in results[0]["content"]


def test_block_on_send_message_to_current_chat_allowed():
    orch, sends = _make_orchestrator(block=True)
    orch.execute_tool_calls(
        [_tc("send_message", {"chat_id": "chat_123", "chat_type": "group", "text": "通知大家"})],
        _msg(chat_id="chat_123"))
    assert len(sends) == 1, "发往当前会话的 send_message 应放行"


def test_block_off_allows_third_party():
    orch, sends = _make_orchestrator(block=False)
    orch.execute_tool_calls(
        [_tc("send_ding", {"users": "u999", "content": "买正版Keil"})], _msg())
    orch.execute_tool_calls(
        [_tc("send_message", {"chat_id": "chat_999", "chat_type": "single", "text": "私下通知"})],
        _msg(chat_id="chat_123"))
    assert len(sends) == 2, "关闭护栏后 send_ding 与跨会话 send_message 都应放行"


def test_placeholder_chat_id_is_blocked():
    """模型 hallucinate 占位符 chat_id 时也应拦截，避免发给虚假会话。"""
    orch, sends = _make_orchestrator(block=True)
    results, _ = orch.execute_tool_calls(
        [_tc("send_message", {"chat_id": "oc_xxx123456789", "chat_type": "single", "text": "通知"})],
        _msg(chat_id="chat_123"))
    assert len(sends) == 0, "占位符 chat_id 不是当前会话，应被拦截"
    assert "不是当前会话" in results[0]["content"]


def _orch_with_result(success: bool, result, error: str):
    """构造一个返回指定 ToolCallResult 的编排器（模拟工具成功/失败）。"""
    import json as _json

    def fake_execute(name, args, session_key=None):
        return ToolCallResult(tool_name=name, args=args,
                              success=success, result=result, error=error)

    cfg = ToolsConfig()
    fake_router = SimpleNamespace(
        config=cfg, execute=fake_execute,
        get_available_tool_names=lambda: [name for name in ("failing_tool", "confirm_tool")],
        filter_schemas_by_names=lambda names: [], _tools={},
    )
    agent = SimpleNamespace(config=SimpleNamespace(tools=None), tool_router=fake_router)
    return ToolOrchestrator(agent), _json


def test_failed_tool_result_none_does_not_crash_orchestrator():
    """回归：工具失败（result 显式 None，与编排器第77行一致）时，confirm_required 分支
    不得对 None 调 .get 崩溃；结果后处理（注入 _tool/_ts 并重序列化）必须完整执行。
    修复前会 swallowed exception 并跳过 line135 重序列化，导致 content 缺 _tool 字段。"""
    orch, _json = _orch_with_result(success=False, result=None, error="boom")
    results, _ = orch.execute_tool_calls([_tc("failing_tool", {})], _msg())
    assert len(results) == 1
    content = _json.loads(results[0]["content"])
    assert content["_tool"] == "failing_tool", "后处理须完整跑完，_tool 必须注入"
    assert content["error"] == "boom"
    assert content["result"] is None


def test_confirm_required_instruction_injected_after_refactor():
    """回归：confirm_required 工具的 _instruction 注入在 None 防护重构后仍然生效。"""
    orch, _json = _orch_with_result(
        success=True, result={"status": "confirm_required", "confirm_token": "TOK123"}, error="")
    results, _ = orch.execute_tool_calls([_tc("confirm_tool", {})], _msg())
    content = _json.loads(results[0]["content"])
    assert "confirm_token=\"TOK123\"" in content["_instruction"]
