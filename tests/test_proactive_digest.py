from __future__ import annotations

from datetime import datetime, timedelta

from src.config_models import ProactiveConfig
from src.llm.proactive_digest import ProactiveDigestScheduler, build_digest


class _Row:
    def __init__(self, summary_text: str, updated_at: str) -> None:
        self.summary_text = summary_text
        self.updated_at = updated_at


class _FakeRepo:
    """模拟 ConversationRepo：list_recent_summaries 由构造时注入（含 since 过滤）。"""

    def __init__(self, recent_rows: list[dict] | None = None,
                 summaries: dict[str, _Row | None] | None = None) -> None:
        self._recent_rows = recent_rows or []
        self._summaries = summaries or {}
        self.send_calls: list[tuple] = []

    def get_recent_conversations(self, limit: int = 20, platform: str = "") -> list[dict]:
        return []

    def get_conversation_summary(self, chat_id: str, platform: str = "") -> _Row | None:
        return self._summaries.get(chat_id)

    def list_recent_summaries(self, limit: int = 20, platform: str = "",
                              since: str | None = None) -> list[dict]:
        out = []
        for r in self._recent_rows:
            if since and (r.get("updated_at") or "") < since:
                continue
            out.append(r)
        return out[:limit]


class _FakeStore:
    def __init__(self, repo: _FakeRepo) -> None:
        self._conversation_repo = repo


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def chat_message_send(self, **kwargs) -> dict:
        self.sent.append(kwargs)
        return {"ok": True}


def _scheduler(cfg: ProactiveConfig, repo: _FakeRepo, adapter: _FakeAdapter) -> ProactiveDigestScheduler:
    store = _FakeStore(repo)
    sched = ProactiveDigestScheduler(
        agent=None, store=store, adapter=adapter, config=cfg, platform="dingtalk",
    )
    return sched


# ── build_digest 纯函数 ──
def test_build_digest_empty():
    assert build_digest([]) == "（今日无新对话摘要）"


def test_build_digest_custom_title():
    items = [{"chat_name": "张三", "chat_id": "c1", "summary": "讨论了排期"}]
    out = build_digest(items, title="📋 近 24 小时对话摘要（共 1 段）")
    assert "近 24 小时对话摘要" in out
    assert "共 1 段" in out


def test_build_digest_truncates():
    items = [{"chat_name": "张三", "chat_id": "c1",
              "summary": "x" * 500}]
    out = build_digest(items, max_summary_chars=10)
    assert "x" * 10 + "…" in out
    assert "张三" in out


def test_build_digest_skips_missing_summary():
    items = [{"chat_name": "群A", "chat_id": "c1", "summary": ""}]
    out = build_digest(items)
    assert "（无摘要）" in out


# ── collect_items 回溯窗口过滤 ──
def test_collect_filters_by_lookback():
    now = datetime.now()
    rows = [
        {"chat_id": "old", "chat_name": "旧对话",
         "summary_text": "旧摘要", "updated_at": (now - timedelta(hours=30)).isoformat()},
        {"chat_id": "new", "chat_name": "新对话",
         "summary_text": "新摘要", "updated_at": now.isoformat()},
    ]
    repo = _FakeRepo(recent_rows=rows)
    adapter = _FakeAdapter()
    cfg = ProactiveConfig(enabled=True, lookback_hours=24,
                          owner_user_id="u1", max_conversations=10)
    sched = _scheduler(cfg, repo, adapter)
    items = sched.collect_items()
    ids = [i["chat_id"] for i in items]
    assert ids == ["new"]  # 旧对话（>24h 前）被 since 过滤


def test_collect_skips_no_summary():
    now = datetime.now()
    rows = [{"chat_id": "c1", "chat_name": "无摘要对话",
             "summary_text": "", "updated_at": now.isoformat()}]
    repo = _FakeRepo(recent_rows=rows)
    adapter = _FakeAdapter()
    cfg = ProactiveConfig(enabled=True, lookback_hours=24, owner_user_id="u1")
    sched = _scheduler(cfg, repo, adapter)
    assert sched.collect_items() == []


# ── _run_once 发送 ──
def test_run_once_sends_to_owner_user():
    now = datetime.now()
    rows = [{"chat_id": "c1", "chat_name": "项目群",
             "summary_text": "讨论了排期", "updated_at": now.isoformat()}]
    repo = _FakeRepo(recent_rows=rows)
    adapter = _FakeAdapter()
    cfg = ProactiveConfig(enabled=True, owner_user_id="u123", max_summary_chars=200)
    sched = _scheduler(cfg, repo, adapter)
    sched._run_once()
    assert len(adapter.sent) == 1
    kwargs = adapter.sent[0]
    assert kwargs["user"] == "u123"
    assert "项目群" in kwargs["text"]
    assert "讨论了排期" in kwargs["text"]


def test_run_once_falls_back_to_open_id():
    now = datetime.now()
    rows = [{"chat_id": "c1", "chat_name": "群",
             "summary_text": "摘要", "updated_at": now.isoformat()}]
    repo = _FakeRepo(recent_rows=rows)
    adapter = _FakeAdapter()
    cfg = ProactiveConfig(enabled=True, owner_open_dingtalk_id="ou_abc")
    sched = _scheduler(cfg, repo, adapter)
    sched._run_once()
    assert adapter.sent[0]["open_dingtalk_id"] == "ou_abc"


# ── start 门控 ──
def test_start_noop_when_disabled():
    repo = _FakeRepo([], {})
    adapter = _FakeAdapter()
    cfg = ProactiveConfig(enabled=False)
    sched = _scheduler(cfg, repo, adapter)
    sched.start()
    assert sched._thread is None  # 未启动线程


def test_start_noop_when_owner_missing():
    repo = _FakeRepo([], {})
    adapter = _FakeAdapter()
    cfg = ProactiveConfig(enabled=True)  # owner 未配置
    sched = _scheduler(cfg, repo, adapter)
    sched.start()
    assert sched._thread is None
