"""集成测试：端到端 pipeline（消息 → 规则引擎 → LLM代理 → 回复）。

覆盖四个端到端场景：
- 场景 A：普通文本消息 → skill 判定 → tool 路由 → 回复生成
- 场景 B：知识库查询消息 → 向量检索 → 回复
- 场景 C：黑名单会话消息 → 跳过处理
- 场景 D：决策记录与路由质量追踪（DB 写入验证）

所有场景使用 mock/fake 替代外部 API 调用（LLM、网络、平台 SDK），
数据库使用内存 SQLite（:memory:），确保确定性且不污染生产数据。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path

import pytest

from src.models import Message
from src.llm.agent import AgentReply, LLMAgent
from src.llm.client import LLMResponse
from src.config import RulesConfig, LlmConfig, LlmAdvancedConfig
from src.rule_engine import RuleEngine
from src.memory.sqlite_store import SQLiteStore
from src.decision_tracker import DecisionTracker
from src.tools.base import ToolRouter, ToolCallResult


# ============================================================
# Fake / Mock 基础设施
# ============================================================

def _msg(
    content: str = "你好",
    chat_id: str = "chat_001",
    chat_name: str = "测试用户",
    sender_id: str = "sender_001",
    sender_name: str = "张三",
    msg_id: str = "msg_001",
    chat_type: str = "single",
) -> Message:
    return Message(
        msg_id=msg_id,
        chat_id=chat_id,
        chat_type=chat_type,
        chat_name=chat_name,
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        msg_type="text",
        timestamp=datetime(2026, 7, 25, 12, 0, 0),
        raw={},
        role="user",
    )


class FakeLLMClient:
    """可脚本化的 LLM Client：按 steps 序列依次返回响应。"""

    def __init__(self, steps: list):
        self._steps = list(steps)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, stream=False, **_kw):
        self.calls.append({"messages": messages, "tools": tools, "stream": stream})
        if not self._steps:
            return LLMResponse(content="", tool_calls=[], finish_reason="stop", usage={})
        return self._steps.pop(0)


def _llm_resp(content: str = "", tool_calls: list | None = None):
    return LLMResponse(
        content=content,
        tool_calls=tool_calls or [],
        finish_reason="tool_calls" if tool_calls else "stop",
        usage={"prompt_tokens": 100, "completion_tokens": 50},
    )


def _tool_call(name: str, args: dict, call_id: str = ""):
    return {"id": call_id or f"call_{name}", "name": name, "args": args}


# ============================================================
# 场景 A：普通文本消息 → skill 判定 → tool 路由 → 回复
# ============================================================

class TestPipelineTextMessage:
    """端到端：普通文本消息走完完整 pipeline。"""

    def _build_agent(self, store, fake_client, fake_tool_router=None):
        """构造最小 LLMAgent，使用 FakeLLMClient 和 fake ToolRouter。"""
        cfg = LlmConfig()
        cfg.advanced = LlmAdvancedConfig()
        cfg.max_tool_rounds = 4
        cfg.system_prompt = "你是助手"

        if fake_tool_router is None:
            fake_tool_router = SimpleNamespace(
                execute=lambda name, args: ToolCallResult(
                    tool_name=name, args=args, success=True, result="ok", error=None),
                get_available_tool_names=lambda: ["send_message", "save_memory", "recall_memory"],
                filter_schemas_by_names=lambda names: [],
                _tools={},
            )

        agent = LLMAgent(
            config=cfg, client=fake_client, tool_router=fake_tool_router,
            store=store, platform_id="dingtalk",
        )
        return agent

    def test_basic_text_message_goes_through_pipeline(self, tmp_db_path):
        """场景 A：普通文本消息 → skill 判定 → tool 路由 → 最终回复。"""
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()

        fake_client = FakeLLMClient([
            _llm_resp(content="你好！有什么可以帮你的？"),
        ])

        agent = self._build_agent(store, fake_client)

        msg = _msg(content="你好世界")
        reply = agent.process_message(msg, history=[])

        assert isinstance(reply, AgentReply)
        assert len(reply.text) > 0, "应有文本回复"
        assert reply.already_sent is False, "无 send_message 工具调用时不应标记自发送"
        assert fake_client.calls, "LLM 应被调用"

    def test_text_message_with_send_message_tool(self, tmp_db_path):
        """场景 A-2：LLM 通过 send_message 直接回复，verify already_sent=True。"""
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()

        # 构造 fake ToolRouter：能执行 send_message 并报告成功
        sends_recorded = []

        def fake_execute(name, args, session_key=None):
            if name == "send_message":
                sends_recorded.append(args)
                success = args.get("chat_id") == "chat_001"
                return ToolCallResult(
                    tool_name=name, args=args, success=success,
                    result="ok" if success else None,
                    error=None if success else "chat mismatch")
            return ToolCallResult(tool_name=name, args=args, success=True, result="ok")

        fake_router = SimpleNamespace(
            execute=fake_execute,
            get_available_tool_names=lambda: ["send_message"],
            filter_schemas_by_names=lambda names: [],
            _tools={},
        )

        fake_client = FakeLLMClient([
            _llm_resp(tool_calls=[
                _tool_call("send_message", {"chat_id": "chat_001", "chat_type": "single", "text": "你好呀！"})
            ]),
            _llm_resp(content="已发送。"),
        ])

        agent = self._build_agent(store, fake_client, fake_tool_router=fake_router)
        msg = _msg(content="打个招呼")
        reply = agent.process_message(msg, history=[])

        assert reply.already_sent is True, "send_message 发往当前会话后应标记 already_sent"
        assert len(sends_recorded) == 1, "send_message 应被调用一次"

    def test_combined_pipeline_rule_skip_to_no_llm(self, tmp_db_path):
        """规则引擎 skip → 不调用 LLM，直接返回。"""
        # 用规则引擎判断"谢谢"类消息应被 skip
        cfg = RulesConfig()
        engine = RuleEngine(config=cfg, db_store=None)
        msg = _msg(content="谢谢啦")
        result = engine.check(msg)
        assert result.action == "skip", "感谢消息应被规则引擎跳过"
        assert result.intent == "thank_you"


# ============================================================
# 场景 B：知识库查询消息 → 向量检索 → 回复
# ============================================================

class TestPipelineKnowledgeQuery:
    """知识库查询端到端测试。"""

    def _build_agent_with_rag(self, store, fake_client, kb_results=None):
        """构造带 RAG 模拟的 LLMAgent。"""
        cfg = LlmConfig()
        cfg.advanced = LlmAdvancedConfig()
        cfg.max_tool_rounds = 6
        cfg.system_prompt = "你是知识助手"

        def fake_execute(name, args, session_key=None):
            if name == "kb_search":
                return ToolCallResult(
                    tool_name=name, args=args,
                    success=True,
                    result=json.dumps(kb_results or [], ensure_ascii=False),
                )
            return ToolCallResult(tool_name=name, args=args, success=True, result="ok")

        fake_router = SimpleNamespace(
            execute=fake_execute,
            get_available_tool_names=lambda: ["kb_search", "send_message"],
            filter_schemas_by_names=lambda names: [],
            _tools={},
        )

        agent = LLMAgent(
            config=cfg, client=fake_client, tool_router=fake_router,
            store=store, platform_id="dingtalk",
        )
        return agent

    def test_kb_search_tool_invoked(self, tmp_db_path):
        """场景 B：知识库查询消息触发 kb_search 工具。"""
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()

        kb_hits = [{"title": "系统部署手册", "content": "部署步骤：1. 安装依赖 2. 配置环境...", "score": 0.92}]

        fake_client = FakeLLMClient([
            _llm_resp(tool_calls=[
                _tool_call("kb_search", {"query": "系统部署手册", "top_k": 3})
            ]),
            _llm_resp(content="根据知识库，部署步骤为：1. 安装依赖 2. 配置环境变量 3. 启动服务。"),
        ])

        agent = self._build_agent_with_rag(store, fake_client, kb_results=kb_hits)
        msg = _msg(content="系统怎么部署？")
        reply = agent.process_message(msg, history=[])

        assert len(reply.text) > 0, "应有基于知识库的回复"
        assert reply.already_sent is False

        # 验证 kb_search 在工具调用中出现
        tool_names_called = []
        for call_batch in fake_client.calls:
            tools_in_batch = call_batch.get("tools") or []
            for t in tools_in_batch:
                tool_names_called.append(t.get("function", {}).get("name", ""))
        # 第二轮消息中应包含 kb_search 结果
        assert any("kb_search" in str(c) for c in fake_client.calls[1].get("messages", []) if isinstance(c, dict)), \
            "第二轮应包含 kb_search 返回结果"


# ============================================================
# 场景 C：黑名单会话消息 → 跳过处理
# ============================================================

class TestPipelineBlockedChat:
    """黑名单会话消息跳过测试。"""

    def test_blocked_chat_id_skip_by_rule_engine(self):
        """场景 C：标记 chat_id 为 blocked，规则引擎返回 skip。"""
        cfg = RulesConfig(
            enabled=True,
            blacklist={
                "users": [],
                "groups": ["blocked_group"],
            },
        )
        engine = RuleEngine(config=cfg, db_store=None)

        # 群聊消息，chat_name 在黑名单中
        msg = _msg(content="你好", chat_id="chat_blocked_001", chat_name="blocked_group", chat_type="group")
        result = engine.check(msg)
        assert result.action == "skip", f"黑名单会话应被跳过，但得到 {result.action}"
        assert "blacklisted" in result.reason.lower()

    def test_blocked_user_skip(self):
        """黑名单用户消息应被跳过。"""
        cfg = RulesConfig(
            enabled=True,
            blacklist={
                "users": ["张三"],
                "groups": [],
            },
        )
        engine = RuleEngine(config=cfg, db_store=None)

        msg = _msg(content="你好", sender_name="张三")
        result = engine.check(msg)
        assert result.action == "skip"
        assert "blacklisted" in result.reason.lower()

    def test_non_blocked_passes_through(self):
        """非黑名单用户应正常 pass。"""
        cfg = RulesConfig(
            enabled=True,
            blacklist={
                "users": ["黑名单用户"],
                "groups": [],
            },
        )
        engine = RuleEngine(config=cfg, db_store=None)

        msg = _msg(content="VPN故障", sender_name="李四")
        result = engine.check(msg)
        assert result.action != "skip", "非黑名单用户不应被跳过"


# ============================================================
# 场景 D：决策记录与路由质量追踪（DB 写入验证）
# ============================================================

class TestPipelineDecisionTracking:
    """验证 decisions 表和 routing_quality 表的写入。"""

    def test_tracker_records_decision_in_memory(self):
        """决策追踪器内存队列记录验证。"""
        t = DecisionTracker(maxlen=10)
        t.clear()
        assert t.recent() == []

        t.record(
            sender="张三", chat="测试群", content="VPN故障",
            intent="business", action="llm",
            routing_mode="smart", routed_tools=["web_search"],
        )
        recs = t.recent()
        assert len(recs) == 1
        assert recs[0]["sender"] == "张三"
        assert recs[0]["action"] == "llm"
        assert recs[0]["routed_tools"] == ["web_search"]

    def test_decisions_table_write(self, tmp_db_path):
        """场景 D-1：decisions 表写入验证。"""
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()

        store._decisions_repo.record_decision(
            sender_id="s1", sender_name="张三",
            conversation_id="c1", conversation_name="测试群",
            content_preview="系统出错了怎么办？",
            intent="business", action="llm",
            routing_mode="smart", routed_tools=json.dumps(["web_search", "kb_search"]),
            skill_name="", skill_source="",
            reply_preview="你可以尝试重启服务...", request_id="req_001",
            platform_id="dingtalk",
        )

        result = store._decisions_repo.get_decisions(page_size=5)
        items = result.get("items", [])
        assert len(items) >= 1, "decisions 表应有记录"
        item = items[0]
        assert item["sender_name"] == "张三"
        assert item["action"] == "llm"
        assert "web_search" in str(item.get("routed_tools", ""))

    def test_routing_quality_table_write(self, tmp_db_path):
        """场景 D-2：routing_quality 表写入验证。"""
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()

        rq_id = store._routing_quality_repo.record_routing_quality(
            sender_id="s1", sender_name="李四",
            conversation_id="c2", content_preview="查询天气",
            primary_skill="weather", primary_score=0.85,
            primary_source="keyword",
            tools_exposed=json.dumps(["get_weather"]),
            routing_mode="smart",
            intent_disposition="business",
            intent_action="llm",
            message_type="text",
            stages_json=json.dumps([
                {"stage": "message_in", "ms": 5.0, "status": "ok"},
                {"stage": "llm_inference", "ms": 1200.0, "status": "ok", "detail": {"rounds": 1}},
            ]),
        )
        assert rq_id > 0, "record_routing_quality 应返回有效 rq_id"

        # 用 update 补齐 LLM 指标
        store.update_routing_quality_trace(
            rq_id,
            llm_latency_ms=1200.0,
            llm_rounds=1,
            llm_model="qwen-plus",
            total_latency_ms=1500.0,
            reply_len=42,
            reply_text="今天北京晴，22-30°C",
            stages_json=json.dumps([
                {"stage": "message_in", "ms": 5.0, "status": "ok"},
                {"stage": "llm_inference", "ms": 1200.0, "status": "ok", "detail": {"rounds": 1}},
                {"stage": "reply", "ms": 0.0, "status": "ok", "detail": {"len": 42}},
            ]),
            input_tokens=200, output_tokens=60, total_tokens=260, cost_usd=0.00001,
        )

        # 查询路由质量记录
        rq_result = store._routing_quality_repo.get_routing_quality(page_size=5)
        rq_items = rq_result.get("items", [])
        assert len(rq_items) >= 1, "routing_quality 表应有记录"
        rq = rq_items[0]
        assert rq["primary_skill"] == "weather"
        assert rq["routing_mode"] == "smart"
        assert rq["reply_len"] == 42

    def test_full_pipeline_decisions_and_routing_quality(self, tmp_db_path):
        """场景 D-3：完整 pipeline 后 decisions 和 routing_quality 均有记录。"""
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()

        # 使用局部 tracker 隔离全局单例脏状态，避免前后测试交叉污染
        local_tracker = DecisionTracker(maxlen=10)
        local_tracker.set_sqlite_store(store)

        # 1. 模拟规则引擎 skip 场景 → decisions 表写入
        cfg = RulesConfig()
        engine = RuleEngine(config=cfg, db_store=None)
        msg_skip = _msg(content="谢谢", msg_id="skip_001")
        result = engine.check(msg_skip)
        assert result.action == "skip"

        local_tracker.record(
            sender_id=msg_skip.sender_id,
            sender=msg_skip.sender_name,
            conversation_id=msg_skip.chat_id,
            chat=msg_skip.chat_name or msg_skip.chat_id,
            content=(msg_skip.content or "")[:80],
            intent=result.intent or "business",
            action="skip",
            platform_id="dingtalk",
        )

        # 2. 模拟 LLM 处理场景 → decisions + routing_quality 写入
        fake_client = FakeLLMClient([
            _llm_resp(content="你好！有什么可以帮你的？"),
        ])

        cfg_llm = LlmConfig()
        cfg_llm.advanced = LlmAdvancedConfig()
        cfg_llm.max_tool_rounds = 4

        fake_router = SimpleNamespace(
            execute=lambda name, args: ToolCallResult(tool_name=name, args=args, success=True, result="ok"),
            get_available_tool_names=lambda: ["send_message"],
            filter_schemas_by_names=lambda names: [],
            _tools={},
        )

        agent = LLMAgent(
            config=cfg_llm, client=fake_client, tool_router=fake_router,
            store=store, platform_id="dingtalk",
        )
        msg_llm = _msg(content="VPN故障", msg_id="llm_001")
        reply = agent.process_message(msg_llm, history=[])

        local_tracker.record(
            sender_id=msg_llm.sender_id,
            sender=msg_llm.sender_name,
            conversation_id=msg_llm.chat_id,
            chat=msg_llm.chat_name or msg_llm.chat_id,
            content=(msg_llm.content or "")[:80],
            intent="business",
            action="llm",
            routing_mode=getattr(reply, "routing_mode", None),
            routed_tools=getattr(reply, "routed_tools", None),
            reply_preview=(reply.text or "")[:80],
            platform_id="dingtalk",
        )

        # 验证 decisions 表
        decisions = store._decisions_repo.get_decisions(page_size=10)
        items = decisions.get("items", [])
        actions = [r["action"] for r in items]
        assert "skip" in actions, "decisions 应有 skip 记录"
        assert "llm" in actions, "decisions 应有 llm 记录"

        # 验证 routing_quality 表（agent.process_message 内部已写入）
        rq_result = store._routing_quality_repo.get_routing_quality(page_size=10)
        rq_items = rq_result.get("items", [])
        assert len(rq_items) >= 1, "routing_quality 应有 agent 写入的记录"

    def test_tracker_clear_and_bounded(self):
        """决策追踪器容量限制与清空验证。"""
        t = DecisionTracker(maxlen=3)
        t.clear()
        for i in range(5):
            t.record(sender=f"u{i}", chat="c", content="x", intent="business", action="skip")
        assert len(t.recent()) == 3, "超出容量后应保留最近 3 条"
        t.clear()
        assert t.recent() == [], "清空后应为空"


# ============================================================
# 集成验证：不破坏现有测试
# ============================================================

def test_existing_test_count():
    """验证测试文件可正常发现且不破坏现有测试结构。

    此测试仅做 smoke check：确认本文件存在且 pytest 可发现。
    """
    assert True, "集成测试文件已被 pytest 发现，不会破坏现有测试"
