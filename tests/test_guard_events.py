"""防护事件存储：JSONL 持久化、分页过滤、统计聚合、滚动轮转。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.guard import events


def _record(i: int, **kw) -> dict:
    now = datetime.now().astimezone() - timedelta(minutes=i)
    base = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "event_id": f"e{i}",
        "platform": "dingtalk",
        "chat_name": "研发群",
        "sender_name": "张三" if i % 2 == 0 else "李四",
        "risk": "high" if i % 3 == 0 else "low",
        "signals": ["role_spoofing"] if i % 3 == 0 else ["control_chars"],
        "action": "isolated" if i % 3 == 0 else "detected",
        "action_label": "已清洗并隔离" if i % 3 == 0 else "已记录，正文零改写",
        "blocked": False,
        "content_preview": f"消息 {i}",
        "char_count": 10,
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _isolated(tmp_path):
    events.set_events_path(tmp_path / "events.jsonl")
    yield
    events.set_events_path(None)


class TestListEvents:
    def test_newest_first_and_pagination(self) -> None:
        for i in range(10):
            events.append_event(_record(i))
        page, total = events.list_events(limit=3, offset=0, days=0)
        assert total == 10
        # appendleft：最后写入的排最前
        assert [r["event_id"] for r in page] == ["e9", "e8", "e7"]
        page2, _ = events.list_events(limit=3, offset=3, days=0)
        assert [r["event_id"] for r in page2] == ["e6", "e5", "e4"]

    def test_limit_is_clamped(self) -> None:
        events.append_event(_record(0))
        page, _ = events.list_events(limit=99999, days=0)
        assert len(page) == 1
        assert events.list_events(limit=-5, days=0)[0] != []

    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({"risk": "high"}, 4),
            ({"action": "detected"}, 6),
            ({"platform": "feishu"}, 0),
            ({"keyword": "张三"}, 5),
        ],
    )
    def test_filters(self, filters: dict, expected: int) -> None:
        for i in range(10):
            events.append_event(_record(i))
        _, total = events.list_events(days=0, **filters)
        assert total == expected

    def test_days_window_excludes_old(self) -> None:
        events.append_event(_record(0))
        old = _record(1, ts=(datetime.now().astimezone() - timedelta(days=30)).isoformat(),
                      date=(datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
                      event_id="old")
        events.append_event(old)
        assert events.list_events(days=7)[1] == 1
        assert events.list_events(days=0)[1] == 2

    def test_empty_store(self) -> None:
        assert events.list_events(days=7) == ([], 0)


class TestPersistence:
    def test_reload_from_file_after_path_reset(self, tmp_path) -> None:
        path = tmp_path / "events.jsonl"
        events.set_events_path(path)
        events.append_event(_record(0))
        events.append_event(_record(1))
        # 模拟进程重启：清空内存态但指向同一文件
        events.set_events_path(None)
        events.set_events_path(path)
        items, total = events.list_events(days=0)
        assert total == 2
        assert items[0]["event_id"] == "e1"  # 文件尾行 = 最新，回填后在前

    def test_rotation_caps_file_growth(self) -> None:
        for i in range(60):
            events.append_event(_record(i), max_events=20)
        lines = events.get_events_path().read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 24  # 软阈值 max_events * 1.2
        for line in lines:
            json.loads(line)

    def test_broken_lines_are_skipped(self) -> None:
        path = events.get_events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json\n" + json.dumps(_record(0)) + "\n", encoding="utf-8")
        events.set_events_path(None)
        events.set_events_path(path)
        assert events.list_events(days=0)[1] == 1


class TestStats:
    def test_aggregations(self) -> None:
        for i in range(9):
            events.append_event(_record(i))
        events.note_inspected(100)
        s = events.stats(days=0)
        assert s["total"] == 9
        assert s["inspected_total"] == 100
        assert s["risk"]["high"] == 3
        assert s["risk"]["low"] == 6
        assert s["actions"]["isolated"] == 3
        assert s["signals"]["role_spoofing"] == 3
        assert s["platforms"]["dingtalk"] == 9
        assert len(s["timeline"]) >= 1
        assert s["top_senders"][0]["name"] in ("张三", "李四")

    def test_empty_stats(self) -> None:
        s = events.stats(days=7)
        assert s["total"] == 0
        assert s["risk"] == {}
        assert s["timeline"] == []
