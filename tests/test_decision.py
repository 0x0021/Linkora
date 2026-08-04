"""决策追踪与意图/路由可观测性的单元测试。

覆盖：
- DecisionTracker（有界队列、record/recent/clear）
- RuleResult.intent 字段在 check() 中被正确回填（social 子型 / business）
- AgentReply 携带 routing_mode / routed_tools
- LLMAgent._resolve_routing_mode 三种模式 + 旧开关兼容
"""

import asyncio
from types import SimpleNamespace

from src.decision_tracker import DecisionTracker
from src.llm.agent import AgentReply, LLMAgent
from src.models import Message
from src.config import RulesConfig
from src.rule_engine import RuleEngine
from datetime import datetime


def test_tracker_record_and_recent():
    t = DecisionTracker(maxlen=5)
    assert t.recent() == []
    t.record(sender="张三", chat="群A", content="你好", intent="business", action="llm",
             routing_mode="smart", routed_tools=["web_search"])
    recs = t.recent()
    assert len(recs) == 1
    assert recs[0]["sender"] == "张三"
    assert recs[0]["routed_tools"] == ["web_search"]
    assert "ts" in recs[0]


def test_tracker_bounded():
    t = DecisionTracker(maxlen=3)
    for i in range(5):
        t.record(sender=f"u{i}", chat="c", content="x", intent="business", action="skip")
    assert len(t.recent()) == 3  # 超出上限后被丢弃最旧


def test_tracker_clear():
    t = DecisionTracker()
    t.record(sender="u", chat="c", content="x", intent="business", action="skip")
    t.clear()
    assert t.recent() == []


def _msg(content: str) -> Message:
    return Message(
        msg_id="m1", chat_id="c1", chat_type="single", chat_name="私聊",
        sender_id="s1", sender_name="某人", content=content,
        msg_type="text", timestamp=datetime.now(),
    )


def test_rule_result_intent_social_thankyou():
    re = RuleEngine(RulesConfig(), db_store=None)
    result = re.check(_msg("谢谢啦"))
    assert result.action == "skip"
    assert result.intent == "thank_you"


def test_rule_result_intent_social_polite():
    # “你好” 因含单字“好”曾被判为确认收到；优先级修复后应归为 polite
    re = RuleEngine(RulesConfig(), db_store=None)
    result = re.check(_msg("你好"))
    assert result.action == "skip"
    assert result.intent == "polite"


def test_rule_result_intent_business_pass():
    re = RuleEngine(RulesConfig(), db_store=None)
    result = re.check(_msg("明天深圳天气怎么样"))
    assert result.action == "pass"
    assert result.intent == "business"


def test_agent_reply_routing_fields():
    r = AgentReply(text="hi", routing_mode="smart", routed_tools=["web_search", "send_message"])
    assert r.routing_mode == "smart"
    assert r.routed_tools == ["web_search", "send_message"]
    # 默认值
    r2 = AgentReply(text="x")
    assert r2.routing_mode is None
    assert r2.routed_tools is None


def _make_agent(mode: str) -> LLMAgent:
    cfg = SimpleNamespace()
    if mode == "old_all":
        cfg.expose_all_tools = True
        cfg.model_fields_set = {"expose_all_tools"}
    elif mode == "old_keyword":
        cfg.expose_all_tools = False
        cfg.model_fields_set = {"expose_all_tools"}
    elif mode == "default":
        cfg.model_fields_set = set()  # 两者都没设 → 默认 smart
    else:
        cfg.tool_routing_mode = mode
        cfg.model_fields_set = {"tool_routing_mode"}
    tr = SimpleNamespace(config=cfg)
    return LLMAgent(SimpleNamespace(), SimpleNamespace(), tr)


def test_resolve_routing_mode():
    assert _make_agent("smart")._resolve_routing_mode() == "smart"
    assert _make_agent("all")._resolve_routing_mode() == "all"
    assert _make_agent("keyword")._resolve_routing_mode() == "keyword"
    # 旧开关兼容
    assert _make_agent("old_all")._resolve_routing_mode() == "all"
    assert _make_agent("old_keyword")._resolve_routing_mode() == "keyword"
    # 两者都没设 → 默认 smart
    assert _make_agent("default")._resolve_routing_mode() == "smart"


def test_resolve_routing_mode_invalid_falls_back_to_smart():
    cfg = SimpleNamespace(tool_routing_mode="bogus", model_fields_set={"tool_routing_mode"})
    agent = LLMAgent(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(config=cfg))
    assert agent._resolve_routing_mode() == "smart"


def test_decisions_api_returns_tracker_data():
    """验证 /api/decisions 读取进程内 tracker（通过 monkey-patch 模拟队列内容）。"""
    from web.routers.decisions import recent_decisions
    fake = DecisionTracker()
    fake.record(sender="测", chat="群", content="测试", intent="business", action="llm",
                routing_mode="smart", routed_tools=["web_search"])
    import src.decision_tracker as dt
    original = dt.tracker
    dt.tracker = fake
    try:
        resp = asyncio.run(recent_decisions(n=10))
        assert resp["total"] == 1
        assert resp["decisions"][0]["sender"] == "测"
        assert resp["decisions"][0]["routed_tools"] == ["web_search"]
    finally:
        dt.tracker = original


def test_tracker_with_sqlite_store():
    """record() with sqlite_store set should persist."""
    import tempfile
    from src.memory.sqlite_store import SQLiteStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(f"{tmpdir}/test.db")
        store.init_db()
        t = DecisionTracker()
        t.set_sqlite_store(store)
        t.record(
            sender="test", chat="群A",
            content="你好", intent="business", action="llm",
            routing_mode="smart", routed_tools=["web_search"],
        )
        assert len(t.recent()) == 1

        history = store._decisions_repo.get_decisions(page_size=10)
        assert history["total"] == 1
        assert history["items"][0]["action"] == "llm"


def test_tracker_record_sqlite_exception_not_fatal():
    """If sqlite_store raises, record still works in-memory."""
    t = DecisionTracker()
    broken = type("BadStore", (), {"record_decision": lambda **kw: (_ for _ in ()).throw(RuntimeError("db error"))})()
    t.set_sqlite_store(broken)
    t.record(sender="x", chat="c", content="test", intent="business", action="skip")
    assert len(t.recent()) == 1


def test_sqlite_prune_old_decisions():
    """_prune_decisions 应删除早于 retention_days 的记录，保留近期记录。"""
    import tempfile
    from src.memory.sqlite_store import SQLiteStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(f"{tmpdir}/test.db")
        store.init_db()
        store.set_decisions_retention_days(10)  # 保留最近 10 天
        cur = store.conn.cursor()
        # 插入一条 90 天前的旧记录
        cur.execute(
            "INSERT INTO decisions (sender_id, sender_name, action, created_at) "
            "VALUES (?, ?, ?, datetime('now', '-90 days', 'localtime'))",
            ("old", "老用户", "skip"),
        )
        # 插入一条昨天的近期记录
        cur.execute(
            "INSERT INTO decisions (sender_id, sender_name, action, created_at) "
            "VALUES (?, ?, ?, datetime('now', '-1 days', 'localtime'))",
            ("new", "新用户", "llm"),
        )
        store.conn.commit()

        assert store._decisions_repo.get_decisions(page_size=100)["total"] == 2
        store._prune_decisions()
        after = store._decisions_repo.get_decisions(page_size=100)["items"]
        assert len(after) == 1
        assert after[0]["sender_id"] == "new"  # 旧记录被清理


def test_sqlite_prune_hard_cap():
    """硬上限兜底：超出 hard_cap 时删除最旧的超额记录。"""
    import tempfile
    from src.memory.sqlite_store import SQLiteStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(f"{tmpdir}/test.db")
        store.init_db()
        store.set_decisions_retention_days(0)  # 关闭时间清理，仅测硬上限
        store._decisions_repo._decisions_hard_cap = 50  # 测试用极小上限
        cur = store.conn.cursor()
        for i in range(60):
            cur.execute(
                "INSERT INTO decisions (sender_id, action, created_at) VALUES (?, 'skip', "
                "datetime('now', ? || ' days', 'localtime'))",
                (f"u{i}", -i),  # i 越大越旧
            )
        store.conn.commit()
        assert store._decisions_repo.get_decisions(page_size=100)["total"] == 60

        store._prune_decisions()
        after = store._decisions_repo.get_decisions(page_size=100)["total"]
        assert after == 50, "应被裁剪到 hard_cap"
        # 保留的应是较新的 50 条（u10..u59），最旧的 u0..u9 被删
        senders = {it["sender_id"] for it in store._decisions_repo.get_decisions(page_size=100)["items"]}
        # 删除的是最旧的 10 条（u50..u59），最新的 u0 应保留
        assert "u59" not in senders and "u0" in senders


def test_record_decision_triggers_prune():
    """record_decision 累计 200 次插入后自动触发一次清理（不抛异常）。"""
    import tempfile
    from src.memory.sqlite_store import SQLiteStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(f"{tmpdir}/test.db")
        store.init_db()
        store.set_decisions_retention_days(0)  # 关闭时间清理，仅测硬上限触发路径
        store._decisions_repo._decisions_hard_cap = 50
        for i in range(250):
            store._decisions_repo.record_decision(sender_id=f"u{i}", action="skip")
        # 250 次插入：第 200 次触发 prune（裁剪到 50），其后 50 次不再触发 → 最终 100。
        # 关键断言：表没有无限增长（250），被硬性约束在 [50, 100]。
        total = store._decisions_repo.get_decisions(page_size=1000)["total"]
        assert 50 <= total <= 100, f"硬上限应约束表规模，实际 {total}"


def test_tracker_recent_falls_back_to_sqlite():
    """recent() 内存为空时从 SQLite 恢复历史并回填内存队列。"""
    import tempfile
    from src.memory.sqlite_store import SQLiteStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = SQLiteStore(f"{tmpdir}/test.db")
        store.init_db()

        # 先在 tracker A 写入一条决策并持久化到 SQLite
        t = DecisionTracker()
        t.set_sqlite_store(store)
        t.record(sender="李四", chat="群B", content="测试", intent="business", action="llm",
                 routing_mode="smart", routed_tools=["doc_search"])

        # 新建 tracker B（内存为空），共享同一个 SQLite store
        t2 = DecisionTracker()
        t2.set_sqlite_store(store)
        recs = t2.recent()
        assert len(recs) >= 1
        assert recs[0]["sender"] == "李四"
        assert recs[0]["routed_tools"] == ["doc_search"]

        # 验证回填：t2 内存队列已有数据
        assert len(t2._records) >= 1


def test_tracker_recent_sqlite_fallback_handles_exception():
    """recent() SQLite 回退异常时安全返回空列表。"""
    t = DecisionTracker()
    broken = type("BadStore", (), {
        "get_decisions": lambda **kw: (_ for _ in ()).throw(RuntimeError("db error"))
    })()
    t.set_sqlite_store(broken)
    assert t.recent() == []
