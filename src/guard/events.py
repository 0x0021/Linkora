"""防护事件留存与聚合查询（供 Web 管理台展示）。

存储用独立 JSONL 文件 ``data/guard-events.jsonl``，不碰主库、不改任何
DB schema、不走 ``write_main_with_retry`` 写入闸门 —— 保证对既有业务流程零侵入。

- 写入：best-effort，文件不可写时只记日志，绝不抛到消息处理链路；
- 读取：内存环形缓冲（最新在前）+ 首次访问时从文件回填，重启不丢历史；
- 轮转：超过 ``max_events * 1.2`` 行时原子重写，避免无限增长；
- 隐私：事件只存内容预览（默认 200 字），且控制字符已可见化。

测试可用 ``set_events_path()`` 指向 tmp_path，``None`` 恢复默认。
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.paths import data_path

logger = logging.getLogger("linkora.guard")

_DEFAULT_PATH: Path = data_path("guard-events.jsonl")


class _EventState:
    """路径 / 缓冲 / 计数统一封装，避免模块级 global 散落。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.path: Path = _DEFAULT_PATH
        self.buffer: deque[dict[str, Any]] = deque()
        self.loaded_path: Path | None = None
        self.inspected_total = 0
        self.file_lines = 0


_state = _EventState()


def set_events_path(path: str | Path | None) -> None:
    """覆盖事件文件路径（测试用）；传 None 恢复默认并清空缓冲。"""
    with _state.lock:
        _state.path = Path(path) if path else _DEFAULT_PATH
        _state.buffer.clear()
        _state.loaded_path = None
        _state.inspected_total = 0
        _state.file_lines = 0


def get_events_path() -> Path:
    with _state.lock:
        return _state.path


def note_inspected(count: int = 1) -> None:
    """累计「已检测入站消息数」（含无风险消息，仅内存计数）。"""
    with _state.lock:
        _state.inspected_total += count


def _ensure_loaded() -> None:
    """首次访问时从文件回填最近若干行（调用方必须持锁）。"""
    if _state.loaded_path == _state.path:
        return
    _state.buffer.clear()
    _state.file_lines = 0
    path = _state.path
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            _state.file_lines = len(lines)
            for line in lines[-10000:]:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(record, dict):
                    _state.buffer.appendleft(record)
        except OSError as e:
            logger.warning("[guard] 读取防护事件文件失败: %s", e)
    _state.loaded_path = path


def _rewrite_if_needed(max_events: int) -> None:
    """超过软上限时原子重写文件（调用方必须持锁）。"""
    if _state.file_lines <= max_events * 1.2:
        return
    path = _state.path
    keep = list(_state.buffer)[:max_events]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for record in reversed(keep):
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        tmp.replace(path)
        _state.buffer = deque(keep)
        _state.file_lines = len(keep)
    except OSError as e:
        logger.warning("[guard] 轮转防护事件文件失败: %s", e)


def append_event(record: dict[str, Any], *, max_events: int = 5000) -> None:
    """追加一条事件（best-effort，永不抛异常到主链路）。"""
    with _state.lock:
        _ensure_loaded()
        _state.buffer.appendleft(record)
        if len(_state.buffer) > max_events:
            _state.buffer.pop()
        try:
            path = _state.path
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            _state.file_lines += 1
        except (OSError, TypeError, ValueError) as e:
            logger.warning("[guard] 写防护事件文件失败(best-effort): %s", e)
        _rewrite_if_needed(max_events)


def _in_window(ts: str, days: int) -> bool:
    if days <= 0:
        return True
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return True
    return parsed >= datetime.now(parsed.tzinfo) - timedelta(days=days)


def list_events(
    *,
    limit: int = 50,
    offset: int = 0,
    days: int = 7,
    risk: str = "",
    action: str = "",
    platform: str = "",
    keyword: str = "",
) -> tuple[list[dict[str, Any]], int]:
    """分页查询事件（最新在前）。返回 ``(当前页, 过滤后总数)``。"""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    kw = keyword.strip().lower()
    with _state.lock:
        _ensure_loaded()
        records = list(_state.buffer)

    matched: list[dict[str, Any]] = []
    for rec in records:
        if not _in_window(str(rec.get("ts", "")), days):
            continue
        if risk and rec.get("risk") != risk:
            continue
        if action and rec.get("action") != action:
            continue
        if platform and rec.get("platform") != platform:
            continue
        if kw:
            haystack = " ".join(
                str(rec.get(k, "")) for k in ("sender_name", "chat_name", "content_preview", "msg_id")
            ).lower()
            if kw not in haystack:
                continue
        matched.append(rec)
    return matched[offset: offset + limit], len(matched)


def stats(*, days: int = 7) -> dict[str, Any]:
    """聚合统计：总数、风险分布、处置分布、平台分布、命中信号、趋势、高频来源。"""
    with _state.lock:
        _ensure_loaded()
        records = list(_state.buffer)
        inspected_total = _state.inspected_total

    recent = [r for r in records if _in_window(str(r.get("ts", "")), days)]
    risks: Counter[str] = Counter(str(r.get("risk", "none")) for r in recent)
    actions: Counter[str] = Counter(str(r.get("action", "allow")) for r in recent)
    platforms: Counter[str] = Counter(str(r.get("platform", "")) for r in recent if r.get("platform"))
    signals: Counter[str] = Counter()
    senders: Counter[str] = Counter()
    timeline: Counter[str] = Counter()
    high_by_day: Counter[str] = Counter()

    for rec in recent:
        for sig in rec.get("signals") or []:
            signals[str(sig)] += 1
        name = str(rec.get("sender_name") or "").strip()
        if name:
            senders[name] += 1
        date = str(rec.get("date") or rec.get("ts", "")[:10])
        timeline[date] += 1
        if rec.get("risk") == "high":
            high_by_day[date] += 1

    return {
        "total": len(recent),
        "inspected_total": inspected_total,
        "blocked": sum(1 for r in recent if r.get("blocked")),
        "risk": dict(risks),
        "actions": dict(actions),
        "platforms": dict(platforms),
        "signals": dict(signals.most_common()),
        "top_senders": [{"name": k, "count": v} for k, v in senders.most_common(8)],
        "timeline": [
            {"date": d, "total": timeline[d], "high": high_by_day[d]}
            for d in sorted(timeline)
        ],
    }


__all__ = [
    "append_event",
    "get_events_path",
    "list_events",
    "note_inspected",
    "set_events_path",
    "stats",
]
