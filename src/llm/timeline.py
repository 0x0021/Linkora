"""对话时间线标注：把「消息何时说的」「相邻两条隔了多久」暴露给 LLM。

**为什么不在这里做话题分类**：
    判断「这两句话是不是同一件事」是 LLM 的强项。用正则 / 关键词写话题分类器
    只会误杀正常业务消息（2026-08 已踩过一次：「老数据」含 business 词被意图
    分类短路）。本模块只负责提供**客观时间事实**，话题边界交给模型自己判断。

产出三类标注：

- :func:`format_time_label`  单条消息的可读时间（``今天 14:23`` / ``昨天 09:10``
  / ``08-05 16:40``），让模型知道每句话是什么时候说的。
- :func:`gap_notice`         历史相邻两条间隔超阈值时，插入一句自然语言分隔提示。
- :func:`incoming_gap_notice` 当前这条新消息距最后一条历史超阈值时的提示，
  提醒模型先判断是否同一件事，别把上文的待办 / 索取信息带进来。

阈值语义与 ``history_session_gap_minutes``（默认 360 分钟）不同：
后者是**硬切分**——超过就把更早的历史整段丢掉；本模块阈值（默认 30 分钟）
是**软提示**——历史照留，只是告诉模型「这里可能换话题了」，判断权归模型。
"""

from __future__ import annotations

from datetime import datetime

# 话题软断层阈值（分钟）：相邻消息间隔超过它，就在上下文里插入分隔提示。
# 取 30 分钟的依据：IT 支持类会话里，同一件事的往返通常在几分钟内；
# 隔了半小时以上再开口，多半是新起一摊事（但仍由 LLM 最终判断）。
DEFAULT_TOPIC_GAP_MINUTES = 30


def format_time_label(ts: object, now: datetime | None = None) -> str:
    """把时间戳格式化成人类可读标签。

    返回示例：``今天 14:23`` / ``昨天 09:10`` / ``前天 18:05`` / ``08-05 16:40``。
    ``ts`` 不是 datetime（None / 字符串 / 脏数据）时返回空串——调用方据此跳过标注，
    绝不因为时间字段异常而影响主回复。
    """
    if not isinstance(ts, datetime):
        return ""
    ref = now if isinstance(now, datetime) else datetime.now()
    hm = ts.strftime("%H:%M")
    try:
        delta_days = (ref.date() - ts.date()).days
    except (AttributeError, TypeError, ValueError):
        return ""
    if delta_days == 0:
        return f"今天 {hm}"
    if delta_days == 1:
        return f"昨天 {hm}"
    if delta_days == 2:
        return f"前天 {hm}"
    return f"{ts.strftime('%m-%d')} {hm}"


def humanize_gap(seconds: float) -> str:
    """把秒数转成中文口语时长：``12 分钟`` / ``3 小时 20 分钟`` / ``2 天 5 小时``。"""
    total_minutes = int(max(0, seconds) // 60)
    if total_minutes < 60:
        return f"{total_minutes} 分钟"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
    days, rem_hours = divmod(hours, 24)
    return f"{days} 天" + (f" {rem_hours} 小时" if rem_hours else "")


def _gap_seconds(prev_ts: object, cur_ts: object) -> float | None:
    """计算两个时间戳的间隔秒数；任一非法或 tz 混用则返回 None（放弃标注）。"""
    if not isinstance(prev_ts, datetime) or not isinstance(cur_ts, datetime):
        return None
    try:
        seconds = (cur_ts - prev_ts).total_seconds()
    except TypeError:
        # tz-aware 与 naive 相减会抛 TypeError；标注是锦上添花，静默放弃即可。
        return None
    return seconds if seconds > 0 else None


def gap_notice(
    prev_ts: object,
    cur_ts: object,
    *,
    threshold_minutes: int = DEFAULT_TOPIC_GAP_MINUTES,
) -> str | None:
    """历史相邻两条消息间隔超阈值时，返回一句分隔提示；否则 ``None``。

    提示以 system 消息形式插在两段历史之间，模型不会把它当成可模仿的输出格式。
    """
    if threshold_minutes <= 0:
        return None
    seconds = _gap_seconds(prev_ts, cur_ts)
    if seconds is None or seconds < threshold_minutes * 60:
        return None
    return (
        f"—— 上面的对话到此中断，隔了 {humanize_gap(seconds)} 才有下面的消息，"
        "很可能已经换了一件事。请分别看待，不要把上下两段当成同一个话题 ——"
    )


def incoming_gap_notice(
    prev_ts: object,
    cur_ts: object,
    *,
    threshold_minutes: int = DEFAULT_TOPIC_GAP_MINUTES,
) -> str | None:
    """当前新消息距最后一条历史超阈值时，返回提示；否则 ``None``。

    这是防「串味」的最后一道提示：对方隔了很久才开口，多半是新的事，
    此时若模型还惦记着上文没办完的事（比如继续索要工号手机号），就会答非所问。
    """
    if threshold_minutes <= 0:
        return None
    seconds = _gap_seconds(prev_ts, cur_ts)
    if seconds is None or seconds < threshold_minutes * 60:
        return None
    return (
        f"—— 距上一条消息已过去 {humanize_gap(seconds)}，下面是对方刚发来的新消息。"
        "请先判断它和上文是不是同一件事：如果不是，就只回答这条消息本身，"
        "不要延续上文的话题，也不要再索要上文提到过的信息 ——"
    )
