from __future__ import annotations

import logging
import re

from src.models import Message
from src.memory.platform_context import get_current_platform

logger = logging.getLogger(__name__)


from src.poller_mixins_base import PollerMixinBase


class DedupMixin(PollerMixinBase):
    """MessagePoller 子系统萃取（mixin，经多继承组合回主类）。"""

    def _is_msg_processed(self, msg_id: str) -> bool:
        """检查消息是否已在之前的轮次中处理过（跨轮次去重）。

        同时查内存和数据库，确保重启后也不重复处理。
        内存优先（快速路径），数据库兜底（重启恢复 + LRU 淘汰恢复）。
        """
        # 优先查内存（快速路径）
        if msg_id in self._processed_msg_ids:
            self._processed_msg_ids.move_to_end(msg_id)
            return True
        # 再查数据库（防止内存中已淘汰但DB中有的记录，或刚重启）
        try:
            if self.store._message_repo.is_message_processed(msg_id):
                # 同步到内存并标记为最近使用（LRU）
                self._processed_msg_ids[msg_id] = True
                self._processed_msg_ids.move_to_end(msg_id)
                return True
        except Exception as e:
            logger.debug("[轮询器] 消息去重查询失败: %s", e)
        return False

    def _mark_msg_processed(self, msg_id: str, chat_id: str, msg=None) -> None:
        """标记消息为已处理（用于跨轮次去重）。

        同时写入内存和数据库。如果传入 msg 且是合并消息，也标记所有原始 msg_id。
        """
        self._processed_msg_ids[msg_id] = True
        try:
            self.store._message_repo.mark_message_processed(msg_id, chat_id)
        except Exception as e:
            logger.warning("[轮询器] 标记消息已处理失败: %s", e)
        # 标记合并消息的所有原始 ID（防止未标记的消息被重复处理）
        # 兼容两套合并路径的 key：poller._combine_message_group 用 original_ids，
        # poller_utils.merge_consecutive_messages 用 merged_original_ids。
        if msg is not None:
            raw = getattr(msg, "raw", None)
            if isinstance(raw, dict):
                orig_ids = raw.get("merged_original_ids") or raw.get("original_ids") or []
                if isinstance(orig_ids, str):
                    orig_ids = [orig_ids]
                for orig_id in orig_ids:
                    if orig_id and orig_id != msg_id:
                        self._processed_msg_ids[orig_id] = True
                        try:
                            self.store._message_repo.mark_message_processed(orig_id, chat_id)
                        except Exception:
                            logger.warning("mark_message_processed failed for orig_id=%s", orig_id, exc_info=True)
        # 移到末尾（LRU：最旧的在前面）
        self._processed_msg_ids.move_to_end(msg_id)
        # 超过容量限制时淘汰最旧的
        while len(self._processed_msg_ids) > self.config.max_processed_msg_ids:
            self._processed_msg_ids.popitem(last=False)

    def _is_self_message(self, message: Message) -> bool:
        """判断是否是自己发的消息（用于过滤，避免自己回自己）。"""
        # 判断1：sender_id 匹配当前登录用户的 openDingTalkId 或 userId
        if message.sender_id and (
            message.sender_id == self.current_user_id
            or message.sender_id == self.current_user_user_id
        ):
            return True
        # 判断2：仅当消息没有可用 sender_id 时，才用姓名兜底（避免同名用户碰撞，
        # 例如组织内有两个「张旭」时，不能仅凭姓名把他人消息误判为自己发的）。
        if (not message.sender_id) and message.sender_name and self.current_user_name:
            if message.sender_name.strip() == self.current_user_name.strip():
                return True
        # 判断3：AI 助手发的消息（sender_name="AI助手" 或 sender_id="ai"）
        if message.sender_name == "AI助手" or message.sender_id == "ai":
            return True
        # 兜底：检查 raw 里可能的 ID 字段
        raw = message.raw or {}
        raw_sender_id = (
            raw.get("senderOpenDingTalkId") or
            raw.get("senderId") or
            raw.get("openDingTalkId") or
            ""
        )
        if raw_sender_id and (
            raw_sender_id == self.current_user_id
            or raw_sender_id == self.current_user_user_id
        ):
            return True
        # 所有判断都未命中时的诊断日志（帮助定位「明明是自己发的却漏判」的极端情况）
        logger.debug(
            "[自我检测] 未识别为自我消息 — sender_id=%s (self=%s) | sender_name=%s (self=%s) | "
            "raw_sid=%s | msg_type=%s",
            message.sender_id[:30] if message.sender_id else "(空)",
            (self.current_user_id or "")[:30],
            message.sender_name or "(空)",
            self.current_user_name or "(空)",
            raw_sender_id[:30] if raw_sender_id else "(空)",
            message.msg_type,
        )
        return False

    def _is_self_sender(self, sender_id: str) -> bool:
        """判断 sender_id 是否是自己。"""
        if not sender_id:
            return False
        return (
            sender_id == self.current_user_id
            or sender_id == self.current_user_user_id
        )

    def _check_if_bot_message(self, msg: Message) -> bool:
        """判断自己发的消息是否是 AI 代发的。

        判断策略（按优先级）：
        1. 数据库中已有该消息且 role='assistant' → AI 代发
        2. 数据库中已有该消息且 is_bot=1 → AI 代发
        3. 兜底：通过内容匹配查找最近的 AI 回复
        """
        # 1. 通过 msg_id 检查数据库
        if msg.msg_id:
            try:
                cur = self.store.conv_conn(get_current_platform()).cursor()
                cur.execute(
                    "SELECT role, is_bot FROM messages WHERE msg_id = ?",
                    (msg.msg_id,),
                )
                row = cur.fetchone()
                if row:
                    return row["role"] == "assistant" or row["is_bot"] == 1
            except Exception as e:
                logger.debug("[轮询器] bot 消息检查失败: %s", e)

        # 2. 通过内容+时间匹配查找（msg_id 不一致时的兜底）
        #    修：只去前导 ## 标记（不要去 **，因为中间成对 ** 是内容的一部分）
        #    加 ±120s 时间窗与 _is_duplicate_self_message 对齐
        if msg.content and msg.chat_id:
            try:
                cur = self.store.conv_conn(get_current_platform()).cursor()
                # 只去掉前导 markdown 标题符（## ### 等），不去 ** 因为中间成对 ** 是粗体内容
                norm = re.sub(r'^#+\s*', '', msg.content).strip()
                content_prefix = norm[:50]
                ts_str = msg.timestamp.isoformat() if hasattr(msg.timestamp, 'isoformat') else str(msg.timestamp)
                cur.execute(
                    "SELECT role, is_bot FROM messages "
                    "WHERE chat_id = ? AND role = 'assistant' "
                    "  AND content LIKE ? "
                    "  AND ABS(julianday(timestamp) - julianday(?)) < 0.00139 "
                    "LIMIT 1",
                    (msg.chat_id, f"{content_prefix}%", ts_str),
                )
                row = cur.fetchone()
                if row:
                    return True
            except Exception as e:
                logger.debug("[轮询器] 内容匹配去重查询失败: %s", e)

        return False

    def _is_duplicate_self_message(self, msg: Message) -> bool:
        """判断自己发的消息是否已存在（防止 assistant 回复双写）。

        轮询器拉到同一回复的真实 DWS msg_id 时不应再存。
        匹配条件：同一 chat_id + role=assistant + 内容前缀匹配 + 时间接近（±120s）。
        修：只去前导 ## 标记（不去 **，中间成对 ** 是内容）。
        """
        if not msg.content or not msg.chat_id:
            return False
        try:
            cur = self.store.conv_conn(get_current_platform()).cursor()
            # 只去掉前导 markdown 标题符（## ### 等），不去 ** 因为中间成对 ** 是粗体内容
            norm = re.sub(r'^#+\s*', '', msg.content).strip()
            prefix = norm[:60]
            # 用时间窗口缩小匹配范围，避免误杀不同时间的相似回复
            ts_str = msg.timestamp.isoformat() if hasattr(msg.timestamp, 'isoformat') else str(msg.timestamp)
            cur.execute(
                """SELECT id FROM messages
                   WHERE chat_id = ? AND role = 'assistant'
                     AND content LIKE ?
                     AND ABS(julianday(timestamp) - julianday(?)) < 0.00139
                   LIMIT 1""",
                (msg.chat_id, f"{prefix}%", ts_str),
            )
            # 0.00139 ≈ 120 秒 / 86400
            return cur.fetchone() is not None
        except Exception as e:
            logger.debug("[轮询器] 重复消息检查失败: %s", e)
            return False

    def _resolve_single_chat_peer(self, open_id: str, title: str) -> dict:
        """查找单聊对方的 userId / openDingTalkId。

        查找顺序：
        1. 数据库会话缓存（之前成功解析过）
        2. 外部好友映射表（手动添加的非组织成员）
        3. DWS chat_conversation_info（直接获取会话详情，含对方信息）
        4. DWS contact_user_search（组织内联系人，用 title 搜索）

        重要：所有 user_id 必须以 ``ou_`` 开头才合法。飞书 Bot/应用类型
        会话（cli_xxx）、会话级 chat_id（oc_xxx）等污染值会被过滤，
        避免后续 ``chat_message_list_direct`` 调用 ``lark-cli --user-id``
        时报 "invalid user ID format, should start with 'ou_'"。
        """
        def _is_valid_ou(value: str) -> bool:
            return bool(value) and str(value).startswith("ou_")

        # 1. 从数据库缓存取
        conv = self.store._conversation_repo.get_conversation(open_id)
        if conv:
            uid = conv.get("peer_user_id") or ""
            oid = conv.get("peer_open_dingtalk_id") or ""
            # 防御：DB 中可能残留 Bot/应用 ID（cli_xxx）或被误写的 chat_id（oc_xxx）
            # 两者均非合法 ou_ 格式，对 user_id 和 open_dingtalk_id 都要校验。
            if uid and not _is_valid_ou(uid):
                logger.debug(
                    "[轮询器] 过滤非法 peer_user_id=%s（仅 ou_ 格式合法），"
                    "降级为按 oid 解析", uid[:24],
                )
                uid = ""
            if oid and not _is_valid_ou(oid):
                logger.debug(
                    "[轮询器] 过滤非法 peer_open_dingtalk_id=%s（仅 ou_ 格式合法），"
                    "聊天对象可能是飞书 Bot", oid[:24],
                )
                oid = ""
            if uid or oid:
                # 纠错：external_friends 表是外部好友的权威来源。
                # DB 缓存可能在 external_friends 注册前被错误写入（如通过
                # contact_user_search 误匹配到机器人自己），导致 peer_open_dingtalk_id
                # 污染。这里交叉校验，若 external_friends 有不同值则覆盖纠正。
                if title:
                    ef = self.store._external_friend_repo.get_external_friend_by_name(title)
                    if ef:
                        ef_oid = ef.get("open_dingtalk_id", "")
                        if ef_oid and ef_oid != oid:
                            logger.debug(
                                "[轮询器] 纠错：外部好友 '%s' 的 peer_open_dingtalk_id "
                                "DB缓存=%s 外部好友=%s，以外部好友为准",
                                title, oid[:24], ef_oid[:24],
                            )
                            self.store._conversation_repo.upsert_conversation(
                                open_id, title, "single",
                                peer_open_dingtalk_id=ef_oid,
                            )
                            return {"user_id": uid, "open_dingtalk_id": ef_oid}
                return {"user_id": uid, "open_dingtalk_id": oid}

        # 2. 外部好友映射表（非组织内成员，如钉钉外部好友）
        if title:
            ef = self.store._external_friend_repo.get_external_friend_by_name(title)
            if ef:
                logger.debug("[轮询器] 通过映射找到外部好友 '%s'：%s",
                            title, ef.get("open_dingtalk_id"))
                self.store._conversation_repo.upsert_conversation(
                    open_id, title, "single",
                    peer_open_dingtalk_id=ef.get("open_dingtalk_id", ""),
                )
                return {
                    "user_id": "",
                    "open_dingtalk_id": ef.get("open_dingtalk_id", ""),
                }

        # 3. 用 chat_conversation_info 直接获取会话详情（最可靠的方式）
        #    注意：该接口需要组织级权限。外部好友 / 跨组织单聊会稳定返回权限错误，
        #    但这不代表"无法收发消息"——list-all 主通道仍能投递其消息，且对方
        #    openDingTalkId 可从消息的 sender 字段直接获取。因此该接口失败
        #    【绝不拉黑】，仅记入内存 _metadata_unavailable 避免每轮空转重试。
        if open_id in self._metadata_unavailable:
            logger.debug("[轮询器] 跳过 chat_conversation_info（已知元数据不可用，外部好友/跨组织单聊常见）: %s", open_id[:30])
        else:
            try:
                conv_info = self.dws.chat_conversation_info(open_id)
                if conv_info:
                    # 单聊的会话详情里，title 通常是对方姓名
                    # 有些版本还会直接返回对方的 openDingTalkId 或 userId
                    remote_title = conv_info.get("title") or conv_info.get("name") or ""
                    remote_oid = conv_info.get("openDingTalkId") or conv_info.get("peerOpenDingTalkId") or ""
                    remote_uid = conv_info.get("userId") or conv_info.get("peerUserId") or ""

                    if remote_title and not title:
                        title = remote_title  # 补全 title，供后续搜索使用

                    # 过滤：userId/openDingTalkId 必须是 ou_ 前缀才合法。
                    # Bot/应用（cli_xxx）和会话级 chat_id（oc_xxx）都不是有效用户 ID。
                    if remote_uid and not _is_valid_ou(remote_uid):
                        logger.debug(
                            "[轮询器] 过滤非法 userId=%s（仅 ou_ 格式合法），"
                            "聊天对象可能是飞书 Bot，不存为 peer_user_id",
                            remote_uid[:24],
                        )
                        remote_uid = ""
                    if remote_oid and not _is_valid_ou(remote_oid):
                        logger.debug(
                            "[轮询器] 过滤非法 openDingTalkId=%s（仅 ou_ 格式合法），"
                            "聊天对象可能是飞书 Bot", remote_oid[:24],
                        )
                        remote_oid = ""

                    if remote_oid or remote_uid:
                        logger.debug("[轮询器] 通过会话详情解析到对方 %s：uid=%s, oid=%s",
                                    open_id, remote_uid, remote_oid)
                        self.store._conversation_repo.upsert_conversation(
                            open_id, title or remote_title,
                            "single",
                            peer_user_id=remote_uid,
                            peer_open_dingtalk_id=remote_oid,
                        )
                        return {"user_id": remote_uid, "open_dingtalk_id": remote_oid}
            except Exception as e:
                if self._is_global_permission_error(e):
                    # 跨组织会话：当前 DWS profile 无该组织权限 → 抛出，
                    # 交由主循环统一持久化跳过（避免每轮重试反复触发权限验证/弹窗）
                    raise
                if self._is_permission_error(e):
                    # ⚠️ 关键修正：chat_conversation_info 权限失败 ≠ 无法收发消息。
                    # 外部好友 / 跨组织单聊调该接口必失败，但消息通道正常。
                    # 因此【不拉黑】，仅记入内存集合避免重复调用，改用消息 sender 兜底。
                    self._metadata_unavailable.add(open_id)
                    logger.debug(
                        "[轮询器] chat_conversation_info 无权限（外部好友/跨组织单聊常见，"
                        "不影响收发），跳过元数据解析: %s | %s", open_id[:30], e
                    )
                    # 不 return，继续走第 4 步联系人搜索兜底
            logger.debug("[轮询器] chat_conversation_info 未能解析 %s 的会话详情（尝试联系人搜索兜底）", open_id)

        # 4. 通过 DWS 联系人搜索查找（仅对组织内成员有效）
        if not title:
            logger.debug("[轮询器] 无法解析对方 %s：缺少标题（会话详情获取失败，且不是外部好友）", open_id)
            return {"user_id": "", "open_dingtalk_id": ""}
        try:
            results = self.dws.contact_user_search(title)
            if results:
                # ⚠️ 重名防护：组织内可能存在同名用户（如两个「张旭」）。
                # 若按姓名搜索返回多个候选，无法仅凭姓名确定唯一对象，
                # 此时【绝不】把 results[0] 写入 DB 缓存（否则会持久化错误的
                # peer_open_dingtalk_id，导致回复路由到错误的同名用户）。
                # 返回 best-effort 供本次回复尝试，但不 upsert 缓存；
                # 真正的唯一 peer 由消息 sender_id（discovery / 发送时 P2 纠错）解析。
                if len(results) > 1:
                    cand_ids = [
                        r.get("openDingTalkId") or r.get("userId") or "?"
                        for r in results
                    ]
                    logger.warning(
                        "[轮询器] 联系人 '%s' 搜索命中 %d 个同名候选（%s），"
                        "跳过按姓名写库（改由消息 sender_id 唯一解析，避免回复错人）",
                        title, len(results), cand_ids,
                    )
                    first = results[0]
                    uid = first.get("userId", "") if _is_valid_ou(first.get("userId", "")) else ""
                    oid = first.get("openDingTalkId", "")
                    return {"user_id": uid, "open_dingtalk_id": oid}  # 不缓存
                first = results[0]
                uid = first.get("userId", "") if _is_valid_ou(first.get("userId", "")) else ""
                oid = first.get("openDingTalkId", "")
                if uid or oid:
                    logger.debug("[轮询器] 通过联系人搜索解析到对方 '%s'：uid=%s, oid=%s", title, uid, oid)
                    self.store._conversation_repo.upsert_conversation(
                        open_id, title, "single",
                        peer_user_id=uid,
                        peer_open_dingtalk_id=oid,
                    )
                    return {"user_id": uid, "open_dingtalk_id": oid}
        except Exception as e:
            logger.warning("搜索联系人 '%s' 失败：%s", title, e)

        logger.debug(
            "[轮询器] 无法解析对方 '%s'（会话 %s）："
            "未在联系人列表或外部好友映射中找到。"
            "如果是外部好友（非组织内成员），请通过 API 添加：POST /api/external-friends",
            title, open_id,
        )
        return {"user_id": "", "open_dingtalk_id": ""}
