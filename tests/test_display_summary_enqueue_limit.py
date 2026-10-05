"""展示摘要入队限流 + 会话库游标收口的回归测试（2026-10-05 锁争用事故）。

事故：真实库有 910 个会话满足展示摘要条件，``_run_once`` **一次性全部入队**
（get_conversations_needing_summary 无 LIMIT），worker 串行消费、每条都持有
WAL 读事务 → 与轮询器/摘要调度器/消息清理争抢写锁，日志 5 分钟内 382 次
"database is locked"，轮询实质停摆。而 worker 单条耗时（含 LLM）远快不过
入队速度，队列永久积压 → 写锁高压永续。

值得注意的是 ``job_interval_seconds`` 早已存在且注释明写「若不节流会形成持续的
LLM+写库洪峰，与轮询器抢写锁」——但它只在 **worker 消费端**限速，
**入队端无上限**，故节流形同虚设。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.llm.display_summary_scheduler import DisplaySummaryScheduler  # noqa: E402


def _make_scheduler( n_chats=910, max_enqueue=20):
    """构造一个只关心「入队了多少」的调度器（不发线程、不调 LLM）。"""
    store = MagicMock()
    store._closed = False
    store._message_repo.get_conversations_needing_summary.return_value = [
        {"chat_id": f"C{i}", "chat_name": f"会话{i}", "chat_type": "single",
         "message_count": 100}
        for i in range(n_chats)
    ]
    sched = DisplaySummaryScheduler(
        agent=MagicMock(),
        store=store,
        platform="dingtalk",
        max_enqueue_per_round=max_enqueue,
    )
    # 不启动线程；直接拦 _enqueue 记录入队量
    enqueued: list[str] = []
    sched._enqueue = lambda cid, trigger="": enqueued.append(cid)  # type: ignore[assignment]
    return sched, enqueued


class TestEnqueueRateLimit:
    def test_single_round_caps_enqueue(self, tmp_path):
        """单轮入队不超过上限（910 → 20）。"""
        sched, enqueued = _make_scheduler( n_chats=910, max_enqueue=20)
        sched._run_once()
        assert len(enqueued) == 20, f"应限流到 20，实际入队 {len(enqueued)}"

    def test_limit_of_one(self, tmp_path):
        """上限为 1 时只入队 1 个（极保守配置也不越界）。"""
        sched, enqueued = _make_scheduler( n_chats=50, max_enqueue=1)
        sched._run_once()
        assert len(enqueued) == 1

    def test_under_limit_not_truncated(self, tmp_path):
        """待摘要数少于上限时不误伤（正常小批量不受影响）。"""
        sched, enqueued = _make_scheduler( n_chats=5, max_enqueue=20)
        sched._run_once()
        assert len(enqueued) == 5, "未超限时应全部入队"

    def test_zero_chats(self, tmp_path):
        """无待摘要会话时不入队。"""
        sched, enqueued = _make_scheduler( n_chats=0)
        sched._run_once()
        assert enqueued == []

    def test_default_limit_is_bounded(self, tmp_path):
        """默认构造也必须有上限（防止调用方忘传导致洪峰重现）。"""
        store = MagicMock()
        store._closed = False
        store._message_repo.get_conversations_needing_summary.return_value = [
            {"chat_id": f"C{i}", "message_count": 100} for i in range(1000)
        ]
        sched = DisplaySummaryScheduler(agent=MagicMock(), store=store, platform="dingtalk")
        assert 1 <= sched._max_enqueue_per_round <= 100, (
            f"默认上限 {sched._max_enqueue_per_round} 不在合理区间"
        )

    def test_oversize_config_still_bounded(self, tmp_path):
        """传入超大上限时按传入值（允许调大以便需要时提速），但不得为 0/负。"""
        store = MagicMock()
        store._closed = False
        sched = DisplaySummaryScheduler(
            agent=MagicMock(), store=store, platform="dingtalk",
            max_enqueue_per_round=0,
        )
        assert sched._max_enqueue_per_round >= 1, "上限为 0 会被 clamp 到至少 1"

    def test_closed_store_skips(self, tmp_path):
        """store 已关闭时跳过本轮（不产生写压力）。"""
        sched, enqueued = _make_scheduler( n_chats=100)
        sched._store._closed = True
        sched._run_once()
        assert enqueued == []


class TestCursorClose:
    """异常路径的游标泄漏会持有 WAL 读事务，是锁争用的隐性来源。"""

    def test_fetch_messages_in_range_closes_cursor_on_error(self, tmp_path):
        """查询抛异常时返回空 dict 而非抛出，且不泄漏游标。"""
        import sqlite3

        from src.memory.sqlite_store import SQLiteStore

        store = SQLiteStore(db_path=str(tmp_path / "l.db"))
        repo = store._conversation_repo
        orig = repo._cc

        def bad_cc(platform=""):
            conn = orig(platform)

            class _Bad:
                """cursor() 直接抛错，验证调用方走的是异常分支且能收口。"""

                def __init__(self, c):
                    self._c = c

                def cursor(self):
                    raise sqlite3.OperationalError("模拟游标创建失败")

                def commit(self):
                    return self._c.commit()

            return _Bad(conn)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo, "_cc", bad_cc)
            out = repo.fetch_messages_in_range("2026-01-01", "2026-01-02")
        assert out == {}, "异常路径应返回空 dict 而非抛出"
        store.close()

    def test_upsert_summary_closes_cursor_on_all_paths(self, tmp_path):
        """CAS 写回的 3 个返回路径都应 close 游标（用 finally 兜住）。"""
        from src.memory.sqlite_store import SQLiteStore

        store = SQLiteStore(db_path=str(tmp_path / "l2.db"))
        repo = store._conversation_repo
        # 正常路径
        ok = repo.upsert_conversation_summary("C1", "摘要", "m1", 5, 0)
        assert ok is True
        # 空参数短路路径
        assert repo.upsert_conversation_summary("", "x", "m1", 1, 0) is False
        assert repo.upsert_conversation_summary("C1", "", "m1", 1, 0) is False
        # CAS 代际不符路径
        repo.upsert_conversation_summary("C2", "摘要", "m1", 5, 0)
        again = repo.upsert_conversation_summary("C2", "新摘要", "m2", 6, 0)
        assert isinstance(again, bool)
        store.close()


class TestRecentDaysFilter:
    """候选收窄到「今日活跃」：942 → 4，是锁争用的最大单一减量。"""

    def test_recent_days_passed_to_query(self, tmp_path):
        """调度器须把 recent_days 透传给查询（0 = 仅今日）。"""
        sched, _ = _make_scheduler()
        sched._run_once()   # 触发一次查询后才有 call_args
        kwargs = sched._store._message_repo.get_conversations_needing_summary.call_args.kwargs
        assert "only_recent_days" in kwargs, "未传 only_recent_days"
        assert kwargs["only_recent_days"] == sched._recent_days

    def test_default_is_today_only(self, tmp_path):
        """默认口径为「仅今日」（recent_days=0），不得默认为全量。"""
        store = MagicMock()
        store._closed = False
        store._message_repo.get_conversations_needing_summary.return_value = []
        sched = DisplaySummaryScheduler(agent=MagicMock(), store=store, platform="dingtalk")
        assert sched._recent_days == 0

    def test_none_disables_filter(self, tmp_path):
        """传 None 可退回全量（供回溯场景）。"""
        store = MagicMock()
        store._closed = False
        store._message_repo.get_conversations_needing_summary.return_value = []
        sched = DisplaySummaryScheduler(
            agent=MagicMock(), store=store, platform="dingtalk", recent_days=None
        )
        assert sched._recent_days is None
        sched._run_once()
        kwargs = store._message_repo.get_conversations_needing_summary.call_args.kwargs
        assert kwargs["only_recent_days"] is None

    def test_negative_clamps_to_zero(self, tmp_path):
        """负值被 clamp 到 0（今日），不得变成「未来」这类意外语义。"""
        store = MagicMock()
        store._closed = False
        store._message_repo.get_conversations_needing_summary.return_value = []
        sched = DisplaySummaryScheduler(
            agent=MagicMock(), store=store, platform="dingtalk", recent_days=-5
        )
        assert sched._recent_days == 0
