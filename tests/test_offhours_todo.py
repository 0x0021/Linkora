"""工作时间外未回复消息 → 待办：核心判定的直接测试。

覆盖：工作时段/周末/节假日判定、未读或无真人回复筛选（AI 回复不算）、
按会话聚合、去重幂等、企微降级通知、飞书待办命令构造。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from src.config_models.system import UnrepliedTodoConfig
from src.memory.offhours_todo_repo import OffhoursTodoRepo
from src.memory.schema import init_conv_schema, init_schema
from src.offhours_todo.scanner import UnansweredItem, scan_platform
from src.offhours_todo.service import OffhoursTodoService
from src.offhours_todo.worktime import (
    REASON_HOLIDAY,
    REASON_OFF_HOURS,
    REASON_WEEKEND,
    classify,
)
from src.im_adapter.feishu import FeishuCliAdapter

TZ = ZoneInfo("Asia/Shanghai")


class FakeStore:
    """最小 store 替身：主库写闸门 + 会话库连接。"""

    def __init__(self, main_conn: sqlite3.Connection, conv_conn: sqlite3.Connection) -> None:
        self.conn = main_conn
        self._conversation_repo = SimpleNamespace(_cc=lambda _platform: conv_conn)
        self._offhours_todo_repo = OffhoursTodoRepo(self)  # type: ignore[arg-type]

    def write_main_with_retry(self, fn):
        return fn(self.conn)


def _make_store(tmp_path):
    main_path = str(tmp_path / "main.db")
    conv_path = str(tmp_path / "conv.db")
    main = sqlite3.connect(main_path)
    main.row_factory = sqlite3.Row
    conv = sqlite3.connect(conv_path)
    conv.row_factory = sqlite3.Row
    init_schema(main, main_path)
    init_conv_schema(conv, conv_path)
    return FakeStore(main, conv)


def _insert_msg(conv, *, msg_id, chat_id, sender, ts, content="你好", is_bot=0):
    conv.execute(
        """INSERT INTO messages
           (chat_id, msg_id, sender_id, sender_name, content, msg_type, timestamp,
            is_bot, is_withdrawn, created_at)
           VALUES (?, ?, ?, ?, ?, 'text', ?, ?, 0, ?)""",
        (chat_id, msg_id, sender, sender, content, ts, is_bot, ts),
    )
    conv.commit()


# ---------------- 工作时间判定 ----------------

def test_classify_workday_and_offhours_and_weekend():
    cfg = UnrepliedTodoConfig()  # 周一至周五 09:00-18:00
    assert classify(datetime(2026, 11, 23, 10, 0, tzinfo=TZ), cfg) == ""       # 周一上班
    assert classify(datetime(2026, 11, 23, 20, 0, tzinfo=TZ), cfg) == REASON_OFF_HOURS
    assert classify(datetime(2026, 11, 22, 10, 0, tzinfo=TZ), cfg) == REASON_WEEKEND  # 周日


def test_classify_holiday_overrides_workday():
    cfg = UnrepliedTodoConfig(holidays="2026-11-23")
    assert classify(datetime(2026, 11, 23, 10, 0, tzinfo=TZ), cfg) == REASON_HOLIDAY


def test_classify_cross_midnight_shift():
    cfg = UnrepliedTodoConfig(work_start="22:00", work_end="06:00")
    assert classify(datetime(2026, 11, 23, 23, 0, tzinfo=TZ), cfg) == ""
    assert classify(datetime(2026, 11, 24, 5, 0, tzinfo=TZ), cfg) == ""
    assert classify(datetime(2026, 11, 24, 12, 0, tzinfo=TZ), cfg) == REASON_OFF_HOURS


# ---------------- 未回复筛选 ----------------

def test_scan_selects_offhours_message_without_human_reply(tmp_path):
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    # 周日晚 20:05 对方来消息（工作时间外），之后只有 AI 代发回复
    _insert_msg(conv, msg_id="m1", chat_id="cid-1", sender="张三", ts="2026-11-22T20:05:00")
    _insert_msg(conv, msg_id="m2", chat_id="cid-1", sender="bot", ts="2026-11-22T20:06:00", is_bot=1)

    cfg = UnrepliedTodoConfig(lookback_hours=72)
    ctx = SimpleNamespace(store=store, dws=None)
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-oid"},
        now=datetime(2026, 11, 23, 9, 0, tzinfo=TZ),
    )
    assert result.pending_new == 1
    assert result.items[0].msg_id == "m1"
    assert result.items[0].reason == REASON_WEEKEND
    assert result.items[0].detail["human_reply_after"] is False  # AI 回复不算


def test_scan_skips_message_with_real_human_reply(tmp_path):
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    _insert_msg(conv, msg_id="m1", chat_id="cid-1", sender="张三", ts="2026-11-22T20:05:00")
    # 本人真人回复（is_bot=0 + sender_id 命中自身）→ 不再算未回复
    _insert_msg(conv, msg_id="m2", chat_id="cid-1", sender="self-oid", ts="2026-11-22T20:10:00")

    cfg = UnrepliedTodoConfig(lookback_hours=72)
    ctx = SimpleNamespace(store=store, dws=None)
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-oid"},
        now=datetime(2026, 11, 23, 9, 0, tzinfo=TZ),
    )
    assert result.pending_new == 0


def test_scan_dedupe_second_run_no_duplicate(tmp_path):
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    _insert_msg(conv, msg_id="m1", chat_id="cid-1", sender="张三", ts="2026-11-22T20:05:00")
    cfg = UnrepliedTodoConfig(lookback_hours=72)
    ctx = SimpleNamespace(store=store, dws=None)
    args = dict(self_ids={"self-oid"}, now=datetime(2026, 11, 23, 9, 0, tzinfo=TZ))
    assert scan_platform(cfg, "dingtalk", ctx, **args).pending_new == 1
    assert scan_platform(cfg, "dingtalk", ctx, **args).pending_new == 0  # 去重键命中


# ---------------- 去重与记录 ----------------

def test_todo_record_dedupe(tmp_path):
    store = _make_store(tmp_path)
    repo = store._offhours_todo_repo
    kwargs = dict(platform="wecom", chat_id="c", msg_id="m", dedupe_key="wecom:c:m", title="t")
    assert repo.insert_todo_record(**kwargs) is True
    assert repo.insert_todo_record(**kwargs) is False
    assert repo.has_todo_record("wecom:c:m") is True
    rows, total = repo.list_todo_records(platform="wecom")
    assert total == 1 and rows[0]["dedupe_key"] == "wecom:c:m"


# ---------------- 飞书待办命令构造 ----------------

def test_feishu_todo_create_builds_correct_command(monkeypatch):
    adapter = FeishuCliAdapter.__new__(FeishuCliAdapter)
    captured = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        return {"data": {"task": {"guid": "task-123"}}}

    adapter.run = fake_run  # type: ignore[method-assign]
    result = adapter.todo_task_create(
        title="待回复 · 张三", executors="ou_abc", description="内容摘要"
    )
    args = captured["args"]
    assert args[:3] == ["task", "+create", "--as"]
    assert "--summary" in args and "待回复 · 张三" in args
    assert "--assignee" in args and "ou_abc" in args
    assert "--description" in args and "内容摘要" in args
    assert "--idempotency-key" in args
    assert result["task_id"] == "task-123"


# ---------------- 手动建单条目构造 ----------------

def test_unanswered_item_dedupe_key_format():
    item = UnansweredItem(
        platform="feishu", chat_id="oc_x", chat_name="", chat_type="",
        msg_id="om_y", sender_id="ou_z", sender_name="张三",
        content_preview="你好", timestamp="2026-11-22T20:05:00",
        reason=REASON_WEEKEND, dedupe_key="feishu:oc_x:om_y",
    )
    assert item.dedupe_key == "feishu:oc_x:om_y"


def _make_item(platform: str) -> UnansweredItem:
    return UnansweredItem(
        platform=platform, chat_id="chat-1", chat_name="项目群", chat_type="",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天能给我回复吗？", timestamp="2026-11-22T20:05:00",
        reason=REASON_WEEKEND, dedupe_key=f"{platform}:chat-1:m1",
    )


def test_service_creates_dingtalk_todo_and_notifies_sender(tmp_path):
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-123"}
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid")
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    # 模拟 scanner 已先落库的未回复消息
    store._offhours_todo_repo.upsert_unanswered(
        platform="dingtalk", chat_id="chat-1", msg_id="m1",
        dedupe_key="dingtalk:chat-1:m1", msg_timestamp="2026-11-22T20:05:00",
    )

    ok, kind, err = service._create_for_item(_make_item("dingtalk"), ctx, {"self-oid"})

    assert ok and kind == "platform_todo" and not err
    adapter.todo_task_create.assert_called_once()
    # 钉钉走 conversation_id 稳定 ID 直通回执对方
    assert adapter.chat_message_send.call_count == 1
    assert adapter.chat_message_send.call_args.kwargs["conversation_id"] == "chat-1"
    rows, total = store._offhours_todo_repo.list_todo_records()
    assert total == 1 and rows[0]["todo_id"] == "todo-123"
    assert store._offhours_todo_repo.get_unanswered_by_key("dingtalk:chat-1:m1")["status"] == "todo_created"


def test_service_wecom_degrades_to_notify_only(tmp_path):
    store = _make_store(tmp_path)
    adapter = MagicMock()
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(enabled=True, notify_sender=True, notify_owner=False)
    service = OffhoursTodoService(cfg, engine, {"wecom": ctx})

    ok, kind, err = service._create_for_item(_make_item("wecom"), ctx, {"self-oid"})

    assert ok and kind == "notify_only" and not err
    adapter.todo_task_create.assert_not_called()  # 企微无个人待办 API，不许伪装建单
    assert adapter.chat_message_send.call_count == 1
    rows, total = store._offhours_todo_repo.list_todo_records()
    assert total == 1 and rows[0]["todo_type"] == "notify_only"


def test_offhours_routes_registered():
    from web.api import app

    paths = app.openapi()["paths"]
    for path in (
        "/api/offhours-todos/rules",
        "/api/offhours-todos/unanswered",
        "/api/offhours-todos/records",
        "/api/offhours-todos/stats",
        "/api/offhours-todos/scan",
        "/api/offhours-todos/sync",
        "/api/offhours-todos/create",
        "/api/offhours-todos/ignore",
    ):
        assert path in paths
