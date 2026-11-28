"""工作时间外未回复消息 → 待办：核心判定的直接测试。

覆盖：工作时段/周末/节假日判定、未读或无真人回复筛选（AI 回复不算）、
按会话聚合、去重幂等、企微降级通知、飞书待办命令构造。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.config_models.system import UnrepliedTodoConfig
from src.memory.offhours_todo_repo import OffhoursTodoRepo
from src.offhours_todo.at_mention import is_at_me
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
        # 回执落库走 _message_repo.save_message（与生产同一条路径），
        # 这里用真实 MessageRepo + 会话库连接，验证 role/is_bot 标记确实落到位。
        self._message_repo = _FakeMessageRepo(conv_conn)

    def write_main_with_retry(self, fn):
        return fn(self.conn)


class _FakeMessageRepo:
    """只实现 save_message 的最小仓储替身（写会话库 messages 表）。"""

    def __init__(self, conv_conn: sqlite3.Connection) -> None:
        self._conn = conv_conn

    def save_message(self, message, role: str = "user", skip_reason: str = "") -> None:
        ts = message.timestamp.isoformat() if message.timestamp else ""
        self._conn.execute(
            """INSERT OR IGNORE INTO messages
               (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type,
                timestamp, role, image_path, is_bot, skip_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.chat_id, message.chat_type or "single", message.msg_id,
                message.sender_id, message.sender_name, message.content,
                message.msg_type or "text", ts, role, message.image_path or "",
                int(message.is_bot), skip_reason or "", ts,
            ),
        )
        self._conn.commit()


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


def _insert_msg(conv, *, msg_id, chat_id, sender, ts, content="你好", is_bot=0,
                chat_type="single"):
    conv.execute(
        """INSERT INTO messages
           (chat_id, msg_id, sender_id, sender_name, content, msg_type, timestamp,
            is_bot, is_withdrawn, created_at, chat_type)
           VALUES (?, ?, ?, ?, ?, 'text', ?, ?, 0, ?, ?)""",
        (chat_id, msg_id, sender, sender, content, ts, is_bot, ts, chat_type),
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


# ---------------- 群消息 @ 门控（2026-11-26 误建事故） ----------------

def test_at_mention_only_explicit_self_or_all():
    ids = {"DDn1self", "3410690324258637"}
    names = {"主人", "小主"}
    # 线上误建那条：@ 的是别人，不是我
    assert is_at_me("传递涂油脂么？这个咋用图纸传递？@同事甲 ", self_ids=ids, self_names=names) is False
    assert is_at_me("@同事甲 这个你看看", self_ids=ids, self_names=names) is False
    # 正式姓名 / 昵称 / @ID / @所有人 都算
    assert is_at_me("@主人 明天帮看下", self_ids=ids, self_names=names) is True
    assert is_at_me("@小主 在吗", self_ids=ids, self_names=names) is True
    assert is_at_me("@DDn1self 在吗", self_ids=ids, self_names=names) is True
    assert is_at_me("@all 大家看下", self_ids=ids, self_names=names) is True
    assert is_at_me("@所有人 大家看下", self_ids=ids, self_names=names) is True
    # 不带 @ 只提到名字不算（群聊里讨论某人 ≠ 叫某人）
    assert is_at_me("主人 明天帮看下", self_ids=ids, self_names=names) is False
    assert is_at_me("", self_ids=ids, self_names=names) is False


def test_scan_skips_group_message_not_at_me(tmp_path):
    """群里 @ 别人的消息不建单（工业线 ECN 群误建事故的直接回归）。"""
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    _insert_msg(
        conv, msg_id="g1", chat_id="cid-group", sender="李四",
        ts="2026-11-25T19:36:12", chat_type="group",
        content="传递涂油脂么？这个咋用图纸传递？@同事甲 ",
    )
    cfg = UnrepliedTodoConfig(lookback_hours=72)
    ctx = SimpleNamespace(store=store, dws=None)
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-oid"}, self_names={"主人", "小主"},
        now=datetime(2026, 11, 26, 9, 0, tzinfo=TZ),
    )
    assert result.pending_new == 0


def test_scan_keeps_group_message_at_me(tmp_path):
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    _insert_msg(
        conv, msg_id="g2", chat_id="cid-group", sender="李四",
        ts="2026-11-25T19:36:12", chat_type="group",
        content="@小主 这个咋用图纸传递？",
    )
    cfg = UnrepliedTodoConfig(lookback_hours=72)
    ctx = SimpleNamespace(store=store, dws=None)
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-oid"}, self_names={"主人", "小主"},
        now=datetime(2026, 11, 26, 9, 0, tzinfo=TZ),
    )
    assert result.pending_new == 1
    assert result.items[0].msg_id == "g2"


def test_scan_single_chat_not_gated_by_at(tmp_path):
    """单聊本就是对我说的，不需要 @。"""
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    _insert_msg(conv, msg_id="s1", chat_id="cid-single", sender="张三",
                ts="2026-11-25T19:36:12", content="这个咋用图纸传递？")
    cfg = UnrepliedTodoConfig(lookback_hours=72)
    ctx = SimpleNamespace(store=store, dws=None)
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-oid"}, self_names={"主人"},
        now=datetime(2026, 11, 26, 9, 0, tzinfo=TZ),
    )
    assert result.pending_new == 1


def test_scan_group_at_gate_can_be_disabled(tmp_path):
    store = _make_store(tmp_path)
    conv = store._conversation_repo._cc("dingtalk")
    _insert_msg(
        conv, msg_id="g3", chat_id="cid-group", sender="李四",
        ts="2026-11-25T19:36:12", chat_type="group",
        content="@同事甲 这个咋用图纸传递？",
    )
    cfg = UnrepliedTodoConfig(lookback_hours=72, group_require_at_me=False)
    ctx = SimpleNamespace(store=store, dws=None)
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-oid"}, self_names={"主人"},
        now=datetime(2026, 11, 26, 9, 0, tzinfo=TZ),
    )
    assert result.pending_new == 1


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


def _make_item(platform: str, chat_type: str = "single") -> UnansweredItem:
    return UnansweredItem(
        platform=platform, chat_id="chat-1", chat_name="项目群", chat_type=chat_type,
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

    ok, kind, err = service._create_for_item(
        _make_item("dingtalk"), ctx, {"self-oid"},
        # 留言当天晚上收到、主人同样在非工作时段 → 回执成立
        now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

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

    ok, kind, err = service._create_for_item(
        _make_item("wecom"), ctx, {"self-oid"},
        now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    assert ok and kind == "notify_only" and not err
    adapter.todo_task_create.assert_not_called()  # 企微无个人待办 API，不许伪装建单
    assert adapter.chat_message_send.call_count == 1
    rows, total = store._offhours_todo_repo.list_todo_records()
    assert total == 1 and rows[0]["todo_type"] == "notify_only"


# ---------------- 回执时效 / 时段闸门（2026-11-23 真实事故回归） ----------------

def test_notice_skipped_when_message_too_old(tmp_path):
    """陈旧留言不再给对方发「稍后回复」——隔天的回执既不合逻辑也像骚扰。

    真实事故：2026-11-22 09:26 白艳霞的留言，服务 11-23 14:25 才扫到并建单，
    随即发出「稍后回复」，主人当时已坐在工位上。

    本例刻意把「时段闸门」关掉，只让时效闸门单独生效，避免两个条件互相掩盖。
    """
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-x"}
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=6, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="白艳霞", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="白艳霞",
        content_preview="RG626B05 需要加到钉钉", timestamp="2026-11-22T09:26:10",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )

    # 当前仍在非工作时段（周日晚上），唯一拦住回执的只能是「留言太旧」
    ok, kind, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 0, tzinfo=TZ),
    )

    assert ok and kind == "platform_todo" and not err
    adapter.todo_task_create.assert_called_once()          # 待照建
    adapter.chat_message_send.assert_not_called()           # 但不回执
    rows, total = store._offhours_todo_repo.list_todo_records()
    assert total == 1 and rows[0]["sender_notified"] == 0


def test_notice_skipped_when_owner_back_at_work(tmp_path):
    """当前已回到工作时间 → 不发「稍后回复」（主人此刻就能回，说稍后是假话）。

    刻意把时效闸门放宽到 24h 且留言是 1 小时前，确保只有「时段闸门」生效。
    """
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-y"}
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=24, notice_only_when_offhours=True,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天给我回复", timestamp="2026-11-23T13:30:00",
        reason=REASON_OFF_HOURS, dedupe_key="dingtalk:chat-1:m1",
    )

    ok, _, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 23, 14, 0, tzinfo=TZ),
    )

    assert ok and not err
    adapter.chat_message_send.assert_not_called()


def test_notice_allowed_when_still_offhours_and_fresh(tmp_path):
    """留言新鲜且当前仍在非工作时段 → 回执照发（这是功能本意）。"""
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-z"}
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=6, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天给我回复", timestamp="2026-11-22T20:05:00",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )

    ok, _, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    assert ok and not err
    adapter.chat_message_send.assert_called_once()


def test_notice_gate_can_be_disabled(tmp_path):
    """两个闸门都可关：关掉后恢复旧行为（回执照发）。"""
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-w"}
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="白艳霞", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="白艳霞",
        content_preview="RG626B05", timestamp="2026-11-22T09:26:10",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )

    ok, _, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 23, 14, 25, tzinfo=TZ),
    )

    assert ok and not err
    adapter.chat_message_send.assert_called_once()


# ---------------- 平台侧补拉分窗（漏消息回归） ----------------

def test_window_chunks_covers_whole_lookback():
    """回看窗口被完整切成不重叠子窗（钉钉整窗翻页会漏消息，见 2026-11-23 事故）。"""
    from src.offhours_todo.scanner import _window_chunks

    now = datetime(2026, 11, 23, 14, 25, tzinfo=TZ)
    chunks = _window_chunks(now, 48, 6)

    assert len(chunks) == 8
    assert chunks[0][0] == now - timedelta(hours=48)
    assert chunks[-1][1] == now
    for prev, cur in zip(chunks, chunks[1:], strict=False):
        assert prev[1] == cur[0]  # 不重叠、不留缝


def test_platform_pull_is_split_into_windows(tmp_path):
    """平台侧补拉按子窗分次调用（不再整窗一把拉）。"""
    store = _make_store(tmp_path)
    adapter = MagicMock()
    # 平台侧返回空，只验证调用次数与时间窗粒度
    adapter.chat_message_list_all.return_value = {"conversationMessagesList": []}
    ctx = SimpleNamespace(store=store, dws=adapter)
    cfg = UnrepliedTodoConfig(enabled=True, lookback_hours=48)

    with patch("src.offhours_todo.scanner._fetch_unread_chats", return_value=set()):
        scan_platform(
            cfg, "dingtalk", ctx, self_ids={"self-oid"},
            now=datetime(2026, 11, 23, 14, 25, tzinfo=TZ),
        )

    assert adapter.chat_message_list_all.call_count == 8  # 48h / 6h


def test_owner_notified_marked_after_summary_sent(tmp_path):
    """本人汇总送达后回填标记（否则 Web 上这批记录永远显示「未通知本人」）。"""
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-n"}
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=True, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天给我回复", timestamp="2026-11-22T20:05:00",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )

    ok, _, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )
    assert ok and not err
    # 建单时先记 0（汇总还没发）
    assert store._offhours_todo_repo.list_todo_records()[0][0]["owner_notified"] == 0

    # 汇总送达后回填
    updated = store._offhours_todo_repo.mark_owner_notified(["dingtalk:chat-1:m1"])
    assert updated == 1
    assert store._offhours_todo_repo.list_todo_records()[0][0]["owner_notified"] == 1


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


# ---------------- 平台侧拉取补本地库缺口（服务离线期间未读消息） ----------------

def test_fetch_platform_messages_parses_dingtalk_list_all():
    """平台侧 list-all 返回结构 → 统一行；AI 代发标记识别为 is_bot。"""
    from src.offhours_todo.scanner import _fetch_platform_messages

    raw = {
        "conversationMessagesList": [
            {
                "openConversationId": "cid-1",
                "title": "魏欣悦",
                "singleChat": True,
                "messages": [
                    {
                        "messageId": "msg-real",
                        "sender": "魏欣悦",
                        "senderId": "peer-id",
                        "text": "周一改流程",
                        "createTime": "2026-11-21 16:44:02",
                    },
                    {
                        "messageId": "msg-ai",
                        "sender": "小主",
                        "senderId": "self-id",
                        "text": "收到",
                        "createTime": "2026-11-21 17:00:00",
                        "messageAiSendFlag": "DWS",  # AI 代发，不算真人回复
                    },
                ],
            }
        ]
    }
    adapter = SimpleNamespace(chat_message_list_all=lambda **kw: raw)
    rows = _fetch_platform_messages(
        adapter, "dingtalk", start="2026-11-21 00:00:00", end="2026-11-22 00:00:00"
    )
    assert len(rows) == 2
    real, ai = rows[0], rows[1]
    assert real["msg_id"] == "msg-real" and real["is_bot"] == 0
    assert real["timestamp"] == "2026-11-21T16:44:02"  # 空格→T
    assert real["chat_type"] == "single" and real["chat_name"] == "魏欣悦"
    assert ai["is_bot"] == 1  # AI 代发不算真人回复


def test_fetch_platform_messages_non_dingtalk_returns_empty():
    from src.offhours_todo.scanner import _fetch_platform_messages

    adapter = SimpleNamespace(chat_message_list_all=lambda **kw: {"conversationMessagesList": []})
    assert _fetch_platform_messages(adapter, "feishu", start="", end="") == []


def test_merge_rows_dedupes_by_chat_and_msg_id():
    """本地库与平台侧同 (chat_id, msg_id) 只保留平台侧（判定字段更准）。"""
    from src.offhours_todo.scanner import _merge_rows

    local = [
        {"chat_id": "c1", "msg_id": "m1", "sender_name": "旧", "is_bot": 0},
        {"chat_id": "c1", "msg_id": "m2", "sender_name": "本地独有"},
    ]
    platform = [
        {"chat_id": "c1", "msg_id": "m1", "sender_name": "新", "is_bot": 1},
    ]
    merged = _merge_rows(local, platform)
    assert len(merged) == 2
    by_id = {r["msg_id"]: r for r in merged}
    assert by_id["m1"]["is_bot"] == 1 and by_id["m1"]["sender_name"] == "新"
    assert by_id["m2"]["sender_name"] == "本地独有"


def test_scan_platform_finds_message_only_in_platform_side(tmp_path):
    """核心回归：消息只在平台侧、本地库没有，也要被扫出来建 pending。"""
    store = _make_store(tmp_path)
    ctx = SimpleNamespace(store=store, dws=None)
    raw = {
        "conversationMessagesList": [{
            "openConversationId": "cid-offline",
            "title": "魏欣悦",
            "singleChat": True,
            "messages": [{
                "messageId": "msg-offline",
                "sender": "魏欣悦",
                "senderId": "peer",
                "text": "周末发的消息",
                "createTime": "2026-11-21 16:44:02",
            }],
        }]
    }
    # 让 adapter 同时提供未读列表 + 平台侧拉取
    ctx.dws = SimpleNamespace(
        chat_message_list_unread_conversations=lambda count: [
            {"conversationId": "cid-offline"}
        ],
        chat_message_list_all=lambda **kw: raw,
    )
    cfg = UnrepliedTodoConfig(enabled=True)
    # now 固定为 11-22 19:00，lookback 48h 覆盖 11-21
    from datetime import datetime as _dt
    result = scan_platform(
        cfg, "dingtalk", ctx, self_ids={"self-id"},
        lookback_hours=48, now=_dt(2026, 11, 22, 19, 0, tzinfo=TZ),
    )
    assert result.pending_new == 1
    assert result.items[0].dedupe_key == "dingtalk:cid-offline:msg-offline"
    assert result.items[0].reason == REASON_WEEKEND
    assert result.items[0].unread is True


def test_create_for_item_parses_nested_dws_task_id(tmp_path):
    """dws todo task create 返回 {"success": true, "result": {"taskId": ...}} 要认出 task_id。"""
    store = _make_store(tmp_path)
    adapter = MagicMock()
    # 真实 dws 返回结构：result 里再包一层 taskId
    adapter.todo_task_create.return_value = {
        "success": True, "result": {"subject": "x", "taskId": "dws-task-999"}
    }
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid")
    cfg = UnrepliedTodoConfig(enabled=True, notify_sender=False, notify_owner=False)
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})

    ok, kind, err = service._create_for_item(_make_item("dingtalk"), ctx, {"self-oid"})
    assert ok and kind == "platform_todo" and not err
    rows = store._offhours_todo_repo.list_todo_records()[0]
    assert rows[0]["todo_id"] == "dws-task-999"


# ---------------- 只给真人留言建待办：过滤系统推送与纯确认词 ----------------

def test_ignored_message_filters_system_and_trivial():
    from src.offhours_todo.scanner import _is_ignored_message

    cfg = UnrepliedTodoConfig()
    # 系统推送（工作汇报日志）：伪装成 sendType=user，但 sender 名命中
    assert _is_ignored_message(
        {"sender_name": "工作汇报(日志)", "content": "[日志]:某某的周报 url: ..."}, cfg
    ).startswith("system_sender")
    # 内容前缀命中
    assert _is_ignored_message(
        {"sender_name": "某应用", "content": "[日志]:foo"}, cfg
    ).startswith("system_prefix")
    # 纯确认词
    assert _is_ignored_message({"sender_name": "张三", "content": "好的"}, cfg) == "trivial_reply"
    assert _is_ignored_message({"sender_name": "张三", "content": "收到，谢谢"}, cfg) == "trivial_reply"
    assert _is_ignored_message({"sender_name": "李四", "content": "ok"}, cfg) == "trivial_reply"
    # 真留言要保留
    assert _is_ignored_message(
        {"sender_name": "魏欣悦", "content": "徐工，周一：SOP 流程表头请增加申请文件编号"}, cfg
    ) == ""
    assert _is_ignored_message(
        {"sender_name": "张三", "content": "好的，明天我把方案发你"}, cfg
    ) == ""  # 带实质内容的「好的」不能误杀


def test_scan_platform_skips_noise_messages(tmp_path):
    """回归：系统日志推送与纯确认词不进 pending，真留言进。"""
    store = _make_store(tmp_path)
    raw = {
        "conversationMessagesList": [{
            "openConversationId": "cid-noise",
            "title": "混合会话",
            "singleChat": True,
            "messages": [
                {"messageId": "m-log", "sender": "工作汇报(日志)", "senderId": "app-id",
                 "text": "[日志]:某某周报 url: x", "createTime": "2026-11-21 10:00:00"},
                {"messageId": "m-ok", "sender": "张三", "senderId": "u-ok",
                 "text": "好的", "createTime": "2026-11-21 10:05:00"},
                {"messageId": "m-real", "sender": "魏欣悦", "senderId": "u-real",
                 "text": "请补充 SOP 流程的申请文件编号字段", "createTime": "2026-11-21 16:44:02"},
            ],
        }]
    }
    ctx = SimpleNamespace(store=store, dws=SimpleNamespace(
        chat_message_list_unread_conversations=lambda count: [{"conversationId": "cid-noise"}],
        chat_message_list_all=lambda **kw: raw,
    ))
    from datetime import datetime as _dt
    result = scan_platform(
        UnrepliedTodoConfig(enabled=True), "dingtalk", ctx, self_ids={"self-id"},
        lookback_hours=48, now=_dt(2026, 11, 22, 19, 0, tzinfo=TZ),
    )
    keys = [i.dedupe_key for i in result.items]
    assert len(keys) == 1 and keys[0].endswith(":m-real")


def test_resolve_executor_prefers_numeric_user_id():
    """钉钉待办执行人必须是数字 userId，不能是 openDingTalkId（否则待办不出现在待我处理）。"""
    # engine 无数字 ID 时，self_ids 里混着 openDingTalkId 和数字 ID，必须挑数字的
    svc = OffhoursTodoService(UnrepliedTodoConfig(owner_user_id=""), SimpleNamespace(current_user_id=""), {})
    assert svc._resolve_executor("dingtalk", {"DDn1bYIeDbcOPENID", "999888"}) == "999888"
    # engine 有数字 ID 时优先用 engine 的（最可信）
    svc_e = OffhoursTodoService(UnrepliedTodoConfig(owner_user_id=""), SimpleNamespace(current_user_id="3410690324258637"), {})
    assert svc_e._resolve_executor("dingtalk", {"999888"}) == "3410690324258637"
    # 配置优先
    cfg2 = UnrepliedTodoConfig(owner_user_id="3410690324258637")
    svc2 = OffhoursTodoService(cfg2, SimpleNamespace(current_user_id=""), {})
    assert svc2._resolve_executor("dingtalk", set()) == "3410690324258637"


# ------------------------------------------------------------------
# 建单前单会话定向复核（2026-11-23 实测事故修复）
# ------------------------------------------------------------------


def _recheck_adapter(replies: list[dict], *, complete: bool = True) -> MagicMock:
    """构造带单会话复核接口的 adapter 桩。

    replies: 本人在留言之后的回复列表（每项含 senderId/createTime/text）。
    """
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-r"}
    adapter.chat_message_list_conversation.return_value = {
        "messages": replies, "complete": complete, "partial": not complete,
        "has_more": False, "stop_reason": "source_complete" if complete else "page_limit",
    }
    return adapter


def test_create_skipped_when_conversation_recheck_finds_human_reply(tmp_path):
    """建单前复核发现本人已回复 → 不建待办、不发回执（真实事故：白艳霞那条）。

    扫描阶段的「全会话时间窗」补拉会被服务端静默截断（实测 68/118 条），
    截断时可能漏掉「本人在同会话里更晚的那条真人回复」，把「已回复」反转成
    「未回复」→ 误建待办 + 对外误发「稍后回复」回执。故建单前必须单会话复核。
    """
    store = _make_store(tmp_path)
    adapter = _recheck_adapter([
        {"senderId": "self-oid", "createTime": "2026-11-22 11:33:46", "text": "没事",
         "sendType": "user"},
    ])
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="白艳霞", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="白艳霞",
        content_preview="RG626B05 需要加到钉钉", timestamp="2026-11-22T09:26:10",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )
    # 模拟 scanner 已先落库的未回复消息
    store._offhours_todo_repo.upsert_unanswered(
        platform="dingtalk", chat_id="chat-1", msg_id="m1",
        dedupe_key="dingtalk:chat-1:m1", msg_timestamp="2026-11-22T09:26:10",
    )

    ok, kind, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    assert ok and kind == "already_replied" and not err
    adapter.todo_task_create.assert_not_called()      # 不建待办
    adapter.chat_message_send.assert_not_called()       # 不发回执
    assert store._offhours_todo_repo.get_unanswered_by_key(
        "dingtalk:chat-1:m1")["status"] == "replied_later"


def test_create_proceeds_when_conversation_recheck_finds_no_reply(tmp_path):
    """复核确认未回复 → 正常建单 + 发回执（复核不能误杀正常待办）。"""
    store = _make_store(tmp_path)
    adapter = _recheck_adapter([])
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天给我回复", timestamp="2026-11-22T20:05:00",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )

    ok, kind, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    assert ok and kind == "platform_todo" and not err
    adapter.todo_task_create.assert_called_once()
    adapter.chat_message_send.assert_called_once()


def test_create_proceeds_when_conversation_recheck_incomplete(tmp_path):
    """复核结果不完整（接口截断/失败）→ 保守放行建单，不能因探测失败漏掉真待办。

    复核是「防误建」的护栏，不是「准入门禁」：探测不确定时应按原流程走，
    否则接口一抖就整批待办建不出来。
    """
    store = _make_store(tmp_path)
    adapter = _recheck_adapter([], complete=False)
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="明天给我回复", timestamp="2026-11-22T20:05:00",
        reason=REASON_WEEKEND, dedupe_key="dingtalk:chat-1:m1",
    )

    ok, kind, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    assert ok and kind == "platform_todo" and not err
    adapter.todo_task_create.assert_called_once()


def test_notice_not_sent_for_group_chat(tmp_path):
    """群聊不发「稍后回复」回执——那句话是对个人说的承诺，在群里发是噪声。

    真实事故：2026-11-22 19:16 有 10 条回执被发进「工作汇报(日志)」系统日志群。
    """
    store = _make_store(tmp_path)
    adapter = _recheck_adapter([])
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = _make_item("dingtalk", chat_type="group")

    ok, kind, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    assert ok and kind == "platform_todo" and not err
    adapter.todo_task_create.assert_called_once()      # 待办照建
    adapter.chat_message_send.assert_not_called()       # 但群聊不发回执


def test_notice_message_persisted_as_bot(tmp_path):
    """回执落库必须标 assistant/is_bot=1，否则会被当成「本人真人回复」污染判定。

    回执是以本人账号发出的，钉钉侧看 sendType='user'、senderId=本人 → 轮询拉回后
    默认落成 role='user'/is_bot=0。而「未回复」口径正是 is_bot=0 且 sender_id∈本人，
    于是这条自动回执被当成「本人已亲自回复」，把同会话里更早的真留言误判为已回复。
    """
    store = _make_store(tmp_path)
    adapter = _recheck_adapter([])
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = _make_item("dingtalk")

    service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 22, 20, 30, tzinfo=TZ),
    )

    cur = store._conversation_repo._cc("dingtalk").cursor()
    rows = cur.execute(
        "SELECT role, is_bot, content FROM messages WHERE content LIKE '%记入待办%'"
    ).fetchall()
    assert rows, "回执未落库"
    for row in rows:
        assert row["role"] == "assistant"
        assert int(row["is_bot"]) == 1


def test_recheck_only_counts_own_human_replies():
    """单会话复核只认「本人真人回复」：他人消息 / AI 代发 / 留言之前的消息都不算。"""
    from src.offhours_todo.scanner import _recheck_conversation_replies

    adapter = MagicMock()
    adapter.chat_message_list_conversation.return_value = {
        "messages": [
            # 对方的消息 —— 不是本人回复
            {"senderId": "peer", "createTime": "2026-11-22 10:00:00", "text": "我也想问", "sendType": "user"},
            # AI 代发 —— 不是本人回复
            {"senderId": "self-oid", "createTime": "2026-11-22 10:10:00", "text": "AI 代发",
             "sendType": "user", "messageAiSendFlag": "DWS"},
            # 机器人 —— 不是本人回复
            {"senderId": "self-oid", "createTime": "2026-11-22 10:20:00", "text": "机器人", "sendType": "bot"},
            # 留言之前的本人消息 —— 不算「之后的回复」
            {"senderId": "self-oid", "createTime": "2026-11-22 09:00:00", "text": "更早的回复", "sendType": "user"},
        ],
        "complete": True, "partial": False, "has_more": False, "stop_reason": "source_complete",
    }

    replied, reason = _recheck_conversation_replies(
        adapter, "dingtalk", "chat-1", "2026-11-22 09:26:00", {"self-oid"}, TZ,
    )

    assert replied is False and reason == ""


def test_recheck_detects_own_human_reply():
    """复核命中本人真人回复 → 判定为已回复。"""
    from src.offhours_todo.scanner import _recheck_conversation_replies

    adapter = MagicMock()
    adapter.chat_message_list_conversation.return_value = {
        "messages": [
            {"senderId": "self-oid", "createTime": "2026-11-22 11:33:46", "text": "没事", "sendType": "user"},
        ],
        "complete": True, "partial": False, "has_more": False, "stop_reason": "source_complete",
    }

    replied, _ = _recheck_conversation_replies(
        adapter, "dingtalk", "chat-1", "2026-11-22 09:26:00", {"self-oid"}, TZ,
    )

    assert replied is True


def test_recheck_returns_unknown_when_incomplete():
    """复核结果不完整 → 返回 None（无法判定），调用方据此保守放行。"""
    from src.offhours_todo.scanner import _recheck_conversation_replies

    adapter = MagicMock()
    adapter.chat_message_list_conversation.return_value = {
        "messages": [], "complete": False, "partial": True,
        "has_more": True, "stop_reason": "page_limit",
    }

    replied, reason = _recheck_conversation_replies(
        adapter, "dingtalk", "chat-1", "2026-11-22 09:26:00", {"self-oid"}, TZ,
    )

    assert replied is None and "incomplete" in reason


# ------------------------------------------------------------------
# 「对方接续我的回复」判据（2026-11-25 开机补拉误建 4 条事故）
# ------------------------------------------------------------------


def test_trivial_reply_with_emoji_marker_is_ignored():
    """钉钉表情标记不能把纯客套变成待办，也不能误伤配置里的整条表情短语。"""
    from src.offhours_todo.scanner import _is_ignored_message

    cfg = UnrepliedTodoConfig()
    assert _is_ignored_message({"content": "好的，谢谢[抱拳]"}, cfg) == "trivial_reply"
    assert _is_ignored_message({"content": "收到[OK]"}, cfg) == "trivial_reply"
    assert _is_ignored_message({"content": "[OK]"}, cfg) == "trivial_reply"
    # 带实质内容的留言不能误杀
    assert _is_ignored_message({"content": "好的，明天给你"}, cfg) == ""


def _prior_adapter(messages: list[dict], *, complete: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.chat_message_list_conversation.return_value = {
        "messages": messages, "complete": complete, "partial": not complete,
        "has_more": False, "stop_reason": "source_complete" if complete else "page_limit",
    }
    return adapter


def test_prior_own_reply_detected_with_older_direction():
    """留言前紧邻本人真人回复 → 已答复；复核必须走 older 方向。"""
    from src.offhours_todo.scanner import _recheck_prior_own_reply

    adapter = _prior_adapter([
        {"senderId": "self-oid", "createTime": "2026-11-24 21:37:02",
         "text": "业绩数据全部为 0 的不展示", "sendType": "user"},
        {"senderId": "peer", "createTime": "2026-11-24 20:57:12",
         "text": "这三个人还是不显示", "sendType": "user"},
    ])
    replied, reason = _recheck_prior_own_reply(
        adapter, "dingtalk", "chat-1",
        datetime(2026, 11, 24, 21, 49, 8, tzinfo=TZ), {"self-oid"}, TZ, 30,
    )
    assert replied is True and reason == "own_reply_just_before"
    assert adapter.chat_message_list_conversation.call_args.kwargs["direction"] == "older"


def test_prior_reply_rejects_peer_new_request_and_ai():
    """紧邻上一条是对方自己（新诉求）或 AI 代发 → 不算已答复，照常建单。"""
    from src.offhours_todo.scanner import _recheck_prior_own_reply

    before = datetime(2026, 11, 24, 19, 15, 50, tzinfo=TZ)
    peer_last = _prior_adapter([
        {"senderId": "peer", "createTime": "2026-11-24 19:15:40", "text": "再问个事", "sendType": "user"},
        {"senderId": "self-oid", "createTime": "2026-11-24 19:15:00", "text": "好", "sendType": "user"},
    ])
    replied, reason = _recheck_prior_own_reply(
        peer_last, "dingtalk", "chat-1", before, {"self-oid"}, TZ, 30,
    )
    assert replied is False and reason == "prior_sender_not_self"

    ai_last = _prior_adapter([
        {"senderId": "self-oid", "createTime": "2026-11-24 19:15:40", "text": "AI 回执",
         "sendType": "user", "messageAiSendFlag": "DWS"},
    ])
    replied, reason = _recheck_prior_own_reply(
        ai_last, "dingtalk", "chat-1", before, {"self-oid"}, TZ, 30,
    )
    assert replied is False and reason == "prior_is_bot"


def test_prior_reply_respects_window_and_completeness():
    """超窗口 / 结果截断 / 判据关闭 → 不误判为已答复。"""
    from src.offhours_todo.scanner import _recheck_prior_own_reply

    before = datetime(2026, 11, 24, 21, 49, 8, tzinfo=TZ)
    old = _prior_adapter([
        {"senderId": "self-oid", "createTime": "2026-11-24 20:00:00",
         "text": "很早以前回过", "sendType": "user"},
    ])
    replied, reason = _recheck_prior_own_reply(
        old, "dingtalk", "chat-1", before, {"self-oid"}, TZ, 30,
    )
    assert replied is False and reason == "prior_reply_too_old"

    incomplete = _prior_adapter([], complete=False)
    replied, reason = _recheck_prior_own_reply(
        incomplete, "dingtalk", "chat-1", before, {"self-oid"}, TZ, 30,
    )
    assert replied is False and "incomplete" in reason

    fresh = _prior_adapter([
        {"senderId": "self-oid", "createTime": "2026-11-24 21:49:00",
         "text": "刚回过", "sendType": "user"},
    ])
    replied, reason = _recheck_prior_own_reply(
        fresh, "dingtalk", "chat-1", before, {"self-oid"}, TZ, 0,
    )
    assert replied is False and reason == "disabled"


def test_create_skipped_when_peer_continues_my_reply(tmp_path):
    """端到端：我先答、对方发收尾 → 不建待办、不发回执、状态标已答复。"""
    store = _make_store(tmp_path)
    adapter = MagicMock()
    adapter.todo_task_create.return_value = {"task_id": "todo-r"}

    def fake_list(cid, time_str, *, direction="newer"):
        msgs = [] if direction == "newer" else [
            {"senderId": "self-oid", "createTime": "2026-11-24 18:48:06",
             "text": "我在地铁没法验证，先找根网线应急吧", "sendType": "user"},
        ]
        return {"messages": msgs, "complete": True, "partial": False,
                "has_more": False, "stop_reason": "source_complete"}

    adapter.chat_message_list_conversation.side_effect = fake_list
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(current_open_dingtalk_id="self-oid", current_user_id="uid")
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
        prior_reply_window_minutes=30,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id="m1", sender_id="peer", sender_name="张三",
        content_preview="好的，谢谢[抱拳]", timestamp="2026-11-24T18:48:38",
        reason=REASON_OFF_HOURS, dedupe_key="dingtalk:chat-1:m1",
    )
    store._offhours_todo_repo.upsert_unanswered(
        platform="dingtalk", chat_id="chat-1", msg_id="m1",
        dedupe_key="dingtalk:chat-1:m1", msg_timestamp="2026-11-24T18:48:38",
    )

    ok, kind, err = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 25, 9, 39, tzinfo=TZ),
    )

    assert ok and kind == "already_replied" and not err
    adapter.todo_task_create.assert_not_called()
    adapter.chat_message_send.assert_not_called()
    assert store._offhours_todo_repo.get_unanswered_by_key(
        "dingtalk:chat-1:m1")["status"] == "replied_later"


# ------------------------------------------------------------------
# 建单前 LLM 意图闸门（坤哥 2026-11-25：寒暄没营养，判断后再建单）
# ------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[list[dict], float | None]] = []

    def chat(self, messages: list[dict], temperature: float | None = None):
        self.calls.append((messages, temperature))
        return SimpleNamespace(content=self.answer)


class _BrokenLLM:
    def chat(self, messages: list[dict], temperature: float | None = None):
        raise RuntimeError("model unavailable")


def _intent_ctx(store, llm, item=None):
    item = item or _intent_item()
    adapter = _recheck_adapter([])
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(
        current_open_dingtalk_id="self-oid", current_user_id="uid", llm_client=llm,
    )
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
        intent_gate_enabled=True,
    )
    store._offhours_todo_repo.upsert_unanswered(
        platform="dingtalk", chat_id=item.chat_id, msg_id=item.msg_id,
        dedupe_key=item.dedupe_key, msg_timestamp=item.timestamp,
    )
    return OffhoursTodoService(cfg, engine, {"dingtalk": ctx}), ctx, adapter


def _intent_item(msg_id: str = "m1") -> UnansweredItem:
    return UnansweredItem(
        platform="dingtalk", chat_id="chat-1", chat_name="张三", chat_type="single",
        msg_id=msg_id, sender_id="peer", sender_name="张三",
        content_preview="好的，谢谢[抱拳]", timestamp="2026-11-24T18:48:38",
        reason=REASON_OFF_HOURS, dedupe_key=f"dingtalk:chat-1:{msg_id}",
    )


def test_intent_gate_skips_small_talk(tmp_path):
    """LLM 判无实质诉求 → 不建单；正文逐字放进边界标记内供模型判别。"""
    store = _make_store(tmp_path)
    llm = _FakeLLM("NO")
    service, ctx, adapter = _intent_ctx(store, llm)

    ok, kind, err = service._create_for_item(
        _intent_item(), ctx, {"self-oid"}, now=datetime(2026, 11, 25, 9, 39, tzinfo=TZ),
    )

    assert ok and kind == "no_action_intent" and not err
    adapter.todo_task_create.assert_not_called()
    assert store._offhours_todo_repo.get_unanswered_by_key(
        "dingtalk:chat-1:m1")["status"] == "ignored"
    messages, temperature = llm.calls[0]
    assert temperature == 0
    assert "<incoming_message>" in messages[-1]["content"]
    assert "好的，谢谢[抱拳]" in messages[-1]["content"]  # 逐字保留，不改写正文


def test_intent_gate_allows_real_request(tmp_path):
    """LLM 判有实质诉求 → 照常建单。"""
    store = _make_store(tmp_path)
    service, ctx, adapter = _intent_ctx(store, _FakeLLM("YES\n"))

    ok, kind, err = service._create_for_item(
        _intent_item(), ctx, {"self-oid"}, now=datetime(2026, 11, 25, 9, 39, tzinfo=TZ),
    )

    assert ok and kind == "platform_todo" and not err
    adapter.todo_task_create.assert_called_once()


def test_intent_gate_fails_open(tmp_path):
    """模型报错或输出无法解析 → 放行建单，不能因模型抖动漏掉真待办。"""
    for i, llm in enumerate((_BrokenLLM(), _FakeLLM("我无法判断")), start=1):
        store = _make_store(tmp_path)
        item = _intent_item(msg_id=f"m{i}")
        service, ctx, adapter = _intent_ctx(store, llm, item)
        ok, kind, _ = service._create_for_item(
            item, ctx, {"self-oid"},
            now=datetime(2026, 11, 25, 9, 39, tzinfo=TZ),
        )
        assert ok and kind == "platform_todo"
        adapter.todo_task_create.assert_called_once()


def test_intent_gate_can_be_disabled(tmp_path):
    """开关关闭时不做意图判断；LLM 只剩回执文案生成这一处用途。"""
    store = _make_store(tmp_path)
    llm = _FakeLLM("NO")
    service, ctx, adapter = _intent_ctx(store, llm)
    service.cfg.intent_gate_enabled = False

    ok, kind, _ = service._create_for_item(
        _intent_item(), ctx, {"self-oid"}, now=datetime(2026, 11, 25, 9, 39, tzinfo=TZ),
    )

    assert ok and kind == "platform_todo"
    # 意图闸门关闭 → 两次调用分别只是待办摘要与回执文案，不含意图判别
    assert len(llm.calls) == 2
    assert all("消息意图判别器" not in call[0][0]["content"] for call in llm.calls)


def test_local_intent_gate_works_without_llm(tmp_path):
    """LLM 不可用时本地意图分类照常生效（本机密钥过期场景必须能拦住寒暄）。"""
    store = _make_store(tmp_path)
    adapter = _recheck_adapter([])
    ctx = SimpleNamespace(store=store, dws=adapter)
    engine = SimpleNamespace(
        current_open_dingtalk_id="self-oid", current_user_id="uid",
        rule_engine=SimpleNamespace(
            _detect_intent=lambda text: ("acknowledge", "确认收到（权重:2）", 1.0),
        ),
    )
    cfg = UnrepliedTodoConfig(
        enabled=True, notify_sender=True, notify_owner=False, owner_user_id="uid",
        notice_max_age_hours=0, notice_only_when_offhours=False,
        intent_gate_enabled=True,
    )
    service = OffhoursTodoService(cfg, engine, {"dingtalk": ctx})
    item = _intent_item()
    store._offhours_todo_repo.upsert_unanswered(
        platform="dingtalk", chat_id=item.chat_id, msg_id=item.msg_id,
        dedupe_key=item.dedupe_key, msg_timestamp=item.timestamp,
    )

    ok, kind, _ = service._create_for_item(
        item, ctx, {"self-oid"}, now=datetime(2026, 11, 25, 9, 39, tzinfo=TZ),
    )

    assert ok and kind == "no_action_intent"
    adapter.todo_task_create.assert_not_called()
