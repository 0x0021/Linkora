"""摘要「取值范围」缺陷的回归测试（P0）。

背景：线上摘要出现「一条摘要里混进很多不相关的人和事」，根因是取材环节的三处缺陷：

1. **元消息污染**：本系统自己写回 messages 表的产物被当成对话正文再次摘要——
   role='system' 的落库摘要、以「【对话摘要】」开头的历史摘要、以及主动触达 digest
   推送后回灌到主人会话的「📋 …对话摘要…」副本（后者装着**其他所有会话**的人和事）。
   LLM 把其中的人名/事项当成本会话内容复述，且每轮把上轮污染再放大一次。
2. **时区/格式错配**：last_summary_at 是 SQLite datetime('now') 写入的 UTC、空格分隔串；
   messages.timestamp 是应用层写入的本地时间、'T' 分隔串。直接字符串比较时
   'T'(0x54) > ' '(0x20) 恒成立 → 时间部分失效，范围退化成「上次摘要所在日 0 点起」，
   再叠加 8 小时时区差 → 已摘要的旧内容被反复纳入。
3. **超量时丢新保旧**：有起点时按 ASC LIMIT 取的是**最旧**的 N 条，增量超过上限时
   新内容永远摘不到。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.constants import is_summary_noise_message
from src.memory.sqlite_store import SQLiteStore

# 三类「元消息」样本（内容与线上真实形态一致，人名已脱敏）
_NOISE_SYSTEM = "【对话摘要】我协助多位同事解决技术与办公问题：指导甲配置CRM、跟进乙返厂工单。"
_NOISE_LEGACY = "【对话摘要】近期我处理了多项工作，涉及丙、丁、戊的事项。"
_NOISE_DIGEST = "📋 近 24 小时对话摘要（共 8 段）\n\n• **张三**：张三找我处理报表导入。\n\n• **李四**：李四咨询账号开通。"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _sqlite_now_utc(dt: datetime) -> str:
    """模拟 SQLite datetime('now') 的产物：UTC、空格分隔、无时区后缀。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_naive(dt: datetime) -> str:
    """模拟应用层写入 messages.timestamp 的形态：本地时间、'T' 分隔、无时区后缀。"""
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S")


def _make_store(tmp_path):
    return SQLiteStore(db_path=str(tmp_path / "linkora.db"))


def _insert_conversation(cur, chat_id, message_count, last_summary_at, updated_at):
    cur.execute(
        """INSERT OR REPLACE INTO conversations
           (chat_id, chat_name, chat_type, message_count, last_summary_at, created_at, updated_at)
           VALUES (?, ?, 'single', ?, ?, ?, ?)""",
        (chat_id, chat_id, message_count, last_summary_at,
         _iso(datetime.now(timezone.utc)), updated_at),
    )


def _insert_message(cur, chat_id, msg_id, content, ts, role="user", is_archived=0):
    cur.execute(
        """INSERT OR REPLACE INTO messages
           (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, timestamp, role, is_archived, created_at)
           VALUES (?, 'single', ?, 'u1', 'user', ?, 'text', ?, ?, ?, ?)""",
        (chat_id, msg_id, content, ts, role, is_archived, _iso(datetime.now(timezone.utc))),
    )


# -------------------------------------------------------------------------- 1) 元消息过滤


def test_collect_excludes_summary_meta_messages(tmp_path):
    """取材时必须排除三类元消息，否则会把别的会话的人和事混进本会话摘要。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "M", 5, None, _iso(now))
    _insert_message(cur, "M", "m_real1", "帮我看下打印机", _local_naive(now - timedelta(minutes=30)))
    _insert_message(cur, "M", "m_real2", "内网切换试试", _local_naive(now - timedelta(minutes=29)))
    _insert_message(cur, "M", "m_sys", _NOISE_SYSTEM, _local_naive(now - timedelta(minutes=28)), role="system")
    _insert_message(cur, "M", "m_legacy", _NOISE_LEGACY, _local_naive(now - timedelta(minutes=27)))
    _insert_message(cur, "M", "m_digest", _NOISE_DIGEST, _local_naive(now - timedelta(minutes=26)))
    store._message_repo._cc().commit()

    msgs = store._message_repo.collect_dynamic_summary_messages("M", max_messages=100)
    contents = [m.content for m in msgs]
    assert contents == ["帮我看下打印机", "内网切换试试"], (
        f"元消息（历史摘要 / digest 回灌 / system）不得参与摘要取材，实际取到：{contents}"
    )


def test_recent_unarchived_excludes_summary_meta_messages(tmp_path):
    """定时摘要链路（get_recent_unarchived_messages）同样要过滤元消息。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    _insert_conversation(cur, "R", 4, None, _iso(now))
    _insert_message(cur, "R", "r_digest", _NOISE_DIGEST, _local_naive(now - timedelta(minutes=40)))
    _insert_message(cur, "R", "r_legacy", _NOISE_LEGACY, _local_naive(now - timedelta(minutes=30)))
    _insert_message(cur, "R", "r1", "明天的会议改到下午", _local_naive(now - timedelta(minutes=20)))
    _insert_message(cur, "R", "r2", "收到", _local_naive(now - timedelta(minutes=19)))
    store._message_repo._cc().commit()

    msgs = store._message_repo.get_recent_unarchived_messages("R", limit=40)
    contents = [m.content for m in msgs]
    assert contents == ["明天的会议改到下午", "收到"], f"实际取到：{contents}"


def test_is_summary_noise_message_predicate():
    """噪声判定谓词：三类元消息命中，正常对话不误伤。"""
    assert is_summary_noise_message(_NOISE_SYSTEM, "user")
    assert is_summary_noise_message(_NOISE_LEGACY, "user")
    assert is_summary_noise_message(_NOISE_DIGEST, "user")
    assert is_summary_noise_message("随便一句正常话", "system")
    # 正常对话不得误伤
    assert not is_summary_noise_message("帮我看下打印机", "user")
    assert not is_summary_noise_message("📌 会议纪要已发你邮箱", "user")
    assert not is_summary_noise_message("", "user")


# -------------------------------------------------------------------------- 2) 时区/格式边界


def test_collect_boundary_with_sqlite_utc_last_summary_at(tmp_path):
    """last_summary_at 为 SQLite UTC 空格格式、timestamp 为本地 ISO 时的边界正确性。

    这是线上真实形态。修复前 'T' > ' ' 使时间部分失效，早于边界的消息会被错误纳入。
    """
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    boundary = datetime.now(timezone.utc) - timedelta(minutes=15)
    # 与线上一致：last_summary_at 用 SQLite datetime('now') 形态（UTC、空格分隔）
    _insert_conversation(cur, "T", 4, _sqlite_now_utc(boundary), _iso(datetime.now(timezone.utc)))
    # 早于边界 5 分钟（本地 ISO）→ 必须排除
    _insert_message(cur, "T", "t_old", "old", _local_naive(boundary - timedelta(minutes=5)))
    # 晚于边界 5 分钟（本地 ISO）→ 必须收集
    _insert_message(cur, "T", "t_new", "new", _local_naive(boundary + timedelta(minutes=5)))
    store._message_repo._cc().commit()

    msgs = store._message_repo.collect_dynamic_summary_messages("T", max_messages=100)
    assert [m.content for m in msgs] == ["new"], "早于 last_summary_at 的旧消息不得纳入"


def test_chats_needing_summary_counts_with_utc_boundary(tmp_path):
    """get_chats_needing_dynamic_summary 的 unsummarized 计数也要按 UTC 归一。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    boundary = now - timedelta(hours=2)
    _insert_conversation(cur, "U", 5, _sqlite_now_utc(boundary), _iso(now))
    # 边界前 3 条 + 边界后 3 条
    for i in range(3):
        _insert_message(cur, "U", f"u_old{i}", f"old{i}",
                        _local_naive(boundary - timedelta(minutes=10) + timedelta(seconds=i)))
    for i in range(3):
        _insert_message(cur, "U", f"u_new{i}", f"new{i}",
                        _local_naive(boundary + timedelta(minutes=10) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    chats = store._message_repo.get_chats_needing_dynamic_summary(
        quiet_minutes=10, min_messages=3, max_messages_per_chat=100, max_age_hours=24,
    )
    by_id = {c["chat_id"]: c["unsummarized"] for c in chats}
    assert by_id.get("U") == 3, f"unsummarized 应只统计边界后的 3 条，实际：{by_id}"


# -------------------------------------------------------------------------- 3) 超量时保新


def test_collect_keeps_newest_when_exceeding_limit(tmp_path):
    """增量超过 max_messages 时应保留最新的 N 条（修复前 ASC LIMIT 会丢掉最新内容）。"""
    store = _make_store(tmp_path)
    cur = store._message_repo._cc().cursor()
    now = datetime.now(timezone.utc)
    boundary = now - timedelta(minutes=10)
    _insert_conversation(cur, "O", 5, _sqlite_now_utc(boundary), _iso(now))
    for i in range(5):
        _insert_message(cur, "O", f"o{i}", f"o{i}",
                        _local_naive(boundary + timedelta(minutes=1) + timedelta(seconds=i)))
    store._message_repo._cc().commit()

    msgs = store._message_repo.collect_dynamic_summary_messages("O", max_messages=2)
    assert [m.content for m in msgs] == ["o3", "o4"], "超量时应保留最新的消息"
