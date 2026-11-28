"""同一会话连续留言合并成一条待办（摘要口径 + 回退 + 幂等）。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from src.config_models.system import UnrepliedTodoConfig
from src.offhours_todo.scanner import ScanResult, UnansweredItem
from src.offhours_todo.service import OffhoursTodoService

TZ = ZoneInfo("Asia/Shanghai")
CHAT = "cid-thread-test"


def _item(msg_id: str, ts: str, content: str, *, sender: str = "小雅") -> UnansweredItem:
    return UnansweredItem(
        platform="wecom", chat_id=CHAT, chat_name="单聊", chat_type="single",
        msg_id=msg_id, sender_id="peer-1", sender_name=sender,
        content_preview=content, timestamp=ts, reason="weekend",
        dedupe_key=f"wecom:{CHAT}:{msg_id}",
    )


class _FakeLLM:
    def __init__(self, answer: str = "电脑开不了机，需要本人上门帮忙", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls = 0

    def chat(self, messages: list[dict], **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.answer)


class _FakeRepo:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.unanswered_updates: list[tuple[str, str, str]] = []
        self.keys: set[str] = set()

    def has_todo_record(self, dedupe_key: str) -> bool:
        return dedupe_key in self.keys

    def insert_todo_record(self, **kwargs: object) -> bool:
        self.records.append(dict(kwargs))
        self.keys.add(str(kwargs["dedupe_key"]))
        return True

    def update_unanswered_status(self, dedupe_key: str, status: str, *, detail: str = "") -> bool:
        self.unanswered_updates.append((dedupe_key, status, detail))
        return True

    def mark_owner_notified(self, keys: list[str]) -> int:
        return len(keys)


def _run(items: list[UnansweredItem], llm: _FakeLLM) -> tuple[dict, _FakeRepo, list[str]]:
    repo = _FakeRepo()
    ctx = SimpleNamespace(store=SimpleNamespace(_offhours_todo_repo=repo), dws=MagicMock())
    cfg = UnrepliedTodoConfig(enabled=True, notify_sender=False, notify_owner=False)
    service = OffhoursTodoService(cfg, SimpleNamespace(llm_client=llm), {"wecom": ctx})
    result = ScanResult(platform="wecom", scanned=len(items), offhours=len(items),
                        pending_new=len(items), items=items)
    notices: list[str] = []
    with patch("src.offhours_todo.service.scan_platform", return_value=result):
        out = service._scan_one("wecom", ctx, create_todos=True, dry_run=False, notices=notices)
    return out, repo, notices


def test_consecutive_messages_become_one_summarized_todo():
    items = [
        _item("m1", "2026-11-28T10:40:46", "我电脑坏了 帮我看看"),
        _item("m2", "2026-11-28T10:42:49", "开不了机"),
        _item("m3", "2026-11-28T10:44:04", "我不会 你过来帮我弄吧"),
    ]
    out, repo, notices = _run(items, _FakeLLM())

    assert out["notify_only"] == 1          # 只建一条，不再三条轰炸
    assert len(repo.records) == 1
    assert "电脑开不了机" in repo.records[0]["title"]
    assert notices and "电脑开不了机" in notices[0]
    merged = [u for u in repo.unanswered_updates if u[1] == "ignored" and "合并到待办" in u[2]]
    assert len(merged) == 2


def test_messages_outside_window_are_separate_todos():
    items = [
        _item("m1", "2026-11-28T10:40:46", "电脑坏了"),
        _item("m2", "2026-11-28T11:10:46", "另外帮我申请 VPN"),
    ]
    _out, repo, _notices = _run(items, _FakeLLM())

    assert len(repo.records) == 2
    assert not any(u[1] == "ignored" for u in repo.unanswered_updates)


def test_different_senders_are_never_merged():
    items = [
        _item("m1", "2026-11-28T10:40:46", "电脑坏了"),
        _item("m2", "2026-11-28T10:41:46", "帮我打印文件", sender="小文"),
    ]
    _out, repo, _notices = _run(items, _FakeLLM())

    assert len(repo.records) == 2


def test_invalid_summary_falls_back_to_joined_messages():
    items = [
        _item("m1", "2026-11-28T10:40:46", "我电脑坏了 帮我看看"),
        _item("m2", "2026-11-28T10:42:49", "开不了机"),
    ]
    _out, repo, _notices = _run(items, _FakeLLM(answer="明天帮你搞定，请稍候"))

    assert len(repo.records) == 1
    title = repo.records[0]["title"]
    assert "我电脑坏了 帮我看看；开不了机" in title
    assert "明天" not in title and "请稍候" not in title


def test_llm_failure_does_not_block_todo():
    items = [
        _item("m1", "2026-11-28T10:40:46", "电脑坏了"),
        _item("m2", "2026-11-28T10:42:49", "开不了机"),
    ]
    out, repo, _notices = _run(items, _FakeLLM(error=RuntimeError("model unavailable")))

    assert out["notify_only"] == 1
    assert len(repo.records) == 1
    assert "电脑坏了；开不了机" in repo.records[0]["title"]
