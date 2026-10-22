"""话题(Thread)上下文装配：把话题内的对话补进 LLM 历史。

背景（2026-10-22）：P0-1 让 Linkora 能**回复**进钉钉话题，但随即暴露一个不对称——
**读不到**话题里的对话。LLM 的历史来自 ``get_conversation_history(chat_id)``，
即**主群流**；用户在话题下讨论的内容不进主群流，于是 AI 在话题里回复时
「不知道大家在聊什么」，只能看到触发它的那一条消息。

本模块提供 ``fetch_thread_context``：入站消息带 ``thread_id``（openConvThreadId）时，
调 ``dws chat +thread-replies`` 拉取话题内回复，组装成 ``Message`` 列表，
由调用方并入历史。

设计约束：
- **降级不阻断**：拉取失败 / 平台不支持 → 返回空列表，主回复流程照常（绝不因
  话题上下文缺失而让整条消息回复失败）；
- **不落库**：话题上下文是「当轮补的视角」，不写入 messages 表——否则会把
  话题内容重复计入主群流历史，污染后续所有轮次的上下文；
- **不重复计数**：同一条消息可能既在主群流又在话题里，按 msg_id 去重。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.im_adapter.errors import IMAdapterError
from src.models import Message

logger = logging.getLogger("src.platform.thread_context")

# 话题上下文最多注入条数（防止超长话题把 LLM 上下文撑爆）
MAX_THREAD_CONTEXT_MESSAGES = 20

# dws createTime 常见格式
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def _parse_time(value: str) -> datetime:
    """解析 dws 的时间字段；失败回退当前时间（宁可时间不准，也不要崩）。"""
    s = (value or "").strip()
    if not s:
        return datetime.now()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromtimestamp(float(s))
    except (ValueError, OverflowError, OSError):
        return datetime.now()


def fetch_thread_context(
    adapter: Any,
    message: Message,
    *,
    limit: int = MAX_THREAD_CONTEXT_MESSAGES,
    page_all: bool = True,
) -> list[Message]:
    """拉取入站消息所属话题的对话，组装成 ``Message`` 列表。

    Args:
        adapter: IM 适配器（需实现 ``chat_thread_replies``）。
        message: 入站消息；``thread_id`` 为空时直接返回空列表。
        limit: 最多注入条数。
        page_all: 是否拉全量话题回复（再截断到 limit）。

    Returns:
        按时间**正序**（旧→新）的 ``Message`` 列表；无话题 / 不支持 / 失败时返回 ``[]``。
    """
    thread_id = (getattr(message, "thread_id", "") or "").strip()
    if not thread_id:
        return []

    getter = getattr(adapter, "chat_thread_replies", None)
    if not callable(getter):
        return []

    try:
        replies: Any = getter(
            thread_id=thread_id,
            group=getattr(message, "chat_id", "") or "",
            limit=max(1, limit),
            page_all=page_all,
        )
    except (IMAdapterError, ValueError, OSError, RuntimeError) as e:
        # 精确捕获：CLI 失败 / 参数错误 / IO / 适配器内部错误 → 降级为「无话题上下文」。
        # 其余异常属 Bug，上抛（不静默吞）。
        logger.warning("[话题上下文] 拉取话题回复失败，降级为无话题上下文: %s", e)
        return []
    if not isinstance(replies, list):
        return []

    out: list[Message] = []
    for r in replies:
        if not isinstance(r, dict):
            continue
        text = (r.get("text") or "").strip()
        if not text:
            continue
        out.append(Message(
            msg_id=r.get("message_id") or "",
            chat_id=getattr(message, "chat_id", "") or "",
            chat_type=getattr(message, "chat_type", "") or "group",
            chat_name=getattr(message, "chat_name", None),
            sender_id="",
            sender_name=r.get("sender") or "",
            content=text,
            msg_type="text",
            timestamp=_parse_time(r.get("timestamp") or ""),
            raw={},
            role="user",
            thread_id=thread_id,
        ))
        if len(out) >= limit:
            break

    # 按时间正序（旧→新）——与主群流历史的 ASC 契约一致，便于并入时序对齐。
    out.sort(key=lambda m: m.timestamp)
    if out:
        logger.info("[话题上下文] 已注入 %d 条话题消息（thread_id=%s）",
                    len(out), thread_id[:24])
    return out


def merge_thread_context(
    history: list[Message],
    thread_msgs: list[Message],
) -> list[Message]:
    """把话题上下文并入主群流历史。

    - 按 ``msg_id`` 去重（同一消息可能既在主群流又在话题里）；
    - 保持**时间正序**（旧→新），与 ``_normalize_history_asc`` 的契约一致；
    - 话题消息**不落库**，仅当轮生效。
    """
    if not thread_msgs:
        return history
    if not history:
        return list(thread_msgs)

    seen = {m.msg_id for m in history if m.msg_id}
    merged = list(history)
    for m in thread_msgs:
        # msg_id 为空的话题消息无法去重，按内容+时间粗略去重
        if m.msg_id:
            if m.msg_id in seen:
                continue
            seen.add(m.msg_id)
        else:
            key = (m.sender_name, m.content, m.timestamp)
            if any((h.sender_name, h.content, h.timestamp) == key for h in merged):
                continue
        merged.append(m)

    merged.sort(key=lambda m: m.timestamp)
    return merged
