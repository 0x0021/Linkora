from __future__ import annotations

from datetime import datetime

from src.config_models import SummaryBackfillConfig
from src.llm.summary_backfill import SummaryBackfill, _parse_ts


def _local_iso(dt: datetime) -> str:
    """把 naive 本地 datetime 转 ISO 字符串（不带时区，与库内 naive 存储一致）。"""
    return dt.replace(tzinfo=None).isoformat()


class _FakeClient:
    """模拟 agent.client：chat 返回固定摘要文本。"""

    def chat(self, messages, temperature: float = 0.1):
        from types import SimpleNamespace
        return SimpleNamespace(content="【对话摘要】当天讨论了项目排期与待办。")


class _FakeAgent:
    """模拟 LLMAgent：提供 user_name + client，使模块级 summarize_conversation 跑通。"""

    def __init__(self) -> None:
        self.user_name = "坤哥"
        self.client = _FakeClient()
        self.calls: list[list] = []

    def summarize_conversation(self, messages, max_messages: int = 0) -> str:
        # 仅记录调用（生产走模块级函数，此处不被 backfill 直接调用，留作兼容）
        self.calls.append(list(messages))
        return "【对话摘要】当天讨论了项目排期与待办。"


class _FakeRepo:
    """模拟 ConversationRepo（仅 backfill 用到的几个方法）。"""

    def __init__(self) -> None:
        self.upserts: list[tuple] = []
        self.updated_ats: dict[str, str] = {}
        self._range_rows: "dict[str, list[dict]]" = {}

    def set_range(self, grouped: "dict[str, list[dict]]") -> None:
        self._range_rows = grouped

    def fetch_messages_in_range(self, start_iso: str, end_iso: str,
                                platform: str = "", limit_per_chat: int = 200,
                                skip_msg_types=None) -> "dict[str, list[dict]]":
        # 粗筛：直接返回注入数据（python 端会按 _parse_ts 精确归日）
        return self._range_rows

    def list_recent_summaries(self, limit: int = 20, platform: str = "",
                              since: str | None = None) -> list[dict]:
        # 返回 upsert 写回的内容（仅用于近七天预览同源校验）
        out = []
        for chat_id, (summary, day_end) in self._written.items():
            if since and day_end < since:
                continue
            out.append({"chat_id": chat_id, "chat_name": chat_id,
                        "summary_text": summary, "updated_at": day_end})
        return out[:limit]

    def upsert_conversation_summary(self, chat_id: str, summary: str,
                                    older_boundary_msg_id: str, covered_count: int,
                                    expected_generation: int = 0,
                                    platform: str = "") -> bool:
        self.upserts.append((chat_id, summary, covered_count, platform))
        return True

    def update_summary_updated_at(self, chat_id: str, updated_at_iso: str,
                                  platform: str = "") -> bool:
        self.updated_ats[chat_id] = updated_at_iso
        return True

    # —— 辅助：记录写回内容供 list_recent_summaries 复用 ——
    @property
    def _written(self) -> "dict[str, tuple[str, str]]":
        # 用 upserts + updated_ats 组合出 (summary, day_end)
        res: "dict[str, tuple[str, str]]" = {}
        for chat_id, summary, _covered, _plat in self.upserts:
            res[chat_id] = (summary, self.updated_ats.get(chat_id, ""))
        return res


class _FakeStore:
    def __init__(self, repo: _FakeRepo, meta: "dict[str, str] | None" = None) -> None:
        self._conversation_repo = repo
        self._meta = dict(meta or {})

    def get_meta(self, key: str, default: str = "") -> str:
        return self._meta.get(key, default)

    def set_meta(self, key: str, value: str) -> bool:
        self._meta[key] = value
        return True


def _scheduler(store: _FakeStore, agent: _FakeAgent,
               cfg: SummaryBackfillConfig | None = None) -> SummaryBackfill:
    return SummaryBackfill(
        agent=agent, store=store, config=cfg or SummaryBackfillConfig(),
        platform="dingtalk", min_interval_seconds=0.0,
    )


# ── _parse_ts 解析兼容 ──
def test_parse_ts_naive():
    assert _parse_ts("2026-08-20T10:00:00") is not None


def test_parse_ts_zulu():
    dt = _parse_ts("2026-08-20T10:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_ts_invalid():
    assert _parse_ts("") is None
    assert _parse_ts("not-a-date") is None


# ── _list_missed_days 自然日切分 ──
def test_list_missed_days_basic():
    last = datetime(2026, 8, 18, 23, 0, 0)
    now = datetime(2026, 8, 21, 9, 0, 0)
    days = SummaryBackfill._list_missed_days(last, now, max_days=14)
    # 8/19, 8/20（今天 8/21 由正常调度覆盖，不补）
    assert [d.day for d in days] == [19, 20]


def test_list_missed_days_clamps_to_max():
    last = datetime(2026, 8, 1, 0, 0, 0)
    now = datetime(2026, 8, 21, 0, 0, 0)
    days = SummaryBackfill._list_missed_days(last, now, max_days=5)
    # 仅最近 5 天：8/16..8/20
    assert len(days) == 5
    assert [d.day for d in days] == [16, 17, 18, 19, 20]


def test_list_missed_days_no_gap():
    last = datetime(2026, 8, 20, 10, 0, 0)
    now = datetime(2026, 8, 20, 18, 0, 0)
    assert SummaryBackfill._list_missed_days(last, now, max_days=14) == []


# ── 首次运行：写基准时间，不补跑 ──
def test_first_run_writes_baseline_no_backfill():
    repo = _FakeRepo()
    store = _FakeStore(repo, meta={})  # 无 last_run_at
    agent = _FakeAgent()
    sched = _scheduler(store, agent)
    sched.run()
    assert store.get_meta("last_run_at") != ""  # 已写基准
    assert agent.calls == []  # 未调 LLM
    assert repo.upserts == []


# ── 停机跨天：按日补生成并写回 ──
def test_backfill_runs_for_missed_days():
    now = datetime(2026, 8, 21, 9, 0, 0)
    last_run = datetime(2026, 8, 19, 8, 0, 0)  # 停机 2 天
    # 昨天（8/20）有一条消息；前天（8/19）也补（last_run 次日=8/19 起）
    yesterday = datetime(2026, 8, 20, 14, 0, 0)
    msg = {
        "msg_id": "m1", "chat_id": "c1", "chat_type": "group",
        "sender_id": "s1", "sender_name": "张三", "content": "排期定了吗",
        "msg_type": "text", "timestamp": _local_iso(yesterday),
    }
    repo = _FakeRepo()
    repo.set_range({"c1": [msg]})
    store = _FakeStore(repo, meta={"last_run_at": _local_iso(last_run)})
    agent = _FakeAgent()
    cfg = SummaryBackfillConfig(min_messages_per_chat=1)
    sched = _scheduler(store, agent, cfg)
    sched.run(now=now)

    # 8/19、8/20 两个自然日都尝试补；8/20 有消息 → 调 LLM 且写回
    assert len(repo.upserts) == 1
    chat_id, summary, covered, plat = repo.upserts[0]
    assert chat_id == "c1"
    assert summary.startswith("【对话摘要】")
    # updated_at 标到昨天结束时刻，归到 8/20
    assert repo.updated_ats["c1"].startswith("2026-08-20T23:59:59")


# ── 不足 min_messages 跳过 ──
def test_backfill_skips_low_message_days():
    now = datetime(2026, 8, 21, 9, 0, 0)
    last_run = datetime(2026, 8, 19, 8, 0, 0)
    yesterday = datetime(2026, 8, 20, 14, 0, 0)
    # 仅 1 条消息，低于默认 min_messages_per_chat=3
    msg = {"msg_id": "m1", "chat_id": "c1", "chat_type": "group",
           "sender_id": "s1", "sender_name": "张三", "content": "hi",
           "msg_type": "text", "timestamp": _local_iso(yesterday)}
    repo = _FakeRepo()
    repo.set_range({"c1": [msg]})
    store = _FakeStore(repo, meta={"last_run_at": _local_iso(last_run)})
    agent = _FakeAgent()
    cfg = SummaryBackfillConfig(min_messages_per_chat=3)
    sched = _scheduler(store, agent, cfg)
    sched.run(now=now)
    assert agent.calls == []
    assert repo.upserts == []


# ── max_backfill_days 钳制 ──
def test_backfill_clamps_to_max_days():
    now = datetime(2026, 8, 25, 9, 0, 0)
    last_run = datetime(2026, 8, 1, 0, 0, 0)  # 停机 24 天
    # 仅 8/10 有一天消息
    day = datetime(2026, 8, 10, 12, 0, 0)
    msg = {"msg_id": "m1", "chat_id": "c1", "chat_type": "group",
           "sender_id": "s1", "sender_name": "张三", "content": "a",
           "msg_type": "text", "timestamp": _local_iso(day)}
    repo = _FakeRepo()
    repo.set_range({"c1": [msg]})
    store = _FakeStore(repo, meta={"last_run_at": _local_iso(last_run)})
    agent = _FakeAgent()
    cfg = SummaryBackfillConfig(max_backfill_days=5, min_messages_per_chat=1)
    sched = _scheduler(store, agent, cfg)
    sched.run(now=now)
    # 8/10 超出最近 5 天（8/20..8/24），不应补
    assert agent.calls == []
    assert repo.upserts == []
