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


def _split_config_list(value: str) -> frozenset[str]:
    """把逗号分隔的配置项拆成去空集合（大小写保留，调用方按需 lower）。"""
    return frozenset(x.strip() for x in str(value or "").split(",") if x.strip())


def _is_ignored_message(row: dict[str, Any], cfg: UnrepliedTodoConfig) -> str:
    """判断一条消息是否属于「不需要建待办」的噪声，返回原因码（空串=保留）。

    只给「真人消息留言」建待办。以下一律跳过：
    1. 系统/机器人发送者（工作汇报日志、考勤打卡等，伪装成 sendType=user）；
    2. 内容命中系统前缀（`[日志]:` 等）；
    3. 纯确认/客套短语（ok、好的、收到、谢谢……），这些不是待处理的留言。
    """
    sender = str(row.get("sender_name") or "").strip()
    sender_id = str(row.get("sender_id") or "").strip()
    content = str(row.get("content") or "").strip()

    for name in _split_config_list(cfg.ignore_system_senders):
        if name and name in sender:
            return f"system_sender:{name}"
    if sender_id and sender_id in _split_config_list(cfg.ignore_sender_ids):
        return "ignored_sender_id"
    for prefix in _split_config_list(cfg.ignore_content_prefixes):
        if prefix and content.startswith(prefix):
            return f"system_prefix:{prefix}"
    # 纯确认词：去掉中间及两端标点/空白后精确匹配，兼容「收到，谢谢」这类组合客套，
    # 同时避免「好的，明天给你」这类带实质内容的留言被误杀（规范化后不在集合里）。
    trivial_set = _split_config_list(cfg.ignore_trivial_replies)
    normalized = "".join(
        ch for ch in content if ch not in " \t\r\n。.!！~～,，;；:：、…"
    )
    if normalized and (normalized in trivial_set
                       or normalized.lower() in {t.lower() for t in trivial_set}):
        return "trivial_reply"
    return ""


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


def _fetch_platform_messages(
    adapter: Any, platform: str, *, start: str, end: str,
) -> list[dict[str, Any]]:
    """从平台侧按时间窗拉取消息，补上服务离线期间未同步进本地库的未读消息。

    钉钉 ``chat_message_list_all`` 能拉到当前账号时间窗内所有会话消息（不依赖本地
    会话库是否同步过），这正是「昨天发来、服务没跑就没入库」那条消息能进扫描的关键。
    飞书/企微暂无等价的「全会话时间窗」封装，返回空列表，退化为本地库扫描。

    返回统一字段的 dict 列表（与本地库 messages 行同构），失败非致命。
    """
    if platform != "dingtalk" or adapter is None:
        return []
    fn = getattr(adapter, "chat_message_list_all", None)
    if not callable(fn):
        return []
    try:
        data = fn(start=start, end=end, limit=50, max_pages=10)
    except (
        IMAdapterError, OSError, ValueError, TypeError, RuntimeError,
        KeyError, subprocess.SubprocessError,
    ) as e:
        logger.warning("[未回复待办] 平台侧消息拉取失败（降级为本地库）: %s", e)
        return []

    out: list[dict[str, Any]] = []
    convs = data.get("conversationMessagesList", []) if isinstance(data, dict) else []
    for conv in convs or []:
        if not isinstance(conv, dict):
            continue
        chat_id = str(conv.get("openConversationId") or conv.get("conversationId") or "")
        chat_name = str(conv.get("title") or conv.get("conversationTitle") or "")
        single = bool(conv.get("singleChat"))
        for msg in conv.get("messages", []) or []:
            if not isinstance(msg, dict):
                continue
            msg_id = str(msg.get("openMessageId") or msg.get("messageId") or "")
            if not msg_id or not chat_id:
                continue
            # messageAiSendFlag='DWS' 表示 AI 代发；sendType='bot' 为机器人
            ai_flag = str(msg.get("messageAiSendFlag") or "").strip()
            send_type = str(msg.get("sendType") or "").strip().lower()
            is_bot = 1 if (ai_flag or send_type in ("bot", "robot", "ai")) else 0
            out.append({
                "msg_id": msg_id,
                "chat_id": chat_id,
                "chat_type": "single" if single else "group",
                "sender_id": str(msg.get("senderId") or msg.get("senderOpenDingTalkId") or ""),
                "sender_name": str(msg.get("sender") or ""),
                "content": str(msg.get("text") or msg.get("content") or ""),
                "msg_type": "text",
                # createTime 形如 '2026-11-21 16:44:02'，统一成 ISO 'T' 供 parse_message_ts
                "timestamp": str(msg.get("createTime") or "").replace(" ", "T"),
                "is_bot": is_bot,
                "role": "assistant" if is_bot else "user",
                "is_withdrawn": 0,
                "chat_name": chat_name,
            })
    return out


def _merge_rows(local: list[dict[str, Any]], platform_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (chat_id, msg_id) 去重合并；平台侧字段补全本地库空值（如会话名）。

    平台侧排在前面，保证同 ID 时优先采用平台侧的准确 is_bot / sender 标记；
    本地库独有的历史条目（平台时间窗外）保留。
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in platform_rows:
        merged[(str(row.get("chat_id") or ""), str(row.get("msg_id") or ""))] = row
    for row in local:
        key = (str(row.get("chat_id") or ""), str(row.get("msg_id") or ""))
        if key in merged:
            # 本地库补会话名等空字段，不覆盖平台侧的判定字段
            exist = merged[key]
            for field in ("chat_name", "chat_type"):
                if not exist.get(field) and row.get(field):
                    exist[field] = row[field]
            continue
        merged[key] = row
    return list(merged.values())


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
        local_rows = [dict(r) for r in cur.fetchall()]
        cur.close()
    except (sqlite3.Error, AttributeError) as e:
        logger.warning("[未回复待办] 平台 %s 扫描消息失败: %s", platform, e)
        result.errors.append(f"扫描消息失败: {e}")
        local_rows = []

    # 平台侧拉取补本地库缺口（服务离线期间的未读消息从未同步进本地库）
    platform_rows = _fetch_platform_messages(
        adapter, platform,
        start=(now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"),
        end=now.strftime("%Y-%m-%d %H:%M:%S"),
    )
    rows = _merge_rows(local_rows, platform_rows)
    if platform_rows:
        logger.info(
            "[未回复待办] 平台 %s 侧补拉 %d 条消息（本地库 %d 条，合并后 %d 条）",
            platform, len(platform_rows), len(local_rows), len(rows),
        )

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
        # 只给真人消息留言建待办：系统推送（工作汇报日志）、纯确认词（好的/收到）跳过
        ignored = _is_ignored_message(row, cfg)
        if ignored:
            logger.debug("[未回复待办] 跳过非留言消息(%s): %s", ignored,
                         str(row.get("content") or "")[:40])
            continue
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
            chat_name=str(row.get("chat_name") or ""),  # 平台侧消息自带会话名；本地库为空时由 Web 侧补
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
