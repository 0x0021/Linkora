"""脆弱消息逻辑契约测试：锁死历史事故修复，防上游改动打穿。

覆盖：
- _is_same_physical_message：同内容 + 时间窗判重复（防重复回复），超窗判真实连发（放行）。
- _norm_ws：去前导 # 标题符 + 空白归一化（防 AI 自回被误判为伪真人消息）。
- apply_history_tiering：约定输入为 ASC（旧→新），取 history[-max_recent:] 为最新切片。
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.llm.history import apply_history_tiering
from src.platform.message_loop import _is_same_physical_message
from src.poller_core_dedup import _norm_ws


class _Msg:
    def __init__(self, content, timestamp=None, chat_id="c1"):
        self.content = content
        self.timestamp = timestamp
        self.chat_id = chat_id


class _Agent:
    _history_tiering_recent = 3
    _summary_min_older = 1000  # 足够大，确保不走摘要分支，纯测 ASC 切片契约
    _summary_max_age_seconds = 86400
    _summary_min_coverage_ratio = 0.8
    store = None

    def _maybe_schedule_summary(self, *a, **k):
        pass


def test_diff_content_not_duplicate():
    a = _Msg("在吗")
    b = _Msg("收到")
    assert _is_same_physical_message(a, b) is False


def test_same_content_within_window_is_duplicate():
    t = datetime(2026, 1, 1, 12, 0, 0)
    a = _Msg("在吗", t)
    b = _Msg("在吗", datetime(2026, 1, 1, 12, 0, 1))
    assert _is_same_physical_message(a, b) is True


def test_same_content_beyond_window_is_real_resend():
    t = datetime(2026, 1, 1, 12, 0, 0)
    a = _Msg("在吗", t)
    b = _Msg("在吗", datetime(2026, 1, 1, 12, 0, 10))
    assert _is_same_physical_message(a, b) is False


def test_missing_timestamp_conservative_duplicate():
    a = _Msg("在吗", None)
    b = _Msg("在吗", None)
    assert _is_same_physical_message(a, b) is True


def test_tz_mismatch_conservative_duplicate():
    a = _Msg("在吗", datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
    b = _Msg("在吗", datetime(2026, 1, 1, 12, 0, 0))  # naive
    assert _is_same_physical_message(a, b) is True


def test_norm_ws_strips_heading_and_normalizes_ws():
    assert _norm_ws("## 标题") == "标题"
    assert _norm_ws("  hello   world  ") == "hello world"
    assert _norm_ws("") == ""


def test_apply_history_tiering_keeps_newest_when_asc():
    # ASC：旧→新 [m0..m4]，max_recent=3 → 取最新 3 条 [m2, m3, m4]
    hist = [_Msg(f"m{i}") for i in range(5)]
    out = apply_history_tiering(_Agent(), hist, max_recent=3)
    assert [m.content for m in out] == ["m2", "m3", "m4"]
    # 顺序保持 ASC（旧→新），不被反转
    assert out[0].content == "m2" and out[-1].content == "m4"


def test_apply_history_tiering_passthrough_when_short():
    hist = [_Msg(f"m{i}") for i in range(2)]
    out = apply_history_tiering(_Agent(), hist, max_recent=5)
    assert [m.content for m in out] == ["m0", "m1"]
