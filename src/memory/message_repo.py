"""Repository for message repo operations — extracted from SQLiteStore.

Design: receives SQLiteStore instance as constructor parameter, uses
self.store.conn for per-thread connection access. Zero behavior change.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from src.models import Message
from src.memory.platform_context import get_current_platform
from src.memory.image_cleanup import purge_orphan_images

if TYPE_CHECKING:
    from src.memory.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)


class MessageRepo:
    """Repository extracted from SQLiteStore for message operations."""

    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def _cc(self) -> sqlite3.Connection:
        """会话库连接：平台取自当前平台上下文（contextvar）。

        message_repo 全部方法只碰会话相关表，且总是在平台处理上下文内被调用
        （poller 每轮 / runtime 平台回调已设置 current_platform），故无需逐方法传 platform。
        """
        return self.store.conv_conn(get_current_platform())

    def _cc_for(self, platform: str = "") -> sqlite3.Connection:
        """会话库连接：platform 显式给定时优先，否则回退当前平台上下文。

        供 Web 层使用——Web 的平台由请求参数决定（缺省回退 dingtalk），与 src 端
        contextvar 的缺省语义不同，故由调用方显式传入避免路由到错误的会话库。
        """
        return self.store.conv_conn(platform) if platform else self._cc()

    def is_message_processed(self, msg_id: str) -> bool:
        cur = self._cc().cursor()
        cur.execute("SELECT 1 FROM dedup_messages WHERE msg_id = ?", (msg_id,))
        return cur.fetchone() is not None

    def mark_message_processed(self, msg_id: str, chat_id: str) -> None:
        cur = self._cc().cursor()
        now = datetime.now().isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO dedup_messages (msg_id, chat_id, processed_at) VALUES (?, ?, ?)",
            (msg_id, chat_id, now)
        )
        self._cc().commit()

    def load_recent_processed_msg_ids(self, hours: int = 24) -> set[str]:
        """启动时从 DB 加载最近 N 小时的已处理消息 ID，避免重启后重复处理。"""
        cur = self._cc().cursor()
        since = (datetime.now() - timedelta(hours=hours)).isoformat()
        cur.execute(
            "SELECT msg_id FROM dedup_messages WHERE processed_at >= ?",
            (since,)
        )
        return {row[0] for row in cur.fetchall()}

    def cleanup_processed_msgs(self, hours: int = 72) -> None:
        """清理 N 小时前的已处理记录（释放空间）。"""
        cur = self._cc().cursor()
        before = (datetime.now() - timedelta(hours=hours)).isoformat()
        cur.execute("DELETE FROM dedup_messages WHERE processed_at < ?", (before,))
        self._cc().commit()

    def cleanup_old_messages(self, retention_days: int = 90) -> dict:
        """清理超过保留期的旧消息记录，防止 messages 表无限增长。

        Args:
            retention_days: 消息保留天数，超出此时间的消息将被删除。

        Returns:
            dict with keys: deleted_count, before_ts
        """
        before = (datetime.now() - timedelta(days=retention_days)).isoformat()
        # P0-1: 使用 store 级锁保证清理操作原子性，避免与其他清理线程竞态
        with self.store._lock:
            cur = self._cc().cursor()
            cur.execute("SELECT COUNT(*) FROM messages WHERE created_at < ?", (before,))
            count = cur.fetchone()[0]
            if count > 0:
                # 收集待删消息引用的本地图片（相对 data/tmp_images 的 POSIX 路径），
                # 删行后一并清理磁盘文件，避免「消息已删、图片成孤儿文件永久累积」的磁盘泄漏。
                # 文件名含 msg_id（ocr_<msg_id>.png / card_<key>.png），与消息 1:1，可直接删除。
                cur.execute(
                    "SELECT image_path FROM messages WHERE created_at < ? AND image_path != ''",
                    (before,),
                )
                image_paths = [r[0] for r in cur.fetchall()]
                cur.execute("DELETE FROM messages WHERE created_at < ?", (before,))
                self._cc().commit()
                removed_files = purge_orphan_images(self.store.db_path, image_paths)
                logger.info("清理 %d 条旧消息记录（%s 天前），删除孤儿图片 %d 个",
                            count, retention_days, removed_files)
        return {"deleted_count": count, "before_ts": before}

    # ============ 死信队列（P0-2）============

    def save_message(self, message: Message, role: str = "user", skip_reason: str = "") -> None:
        """保存一条消息，同时更新会话统计（写 messages + conversations 跨表事务）。

        使用 with self._cc() as _conn: 确保两表写入原子性：任一失败则全部回滚。

        OA审批系统推送消息使用独立会话（chat_id = 'system:oa_approval'），
        避免与"工作通知"群混在一起。
        """
        with self._cc() as _conn:
            cur = self._cc().cursor()
            now = datetime.now().isoformat()
            chat_name = message.chat_name.strip() if message.chat_name else ""

            # OA审批消息使用独立会话
            if message.sender_name == "OA审批":
                effective_chat_id = "system:oa_approval"
                effective_chat_name = "OA审批"
                effective_chat_type = "other"
            else:
                effective_chat_id = message.chat_id
                effective_chat_name = chat_name
                effective_chat_type = message.chat_type or "single"
            cur.execute(
                """INSERT OR IGNORE INTO messages
                   (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, timestamp, role, image_path, is_bot, skip_reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    effective_chat_id,
                    effective_chat_type,
                    message.msg_id,
                    message.sender_id,
                    message.sender_name,
                    message.content,
                    message.msg_type,
                    message.timestamp.isoformat(),
                    role,
                    message.image_path or "",
                    int(message.is_bot),
                    skip_reason or "",
                    now,
                ),
            )
            if cur.rowcount > 0:
                last_message_time = message.timestamp.isoformat() if message.timestamp else now
                cur.execute(
                    """INSERT INTO conversations
                       (chat_id, chat_name, chat_type, peer_user_id, peer_open_dingtalk_id, last_message_time, message_count, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                           chat_name = COALESCE(NULLIF(excluded.chat_name, ''), conversations.chat_name),
                           chat_type = excluded.chat_type,
                           last_message_time = excluded.last_message_time,
                           message_count = COALESCE(conversations.message_count, 0) + 1,
                           updated_at = excluded.updated_at""",
                    (
                        effective_chat_id,
                        effective_chat_name,
                        effective_chat_type,
                        "",
                        "",
                        last_message_time,
                        now,
                        now,
                    ),
                )

    def update_message_content(self, msg_id: str, content: str) -> None:
        """更新消息内容（用于 OCR 异步识别完成后更新）。"""
        if not msg_id or not content:
            return
        with self.store._lock:
            cur = self._cc().cursor()
            cur.execute(
                "UPDATE messages SET content = ? WHERE msg_id = ?",
                (content, msg_id),
            )
            self._cc().commit()

    def update_message_image_path(self, msg_id: str, image_path: str) -> None:
        """更新消息的图片相对路径（OCR 完成后写入，供消息记录页缩略图展示）。"""
        if not msg_id:
            return
        with self.store._lock:
            cur = self._cc().cursor()
            cur.execute(
                "UPDATE messages SET image_path = ? WHERE msg_id = ?",
                (image_path or "", msg_id),
            )
            self._cc().commit()

    def backfill_missing_image_path(self, msg_id: str, image_path: str, platform: str = "") -> int:
        """幂等回填图片路径：仅当该消息 image_path 为空时才写入（避免覆盖已存在的真值）。

        用于会话浏览路由的磁盘兜底匹配——OCR 回调未回写 image_path 时，按发送者+时间戳
        从磁盘匹配最近图片文件并回填。落在正确的 conv_conn 会话库（按平台隔离），主键用
        msg_id（与全代码库一致）。

        Returns:
            受影响行数（0 表示无需回填或消息不存在）。
        """
        if not msg_id or not image_path:
            return 0
        with self.store._lock:
            conn = self._cc_for(platform)
            cur = conn.cursor()
            cur.execute(
                "UPDATE messages SET image_path = ? "
                "WHERE msg_id = ? AND (image_path = '' OR image_path IS NULL)",
                (image_path, msg_id),
            )
            n = cur.rowcount
            conn.commit()
            return n

    def fix_self_message_roles(self, current_user_id: str, current_user_name: str, current_user_user_id: str = "") -> int:
        """修复因早期 bug 导致方向错误的数据：自己发的消息 role='user' → 'assistant'。

        匹配条件（满足其一即可）：
        - sender_id 等于 current_user_id（通常是 openDingTalkId）
        - sender_id 等于 current_user_user_id（userId）
        - sender_name 等于 current_user_name（去除首尾空格后比较）

        Returns:
            更新的消息行数
        """
        if not current_user_id and not current_user_user_id and not current_user_name:
            return 0
        with self.store._lock:
            cur = self._cc().cursor()
            params: list[str] = []
            conditions: list[str] = []
            if current_user_id:
                conditions.append("sender_id = ?")
                params.append(current_user_id)
            if current_user_user_id:
                conditions.append("sender_id = ?")
                params.append(current_user_user_id)
            if current_user_name:
                conditions.append("TRIM(sender_name) = ?")
                params.append(current_user_name.strip())
            where = f"role = 'user' AND ({' OR '.join(conditions)})"
            cur.execute(
                f"UPDATE messages SET role = 'assistant' WHERE {where}",
                params,
            )
            updated = cur.rowcount
            self._cc().commit()
            if updated:
                logger.info("[SQLiteStore] 修复方向错误消息 %d 条", updated)
            return updated

    def delete_message(self, msg_id: str) -> bool:
        """删除消息记录（用于消息撤回）。返回是否删除成功。

        Bug4 根治：删除消息时必须同步减少所属会话的 message_count，
        否则撤回越多次数越虚高（曾出现 stored=1251 而真实仅 555 的情况），
        进而误导 get_conversations_needing_summary 等依赖计数的逻辑。
        """
        if not msg_id:
            return False
        with self.store._lock:
            cur = self._cc().cursor()
            # 先查所属会话与图片路径，便于同步扣减 message_count 并清理磁盘孤儿图片
            cur.execute("SELECT chat_id, image_path FROM messages WHERE msg_id = ?", (msg_id,))
            row = cur.fetchone()
            chat_id = row["chat_id"] if row else None
            image_path = (row["image_path"] or "") if row else ""
            cur.execute("DELETE FROM messages WHERE msg_id = ?", (msg_id,))
            deleted = cur.rowcount > 0
            if deleted and chat_id:
                cur.execute(
                    "UPDATE conversations SET message_count = MAX(0, COALESCE(message_count, 0) - 1) "
                    "WHERE chat_id = ?",
                    (str(chat_id),),
                )
            self._cc().commit()
            if deleted and image_path:
                purge_orphan_images(self.store.db_path, [image_path])
            return deleted

    def mark_message_withdrawn(self, msg_id: str) -> bool:
        """标记消息为已撤回（软删除：保留记录但标记 is_withdrawn=1）。

        相比 delete_message（硬删），撤回标记保留消息行，
        让 Web 消息记录能看到「该消息已撤回」的占位提示。
        返回是否标记成功。
        """
        if not msg_id:
            return False
        with self.store._lock:
            cur = self._cc().cursor()
            cur.execute(
                "UPDATE messages SET is_withdrawn = 1 WHERE msg_id = ? AND is_withdrawn = 0",
                (msg_id,),
            )
            marked = cur.rowcount > 0
            self._cc().commit()
            return marked

    def update_message(self, msg_id: str, content: str) -> bool:
        """更新消息（用于消息编辑）。返回是否更新成功。"""
        if not msg_id or not content:
            return False
        with self.store._lock:
            cur = self._cc().cursor()
            cur.execute(
                "UPDATE messages SET content = ? WHERE msg_id = ?",
                (content, msg_id),
            )
            self._cc().commit()
            return cur.rowcount > 0

    def get_message_by_id(self, msg_id: str) -> Message | None:
        """根据 msg_id 查询单条消息。"""
        if not msg_id:
            return None
        with self.store._lock:
            cur = self._cc().cursor()
            # 【bug fix】messages 表只有 11 个原始列。get_message_by_id 之前 SELECT 了 13 个
            # 其中 5 个（raw/is_bot/role/is_archived/image_path）不在表里，
            # 任何调用都 OperationalError。修法：只 SELECT 表里实际存在的列。
            # 额外字段（raw/is_bot/role/is_archived/image_path）赋默认值。
            cur.execute(
                "SELECT msg_id, chat_id, chat_type, sender_id, sender_name, "
                "content, msg_type, timestamp "
                "FROM messages WHERE msg_id = ?",
                (msg_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return Message(
                msg_id=row[0],
                chat_id=row[1],
                chat_type=row[2],
                chat_name="",  # messages 表不存 chat_name
                sender_id=row[3] or "",
                sender_name=row[4] or "",
                content=row[5] or "",
                msg_type=row[6] or "text",
                timestamp=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
                raw={},  # messages 表不存 raw
                is_bot=False,  # messages 表不存 is_bot
                role="",  # 不从 messages 表读 role（避免下标偏移）
                image_path="",  # messages 表不存 image_path
            )

    def get_conversation_history(self, chat_id: str, limit: int = 20,
                                  days: int = 1, include_archived: bool = False,
                                  session_gap_minutes: int = 0) -> list[Message]:
        """获取会话历史消息（按时间倒序，最新在前）。

        Args:
            chat_id: 会话ID
            limit: 最大消息条数
            days: 只取最近 N 天的消息（默认 1 天，即只取当天）
            include_archived: 是否包含已归档消息（默认 False，LLM上下文只用非归档）
            session_gap_minutes: 会话间隔切分阈值（分钟）。>0 时，从最新消息往回扫，
                一旦相邻两条消息间隔超过此阈值就截断，只保留最近一段连续对话，
                避免将陈年无关旧话题带入当前上下文。0 表示禁用。
        """
        if not chat_id:
            return []
        with self.store._lock:
            cur = self._cc().cursor()

            # 计算日期范围
            start_date = (datetime.now() - timedelta(days=days - 1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()

            # 参数化查询：archive 条件通过占位符传入，避免字符串拼接 SQL
            if include_archived:
                archive_clause = ""
                archive_params: list = []
            else:
                archive_clause = "AND is_archived = ?"
                archive_params = [0]
            cur.execute(
                f"""SELECT * FROM messages
                   WHERE chat_id = ? AND timestamp >= ? {archive_clause}
                   ORDER BY timestamp DESC LIMIT ?""",
                [str(chat_id), start_date, *archive_params, limit],
            )
            rows = cur.fetchall()
            if not rows:
                return []

            # 一次性获取会话信息，避免循环内重复查询
            chat_id_str = str(chat_id)
            cur.execute("SELECT * FROM conversations WHERE chat_id = ?", (chat_id_str,))
            conv_row = cur.fetchone()
            conv = dict(conv_row) if conv_row else None

            # SQL 已 ORDER BY timestamp DESC（最新在前），直接按序构建即可；
            # 不要再次 reversed，否则会变成正序，违背 docstring "按时间倒序" 契约
            messages = []
            for row in rows:
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                except (ValueError, TypeError) as _exc:
                    logger.warning(f"get_conversation_history: swallowed exception: {_exc}")
                    ts = datetime.now()
                # 防御性兜底：历史里若残留 OCR 占位符("[图片识别中...]")，回放时剔除，
                # 避免把"识别中"状态泄漏给 LLM 上下文，造成答非所问 / 上下文断链。
                raw_content = row["content"] or ""
                if "[图片识别中...]" in raw_content:
                    raw_content = raw_content.replace("[图片识别中...]", "").strip()
                messages.append(Message(
                    msg_id=row["msg_id"] or f"local-{row['id']}",
                    chat_id=row["chat_id"],
                    chat_type=(row["chat_type"] or "") or (conv or {}).get("chat_type", "single"),
                    chat_name=(conv or {}).get("chat_name") or None,
                    sender_id=row["sender_id"] or "",
                    sender_name=row["sender_name"] or "",
                    content=raw_content,
                    msg_type=row["msg_type"] or "text",
                    timestamp=ts,
                    raw={},
                    role=row["role"] or "user",
                ))

            # 会话间隔切分：messages 已按时间倒序（index 0 最新）。
            # 从最新往回扫，若相邻两条（较新 messages[i-1] 与较旧 messages[i]）
            # 时间间隔 > 阈值，则在此断开，只保留最近一段连续对话。
            if session_gap_minutes and session_gap_minutes > 0 and len(messages) > 1:
                gap = timedelta(minutes=session_gap_minutes)
                cut = len(messages)
                for i in range(1, len(messages)):
                    if messages[i - 1].timestamp - messages[i].timestamp > gap:
                        cut = i
                        break
                if cut < len(messages):
                    messages = messages[:cut]
            return messages

    def get_conversations_needing_summary(self, max_messages: int = 50,
                                          summary_interval_hours: int = 24) -> list[dict]:
        """获取需要摘要的会话列表（消息数超过阈值，且不在近期已摘要名单内）。

        通过 last_summary_at 过滤，避免同一批超长会话每轮/每次重启被反复全量重摘要
        （原实现 message_count 不减，导致永续心跳式轰炸免费 LLM 接口）。
        """
        cur = self._cc().cursor()
        cur.execute(
            "SELECT chat_id, chat_name, chat_type, message_count FROM conversations "
            "WHERE message_count >= ? "
            "AND (last_summary_at IS NULL "
            "     OR last_summary_at < datetime('now', ?)) "
            "ORDER BY message_count DESC",
            (max_messages, f"-{summary_interval_hours} hours"),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_recent_unarchived_messages(self, chat_id: str, limit: int = 40) -> list[Message]:
        """获取会话最近的非归档消息（按时间正序），用于增量摘要。

        只取 is_archived=0 的消息，避免把已压缩的历史反复重喂给 LLM，
        既减少 token 消耗，也让每次摘要只处理「上次摘要以来的增量」。
        """
        cur = self._cc().cursor()
        cur.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND is_archived = 0 "
            "ORDER BY timestamp DESC LIMIT ?",
            (str(chat_id), limit),
        )
        rows = list(cur.fetchall())
        rows.reverse()  # 转为时间正序，便于拼接对话
        messages = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (ValueError, TypeError) as _exc:
                logger.warning(f"get_recent_unarchived_messages: swallowed exception: {_exc}")
                ts = datetime.now()
            messages.append(Message(
                msg_id=row["msg_id"] or f"local-{row['id']}",
                chat_id=row["chat_id"],
                chat_type=(row["chat_type"] or ""),
                chat_name=None,
                sender_id=row["sender_id"] or "",
                sender_name=row["sender_name"] or "",
                content=row["content"] or "",
                msg_type=row["msg_type"] or "text",
                timestamp=ts,
                raw={},
                role=row["role"] or "user",
            ))
        return messages

    def get_full_conversation_history(self, chat_id: str) -> list[Message]:
        """获取会话的完整消息历史（按时间顺序）。"""
        cur = self._cc().cursor()
        cur.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp ASC",
            (str(chat_id),),
        )
        rows = cur.fetchall()
        # 注意：SQL 已是 ORDER BY timestamp ASC（时间正序，与 docstring "按时间顺序" 一致），
        # 不要再加 reversed/rows.reverse()，否则会变成倒序与文档矛盾
        messages = []
        for row in rows:
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (ValueError, TypeError) as _exc:
                logger.warning(f"get_full_conversation_history: swallowed exception: {_exc}")
                ts = datetime.now()
            messages.append(Message(
                msg_id=row["msg_id"] or f"local-{row['id']}",
                chat_id=row["chat_id"],
                chat_type=(row["chat_type"] or ""),
                chat_name=None,
                sender_id=row["sender_id"] or "",
                sender_name=row["sender_name"] or "",
                content=row["content"] or "",
                msg_type=row["msg_type"] or "text",
                timestamp=ts,
                raw={},
                role=row["role"] or "user",
            ))
        return messages

    def summarize_and_compress(self, chat_id: str, summary_text: str,
                               keep_ratio: float = 0.4) -> int:
        """用摘要压缩会话历史：旧消息标记为归档，插入摘要消息。

        旧消息不删除，只标记 is_archived=1：
        - LLM 上下文只取非归档消息（省 token）
        - 前端消息记录仍可查看完整历史

        Args:
            chat_id: 会话ID
            summary_text: 生成的摘要文本
            keep_ratio: 保留消息的比例（0.0-1.0）

        Returns:
            归档的消息数量
        """
        if not chat_id or not summary_text:
            return 0

        with self.store._lock:
            cur = self._cc().cursor()

            cur.execute(
                "SELECT id, msg_id, timestamp FROM messages "
                "WHERE chat_id = ? AND is_archived = 0 "
                "ORDER BY timestamp ASC",
                (str(chat_id),),
            )
            rows = cur.fetchall()
            if len(rows) <= 1:
                return 0

            keep_count = max(1, int(len(rows) * keep_ratio))
            archive_rows = rows[:-keep_count]

            if not archive_rows:
                return 0

            archive_ids = [row["id"] for row in archive_rows]
            placeholders = ",".join(["?"] * len(archive_ids))
            cur.execute(
                f"UPDATE messages SET is_archived = 1 WHERE id IN ({placeholders})",
                archive_ids,
            )

            first_timestamp = rows[0]["timestamp"]
            try:
                ts = datetime.fromisoformat(first_timestamp)
            except (ValueError, TypeError) as _exc:
                logger.warning(f"summarize_and_compress: swallowed exception: {_exc}")
                ts = datetime.now()

            summary_msg_id = f"summary-{uuid.uuid4().hex[:12]}"
            cur.execute(
                """INSERT INTO messages
                   (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type, timestamp, role, created_at, is_archived)
                   VALUES (?, 'summary', ?, 'system', '系统', ?, 'system', ?, 'system', ?, 0)""",
                (str(chat_id), summary_msg_id, summary_text, ts.isoformat(), datetime.now().isoformat()),
            )
            # 摘要消息也是 messages 表中的一行, message_count 语义是会话总消息数,
            # 故需同步 +1 (不变式: message_count == count(messages) 对所有路径成立)
            cur.execute(
                "UPDATE conversations SET message_count = COALESCE(message_count, 0) + 1 "
                "WHERE chat_id = ?",
                (str(chat_id),),
            )

            self._cc().commit()

            # 记录本次摘要时间，防止该会话在 summary_interval_hours 内被反复重摘要
            # 注意: 压缩只是把旧消息标记 is_archived=1(不删行)，message_count 语义是
            # 会话总消息数(含归档，因为归档消息仍在 messages 表中)，故此处**不能**减 message_count
            # (早前曾错误地 -归档数, 导致计数低于真实总行数, 见 commit ec52931 回归分析)。
            # 真正需要减 message_count 的只有硬删消息(delete_message/撤回)，那边已处理。
            try:
                # 写入侧改用 SQLite datetime('now') (UTC, 格式与查询侧 datetime('now','-24 hours')
                # 完全一致), 消除原 datetime.now() 本地时间 vs SQLite UTC 的时区/格式双不一致
                # (原写法在 UTC+8 下防重窗口实际≈32h 而非 24h)
                cur.execute(
                    "UPDATE conversations SET last_summary_at = datetime('now') WHERE chat_id = ?",
                    (str(chat_id),),
                )
                self._cc().commit()
            except Exception as e:
                logger.debug("[摘要] 更新 last_summary_at 失败（不影响压缩）: %s", e)

            logger.debug("[摘要] 会话 %s 已压缩：归档 %d 条旧消息，生成摘要", chat_id[:20], len(archive_rows))
            return len(archive_rows)

    def mark_conversation_summarized(self, chat_id: str) -> None:
        """记录会话已摘要（写入 last_summary_at），供防重摘要过滤。"""
        try:
            with self.store._lock:
                cur = self._cc().cursor()
                # 写入侧改用 SQLite datetime('now') (UTC), 与查询侧 datetime('now','-24 hours') 同基准
                cur.execute(
                    "UPDATE conversations SET last_summary_at = datetime('now') WHERE chat_id = ?",
                    (str(chat_id),),
                )
                self._cc().commit()
        except Exception as e:
            logger.debug("[摘要] 更新 last_summary_at 失败（不影响流程）: %s", e)

    # ============ 计数 / 聚合（供 Web 状态与统计面板） ============

    def count_messages(self, platform: str = "") -> int:
        """当前平台会话库的消息总条数。"""
        cur = self._cc_for(platform).cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        return cur.fetchone()[0]

    def count_messages_by_role(self, role: str, platform: str = "") -> int:
        """按 role 统计消息条数（如 role='assistant' 即 AI 回复数）。"""
        cur = self._cc_for(platform).cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM messages WHERE role = ?", (role,))
        return cur.fetchone()["cnt"]

    def count_messages_with_content(self, platform: str = "") -> int:
        """有正文（content 非 NULL）的消息条数，供画像样本量标注。"""
        cur = self._cc_for(platform).cursor()
        cur.execute("SELECT COUNT(*) FROM messages WHERE content IS NOT NULL")
        return cur.fetchone()[0]

    def count_messages_from_sender(self, sender_name: str, platform: str = "") -> int:
        """统计指定发送者发出的有效历史消息数（排除空正文与 '[自动回复]' 前缀）。"""
        cur = self._cc_for(platform).cursor()
        cur.execute(
            "SELECT COUNT(*) FROM messages "
            "WHERE sender_name = ? AND content IS NOT NULL "
            "AND content NOT LIKE '[自动回复]%'",
            (sender_name,),
        )
        return cur.fetchone()[0]

    def count_dedup_messages(self, platform: str = "") -> int:
        """去重表条数；同时作为会话库连通性的廉价探针。"""
        cur = self._cc_for(platform).cursor()
        cur.execute("SELECT COUNT(*) FROM dedup_messages")
        return cur.fetchone()[0]

    def get_last_processed_at(self, platform: str = "") -> str | None:
        """最近一次消息处理时间（dedup_messages.processed_at 最大值），无记录返回 None。"""
        cur = self._cc_for(platform).cursor()
        cur.execute("SELECT processed_at FROM dedup_messages ORDER BY processed_at DESC LIMIT 1")
        row = cur.fetchone()
        return row["processed_at"] if row else None

    def get_daily_message_trend(self, days: int, platform: str = "") -> list[dict]:
        """最近 N 天按自然日聚合的消息条数，按日期升序，返回 [{day, cnt}]。"""
        cur = self._cc_for(platform).cursor()
        cur.execute(
            """SELECT DATE(timestamp) as day, COUNT(*) as cnt
               FROM messages
               WHERE timestamp >= date('now', ?)
               GROUP BY day
               ORDER BY day""",
            (f"-{days} days",),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_message_type_breakdown(self, platform: str = "") -> list[dict]:
        """按 (sender_name, msg_type, chat_type) 分组的消息条数，供调用方归类为展示类别。"""
        cur = self._cc_for(platform).cursor()
        cur.execute(
            """SELECT sender_name, msg_type, chat_type, COUNT(*) as cnt
               FROM messages
               GROUP BY sender_name, msg_type, chat_type"""
        )
        return [dict(row) for row in cur.fetchall()]

    def get_top_senders(self, limit: int = 10, platform: str = "") -> list[dict]:
        """用户侧消息（role 为 'user' 或空）的高频发送者 TOP N，返回 [{sender_name, cnt}]。

        过滤掉系统推送发送者（如 'OA审批', '钉钉人事旗舰版' 等），避免系统消息干扰统计。
        """
        cur = self._cc_for(platform).cursor()
        # 系统发送者白名单（根据实际业务场景调整）
        system_senders = ('OA审批', '钉钉人事旗舰版', '智能人事', '系统', '钉钉', '钉钉机器人')
        cur.execute(
            """SELECT sender_name, COUNT(*) as cnt
               FROM messages
               WHERE (role = 'user' OR role = '')
                 AND sender_name NOT IN ({})
               GROUP BY sender_name
               ORDER BY cnt DESC
               LIMIT ?""".format(','.join(['?'] * len(system_senders))),
            list(system_senders) + [limit],
        )
        return [dict(row) for row in cur.fetchall()]

    def get_recent_user_contents(self, limit: int = 500, platform: str = "") -> list[str]:
        """最近 N 条用户侧消息正文（时间倒序），供关键词分词统计；NULL 归一化为空串。"""
        cur = self._cc_for(platform).cursor()
        cur.execute(
            """SELECT content FROM messages
               WHERE role = 'user' OR role = ''
               ORDER BY timestamp DESC
               LIMIT ?""",
            (limit,),
        )
        return [row["content"] or "" for row in cur.fetchall()]

    # ============ 带会话名的消息列表（供 Web 会话/消息面板） ============

    #: 会话面板消息列表列（含 conversations.chat_name）
    _MESSAGE_VIEW_COLUMNS = (
        "m.id, m.chat_id, m.chat_type, m.msg_id, m.sender_id, m.sender_name, m.content, "
        "m.msg_type, m.timestamp, m.role, m.image_path, m.is_bot, m.is_archived, "
        "m.is_withdrawn, m.skip_reason, c.chat_name"
    )

    #: 消息 CSV 导出列（顺序即导出表头顺序，调用方直接复用以保持契约稳定）
    EXPORT_COLUMNS: tuple[str, ...] = (
        "id", "chat_id", "chat_type", "msg_id", "sender_id", "sender_name",
        "content", "msg_type", "timestamp", "role", "is_bot", "is_archived",
        "is_withdrawn", "skip_reason", "chat_name",
    )

    def list_messages_with_chat_name(self, chat_id: str = "", limit: int = 50,
                                     platform: str = "") -> list[dict]:
        """按时间倒序取消息并左连出会话名；chat_id 为空时跨全部会话取最近 limit 条。"""
        cur = self._cc_for(platform).cursor()
        if chat_id:
            cur.execute(
                f"""SELECT {self._MESSAGE_VIEW_COLUMNS}
                    FROM messages m
                    LEFT JOIN conversations c ON m.chat_id = c.chat_id
                    WHERE m.chat_id = ?
                    ORDER BY m.timestamp DESC LIMIT ?""",
                (chat_id, limit),
            )
        else:
            cur.execute(
                f"""SELECT {self._MESSAGE_VIEW_COLUMNS}
                    FROM messages m
                    LEFT JOIN conversations c ON m.chat_id = c.chat_id
                    ORDER BY m.timestamp DESC LIMIT ?""",
                (limit,),
            )
        return [dict(row) for row in cur.fetchall()]

    def export_messages(self, chat_id: str = "", limit: int = 1000,
                        platform: str = "") -> list[dict]:
        """导出消息记录：chat_id 为空时导出全部会话，时间倒序，最多 limit 条。

        每行是以 ``EXPORT_COLUMNS`` 为键的 dict，列顺序由 ``EXPORT_COLUMNS`` 决定。
        """
        cols = ", ".join(
            f"c.{k}" if k == "chat_name" else f"m.{k}" for k in self.EXPORT_COLUMNS
        )
        cur = self._cc_for(platform).cursor()
        if chat_id:
            cur.execute(
                f"""SELECT {cols}
                    FROM messages m
                    LEFT JOIN conversations c ON m.chat_id = c.chat_id
                    WHERE m.chat_id = ?
                    ORDER BY m.timestamp DESC LIMIT ?""",
                (chat_id, limit),
            )
        else:
            cur.execute(
                f"""SELECT {cols}
                    FROM messages m
                    LEFT JOIN conversations c ON m.chat_id = c.chat_id
                    ORDER BY m.timestamp DESC LIMIT ?""",
                (limit,),
            )
        return [{k: row[k] for k in self.EXPORT_COLUMNS} for row in cur.fetchall()]

    # ============ 决策追踪持久化 ============

