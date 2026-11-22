"""工作时间 / 工作日 / 节假日判定。

全部为纯函数，便于单测。时间来源有两种：
- 消息 ``timestamp``：DB 里的 ISO naive 字符串（本地时区，形如 ``2026-11-20T17:37:11``），
  也可能带 ``Z`` / 微秒 / 显式 offset，统一按 ``cfg.timezone`` 解释；
- ``datetime.now()``：调度器传入的当前时刻，默认取系统本地时间。

判定时段以**消息到达的本地时间**为准（不是 UTC），符合"工作时间"的业务语义。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config_models.system import UnrepliedTodoConfig

logger = logging.getLogger(__name__)

# 判定结果：空串 = 工作时间内（不建待办）
REASON_OFF_HOURS = "off_hours"   # 工作日但在工作时间外（含午休外/夜晚）
REASON_WEEKEND = "weekend"       # 非工作日（周末或配置的休息日）
REASON_HOLIDAY = "holiday"       # 法定节假日（显式日期，覆盖 workdays）


def resolve_tz(name: str) -> ZoneInfo:
    """按 IANA 名称取时区；非法名称回退 Asia/Shanghai（fail-safe）。"""
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError, OSError):
        logger.warning("[未回复待办] 非法时区 %s，回退 Asia/Shanghai", name)
        return ZoneInfo("Asia/Shanghai")


def parse_message_ts(value: str, tz: ZoneInfo) -> datetime | None:
    """解析消息时间戳到带时区 datetime；失败返回 None（调用方跳过该条）。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # 历史数据用本地时间写入（naive），按配置时区解释；不擅自当 UTC 处理，
        # 否则东八区会整体偏移 8 小时，把白天的消息误判为工作时间外。
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _parse_hhmm(value: str, fallback: time) -> time:
    text = str(value or "").strip()
    try:
        hour, minute = text.split(":", 1)
        h, m = int(hour), int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return time(h, m)
    except (ValueError, TypeError):
        pass
    return fallback


def _parse_weekdays(value: str) -> frozenset[int]:
    """解析 "1,2,3,4,5"（ISO 星期 1=周一…7=周日）；空/非法回退周一至周五。"""
    out: set[int] = set()
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            day = int(part)
        except ValueError:
            continue
        if 1 <= day <= 7:
            out.add(day)
    return frozenset(out) if out else frozenset({1, 2, 3, 4, 5})


def _parse_holidays(value: str) -> frozenset[str]:
    out: set[str] = set()
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(datetime.strptime(part, "%Y-%m-%d").date().isoformat())
        except ValueError:
            logger.warning("[未回复待办] 忽略非法节假日日期: %s", part)
    return frozenset(out)


def classify(ts: datetime, cfg: UnrepliedTodoConfig) -> str:
    """判定一条消息是否落在"工作时间外"，返回原因码（空串=工作时间内）。

    Args:
        ts: 已带时区的消息时间（用 :func:`parse_message_ts` 得到）。
        cfg: 工作时段配置。
    """
    local = ts.astimezone(resolve_tz(cfg.timezone))
    day = local.date()

    if day.isoformat() in _parse_holidays(cfg.holidays):
        return REASON_HOLIDAY
    if local.isoweekday() not in _parse_weekdays(cfg.workdays):
        return REASON_WEEKEND

    start = _parse_hhmm(cfg.work_start, time(9, 0))
    end = _parse_hhmm(cfg.work_end, time(18, 0))
    clock = local.time().replace(tzinfo=None)
    if start <= end:
        in_work = start <= clock < end
    else:
        # 跨午夜班组（如 22:00→06:00）
        in_work = clock >= start or clock < end
    return "" if in_work else REASON_OFF_HOURS


def is_within_lookback(ts: datetime, now: datetime, lookback_hours: int) -> bool:
    """判断消息是否落在回看窗口内；带时区统一到 UTC 比较，避免 naive/aware 混比。"""
    def _utc(d: datetime) -> datetime:
        return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)

    delta = _utc(now) - _utc(ts)
    return timedelta(0) <= delta <= timedelta(hours=max(1, lookback_hours))
