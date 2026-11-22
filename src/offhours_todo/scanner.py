"""按平台 + 会话扫描时间窗消息，筛出工作时间外且本人尚未回复的消息。

判定口径（坤哥 2026-11-22 明确）：

- **工作时间外**：工作日但在 work_start/work_end 之外、周末/休息日、显式节假日；
- **本人尚未回复**：满足以下任一即为未回复：
  1. 该会话当前在平台侧有未读消息（钉钉 ``list-unread-conversations``）；
  2. 该消息之后**没有本人真人回复**（``sender_id ∈ self_ids AND is_bot = 0``）。
     AI 代发的回复（``is_bot = 1``）**不算**本人回复。

两者是「或」关系：已读但只有 AI 回过、本人没亲自回的消息，依然要建待办——这是本
功能最核心的场景。平台未读列表拿不到时（飞书/企微无此接口），退化为仅用口径 2，
不阻断扫描。
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from src.config_models.system import UnrepliedTodoConfig
from src.im_adapter.errors import IMAdapterError
from src.offhours_todo.worktime import (
    classify,
    is_within_lookback,
    parse_message_ts,
    resolve_tz,
)
from src.self_identity import collect_self_ids

if TYPE_CHECKING:
    from src.platform.base import PlatformContext

logger = logging.getLogger(__name__)

# 这些消息类型不进待办（与 fetch_messages_in_range 同口径）
_SKIP_MSG_TYPES = ("system", "recall", "revoke")


@dataclass
class UnansweredItem:
    """一条待建单的未回复消息。"""

    platform: str
    chat_id: str
    chat_name: str
    chat_type: str
    msg_id: str
    sender_id: str
    sender_name: str
    content_preview: str
    timestamp: str
    reason: str
    dedupe_key: str
    unread: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanResult:
    """单平台扫描结果。"""

    platform: str = ""
    scanned: int = 0
    offhours: int = 0
    pending_new: int = 0
    replied_backfilled: int = 0
    items: list[UnansweredItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _fetch_unread_chats(adapter: Any, platform: str) -> set[str] | None:
    """取平台当前未读会话 ID 集合；不支持/失败返回 None（表示"未知"）。

    只有钉钉 CLI 暴露了未读会话列表；飞书/企微返回 None，扫描退化为「无真人回复」
    单一判据。失败非致命，仅 debug 日志。
    """
    if platform != "dingtalk" or adapter is None:
        return None
    fn = getattr(adapter, "chat_message_list_unread_conversations", None)
    if not callable(fn):
        return None
    try:
        raw_rows = fn(count=200)
        rows = raw_rows if isinstance(raw_rows, (list, tuple)) else []
    except (
        IMAdapterError, OSError, ValueError, TypeError, RuntimeError,
        KeyError, subprocess.SubprocessError,
    ) as e:
        logger.warning("[未回复待办] 未读会话列表不可用（降级为无真人回复判定）: %s", e)
        return None

    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("conversationId", "conversation_id", "chatId", "chat_id", "openConversationId"):
            val = row.get(key)
            if isinstance(val, str) and val:
                out.add(val)
                break
    return out


def _self_reply_times(
    rows: list[dict[str, Any]], self_ids: frozenset[str] | set[str], tz
) -> dict[str, list[datetime]]:
    """按会话收集「本人真人回复」的时间点（is_bot=0 且 sender_id 命中自身）。"""
    out: dict[str, list[datetime]] = {}
    for row in rows:
        if int(row.get("is_bot") or 0) != 0:
            continue
        sid = str(row.get("sender_id") or "").strip()
        if not sid or sid not in self_ids:
            continue
        ts = parse_message_ts(str(row.get("timestamp") or ""), tz)
        if ts is not None:
            out.setdefault(str(row.get("chat_id") or ""), []).append(ts)
    return out


def scan_platform(
    cfg: UnrepliedTodoConfig,
    platform: str,
    ctx: "PlatformContext",
    *,
    self_ids: frozenset[str] | set[str],
    lookback_hours: int | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """扫描单平台当前账号会话库，返回新增的 pending 项（已落库）。

    DB 操作非致命：查询失败返回带 errors 的空结果，不抛给调度器。
    """
    result = ScanResult(platform=platform)
    store = ctx.store
    adapter = ctx.dws
    if store is None:
        result.errors.append("平台 store 不可用")
        return result

    tz = resolve_tz(cfg.timezone)
    now = now or datetime.now(tz)
    hours = int(lookback_hours or cfg.lookback_hours)
    # 会话库 timestamp 为本地 naive 字符串；字符串比较放宽 1 天边界，
    # 精确窗口在 Python 侧用 is_within_lookback 判定，规避微秒/Z 格式坑。
    start_str = (now - timedelta(hours=hours + 24)).replace(tzinfo=None).isoformat(timespec="seconds")
    end_str = now.replace(tzinfo=None).isoformat(timespec="seconds")
    skip_placeholders = ",".join("?" for _ in _SKIP_MSG_TYPES)

    # 取未读会话（可能为 None = 未知，退化为仅用真人回复判定）
    unread_chats = _fetch_unread_chats(adapter, platform)

    try:
        cur = store._conversation_repo._cc(platform).cursor()
        cur.execute(
            f"""SELECT msg_id, chat_id, chat_type, sender_id, sender_name,
                       content, msg_type, timestamp, is_bot, role
                FROM messages
                WHERE timestamp >= ? AND timestamp < ?
                  AND is_withdrawn = 0
                  AND (msg_type IS NULL OR msg_type NOT IN ({skip_placeholders}))
                ORDER BY chat_id, timestamp ASC""",
            (start_str, end_str, *_SKIP_MSG_TYPES),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    except (sqlite3.Error, AttributeError) as e:
        logger.warning("[未回复待办] 平台 %s 扫描消息失败: %s", platform, e)
        result.errors.append(f"扫描消息失败: {e}")
        return result

    result.scanned = len(rows)
    replies_by_chat = _self_reply_times(rows, self_ids, tz)
    repo = store._offhours_todo_repo

    # 回填存量 pending：会话里出现真人回复后，回复时间点之前的记录不再算未回复。
    for chat_id, reply_times in replies_by_chat.items():
        try:
            result.replied_backfilled += repo.mark_replied_later(
                chat_id=chat_id, up_to_ts=min(reply_times).isoformat(timespec="seconds")
            )
        except (sqlite3.Error, ValueError) as e:
            logger.debug("[未回复待办] 回填会话 %s 失败: %s", chat_id, e)

    for row in rows:
        if int(row.get("is_bot") or 0) != 0:
            continue  # AI 代发 / 机器人消息，不是「对方发来的消息」
        sid = str(row.get("sender_id") or "").strip()
        if sid and sid in self_ids:
            continue  # 本人真人消息（不是待回复对象）
        ts = parse_message_ts(str(row.get("timestamp") or ""), tz)
        if ts is None or not is_within_lookback(ts, now, hours):
            continue

        reason = classify(ts, cfg)
        if not reason:
            continue
        result.offhours += 1

        chat_id = str(row.get("chat_id") or "")
        msg_id = str(row.get("msg_id") or "")
        if not chat_id or not msg_id:
            continue

        # 「未读」或「无真人回复」任一成立 → 本人尚未回复
        is_unread = unread_chats is not None and chat_id in unread_chats
        has_human_reply = any(r > ts for r in replies_by_chat.get(chat_id, []))
        if has_human_reply and not is_unread:
            continue

        dedupe_key = f"{platform}:{chat_id}:{msg_id}"
        detail: dict[str, Any] = {
            "unread": is_unread,
            "human_reply_after": has_human_reply,
            "unread_source": "platform" if unread_chats is not None else "unavailable",
        }
        inserted = repo.upsert_unanswered(
            platform=platform,
            chat_id=chat_id,
            msg_id=msg_id,
            dedupe_key=dedupe_key,
            sender_id=sid,
            sender_name=str(row.get("sender_name") or ""),
            chat_name="",  # 会话名由 Web 侧 JOIN conversations 补齐，避免热路径额外查询
            chat_type=str(row.get("chat_type") or ""),
            content_preview=str(row.get("content") or ""),
            msg_timestamp=str(row.get("timestamp") or ""),
            reason=reason,
            detail=str(detail),
        )
        if inserted:
            result.pending_new += 1
            result.items.append(
                UnansweredItem(
                    platform=platform,
                    chat_id=chat_id,
                    chat_name=str(row.get("chat_name") or ""),
                    chat_type=str(row.get("chat_type") or ""),
                    msg_id=msg_id,
                    sender_id=sid,
                    sender_name=str(row.get("sender_name") or ""),
                    content_preview=str(row.get("content") or "")[:200],
                    timestamp=str(row.get("timestamp") or ""),
                    reason=reason,
                    dedupe_key=dedupe_key,
                    unread=is_unread,
                    detail=detail,
                )
            )

    logger.info(
        "[未回复待办] 平台 %s 扫描完成：扫描 %d 条 / 工作时间外 %d 条 / 新增待处理 %d 条 / 回填 %d 条",
        platform, result.scanned, result.offhours, result.pending_new, result.replied_backfilled,
    )
    return result


def collect_platform_self_ids(engine: Any, ctx: "PlatformContext") -> frozenset[str]:
    """收敛某平台的自身 ID：引擎全局身份 + 平台上下文身份，缺失则留空。

    自身身份由 ``src/self_identity.py`` 在启动时统一反推并挂到引擎上；这里只做
    收集，不重新学习，避免扫描线程与启动期的身份口径漂移。
    """
    values = [
        getattr(engine, "current_open_dingtalk_id", ""),
        getattr(engine, "current_user_id", ""),
        getattr(ctx, "current_user_id", ""),  # dataclass 无此字段时取空
        getattr(ctx, "current_user_user_id", ""),
    ]
    cfg = getattr(ctx, "config", None)
    if cfg is not None:
        values.extend([
            getattr(cfg, "owner_user_id", ""),
            getattr(cfg, "owner_open_dingtalk_id", ""),
        ])
    return collect_self_ids(*values)
