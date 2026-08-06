from __future__ import annotations

import logging
from datetime import datetime, timedelta
from src.models import Message
from src.poller_mixins_base import PollerMixinBase

logger = logging.getLogger(__name__)










class _SyncCancelled(Exception):
    """由 cancel_check 在同步过程中抛出，用于优雅中止历史同步（前端「取消」按钮）。"""
    pass


class HistorySyncMixin(PollerMixinBase):
    # 分窗参数：窗长取 7 天，避免活跃组织单窗消息量超过 list-all 50 页上限
    # （50 页 × 100 条/页 = 5000 条/窗）导致截断漏消息（2026-08-03 事故：
    # range 模式 days=24 单窗触顶，窗口内消息未拉全）。
    SYNC_WINDOW_DAYS = 7           # 每个时间窗跨度（天）
    SYNC_FULL_LOOKBACK_DAYS = 730  # full 模式最远回溯（天，约 2 年；DWS 服务端若更短会提前为空）

    def _build_sync_windows(self, now: datetime, days_back: int | None):
        """生成时间窗列表 [(start_str, end_str), ...]，按从新到旧排序。

        Args:
            now: 当前时间（调用方传入，保证整次同步时间基准一致）。
            days_back: 指定回溯天数（range/conversation 模式）；None 表示 full 模式
                （逐 SYNC_WINDOW_DAYS 天窗从今天往回，直到达到 SYNC_FULL_LOOKBACK_DAYS 下限）。

        range 模式同样按 SYNC_WINDOW_DAYS 拆窗（旧实现单窗直拉 days_back 全量，
        活跃组织下宽窗口单窗触顶 list-all 分页上限而截断漏消息）。
        """
        windows = []
        cursor = now
        if days_back is not None:
            floor = now - timedelta(days=max(1, days_back))
        else:
            floor = now - timedelta(days=self.SYNC_FULL_LOOKBACK_DAYS)
        while cursor > floor:
            win_end = cursor
            win_start = cursor - timedelta(days=self.SYNC_WINDOW_DAYS)
            if win_start < floor:
                win_start = floor
            windows.append((
                win_start.strftime("%Y-%m-%d %H:%M:%S"),
                win_end.strftime("%Y-%m-%d %H:%M:%S"),
            ))
            cursor = win_start
        return windows

    def sync_history(self, days_back: int = 7, conversation_id: str | None = None,
                     full: bool = False, chat_types: list[str] | None = None,
                     progress_cb=None, cancel_check=None) -> dict:
        """手动同步历史消息到数据库（用于补齐对话列表显示）。

        与 poll_once() 的关键区别：
        1. 使用独立的时间窗口，不依赖 _last_list_all_time 游标，
           避免正常轮询把游标推到最新后手动同步拉到 0 条。
        2. 不过滤「需要 AI 回复」的消息——自己的消息、系统消息、未 @ 我的群消息、
           图片消息等全部保存到数据库，确保前端对话列表显示完整。
        3. 不触发 AI 回复，不更新 _last_list_all_time（避免干扰正常轮询）。
        4. 仍保留安全过滤：黑名单会话、不可访问会话、无 msg_id 的消息。

        三种模式（按优先级）：
        - conversation：传了 conversation_id → 只同步该会话（其余会话在窗口内被客户端过滤掉）。
        - full：full=True → 全部历史，逐 30 天窗从今天往回拉，绕开 list_all 20 页上限。
        - range：默认 → 固定 days_back 窗口（兼容旧调用）。

        Args:
            days_back: range/conversation 模式向前回溯的天数（默认 7 天）。
            conversation_id: 仅同步该 openConversationId（conversation 模式）。
            full: True 表示全部历史（忽略 days_back）。
            chat_types: 仅同步指定类型 ["single","group"]，None 表示全部。
            progress_cb: 可选回调，每个时间窗处理完后调用
                progress_cb(window_index, total_windows, saved_so_far, fetched_so_far)，
                用于子进程上报分窗进度。
            cancel_check: 可选无参可调用对象，返回 True 时在每个时间窗开始前抛出
                _SyncCancelled 以优雅中止（前端「取消」按钮依赖它）。

        Returns:
            {"total": 拉取总数, "saved": 新保存数, "skipped_dup": 去重跳过数,
             "skipped_block": 黑名单/不可访问跳过会话数, "fixed_direction": 修复方向条数,
             "windows": 时间窗数, "mode": 模式名}
        """
        now = datetime.now()
        if conversation_id:
            mode = "conversation"
            # 单会话：用单个超长窗口一次拉全（按 chat_ids_filter 客户端过滤后，
            # 单会话不会超过 list_all 的 20 页上限，无需逐窗；比 full 模式省 24 次 DWS 调用）
            windows = self._build_sync_windows(now, self.SYNC_FULL_LOOKBACK_DAYS)
            chat_ids_filter = {conversation_id}
        elif full:
            mode = "full"
            windows = self._build_sync_windows(now, None)
            chat_ids_filter = None
        else:
            mode = "range"
            windows = self._build_sync_windows(now, days_back)
            chat_ids_filter = None

        logger.info(
            "[同步] 模式=%s，共 %d 个时间窗（每窗 %d 天）", mode, len(windows), self.SYNC_WINDOW_DAYS
        )

        agg = {"total": 0, "saved": 0, "skipped_dup": 0, "skipped_block": 0}
        for idx, (start_str, end_str) in enumerate(windows):
            # 取消检查：每个时间窗开始前询问一次（时间窗是 I/O 边界，足够及时）
            if cancel_check and cancel_check():
                logger.info("[同步] 收到取消信号，在第 %d/%d 窗前中止", idx + 1, len(windows))
                raise _SyncCancelled(f"cancelled at window {idx + 1}/{len(windows)}")
            try:
                r = self._sync_history_window(
                    start_str, end_str,
                    chat_ids_filter=chat_ids_filter,
                    chat_types=chat_types,
                )
            except Exception as e:  # noqa: BLE001
                # 单个时间窗失败不应中断整次同步（如某窗 DWS 超时）；记录后继续下一窗
                logger.error("[同步] 时间窗 %s~%s 处理失败（跳过）: %s", start_str, end_str, e)
                if progress_cb:
                    try:
                        progress_cb(idx, len(windows), agg["saved"], agg["total"])
                    except Exception as _exc:  # noqa: BLE001
                        logger.debug(f"sync_history: swallowed exception: {_exc}")
                        pass
                continue
            agg["total"] += r.get("total", 0)
            agg["saved"] += r.get("saved", 0)
            agg["skipped_dup"] += r.get("skipped_dup", 0)
            agg["skipped_block"] += r.get("skipped_block", 0)
            if progress_cb:
                try:
                    progress_cb(idx, len(windows), agg["saved"], agg["total"])
                except Exception as _exc:  # noqa: BLE001
                    logger.debug(f"sync_history: swallowed exception: {_exc}")
                    pass

        # 修复历史错误数据：早期 sync_history 把自己发的消息存成 role='user'，
        # 这里统一修正为 'assistant'，避免前端方向显示错误。
        fixed = 0
        try:
            fixed = self.store._message_repo.fix_self_message_roles(
                self.current_user_id,
                self.current_user_name,
                self.current_user_user_id,
            )
        except Exception as e:
            logger.warning("[同步] 修复方向错误消息失败: %s", e)

        logger.info(
            "[同步] 手动同步完成（%s）：拉取 %d 条，新保存 %d 条，去重跳过 %d 条，"
            "会话跳过 %d 个，修复方向 %d 条，共 %d 窗",
            mode, agg["total"], agg["saved"], agg["skipped_dup"],
            agg["skipped_block"], fixed, len(windows),
        )
        return {
            "total": agg["total"],
            "saved": agg["saved"],
            "skipped_dup": agg["skipped_dup"],
            "skipped_block": agg["skipped_block"],
            "fixed_direction": fixed,
            "windows": len(windows),
            "mode": mode,
        }

    def _sync_history_window(self, start_str: str, end_str: str,
                             chat_ids_filter: set[str] | None = None,
                             chat_types: list[str] | None = None) -> dict:
        """处理单个时间窗：拉取 list-all 结果，按 chat_ids_filter / chat_types 过滤后落库。

        Returns:
            该窗的部分统计 {"total","saved","skipped_dup","skipped_block"}
        """
        try:
            result = self.dws.chat_message_list_all(start_str, end_str, limit=100)
        except Exception as e:
            logger.error("[同步] list-all 调用失败(%s~%s): %s", start_str, end_str, e)
            raise

        conv_list = result.get("conversationMessagesList", []) if isinstance(result, dict) else []
        # 历史同步阶段也把死会话拉黑（如已退群、被踢），避免后续每轮重复请求
        if isinstance(result, dict) and result.get("blocked_chats"):
            nb = self._block_chats_from_list_all(result, source="feishu_history")
            if nb:
                logger.info("[同步] 已将 %d 个不可达会话拉入黑名单", nb)
        logger.info("[同步] list-all 返回 %d 个会话（窗 %s~%s）", len(conv_list), start_str, end_str)

        total = 0
        saved = 0
        skipped_dup = 0
        skipped_conv = 0

        for conv in conv_list:
            conv_id = conv.get("openConversationId", "")
            title = conv.get("title", "")
            chat_type = self._detect_chat_type(conv)
            # 飞书自动纠错：以 API chat_mode 为准修正 DB
            chat_type = self._feishu_correct_chat_type(conv_id, title, chat_type)
            is_single = chat_type == "single"
            msgs = conv.get("messages", [])

            # 会话级过滤：conversation 模式只保留目标会话
            if chat_ids_filter is not None and conv_id not in chat_ids_filter:
                continue
            # 类型过滤（single/group）
            if chat_types and chat_type not in chat_types:
                continue

            # 安全过滤：跳过不可访问会话（退群/被踢等，避免触发权限错误）
            if self._inaccessible_conversations and conv_id in self._inaccessible_conversations:
                logger.debug("[同步] 跳过不可访问会话: %s", (title or conv_id)[:20])
                skipped_conv += 1
                continue
            # 安全过滤：跳过规则引擎黑名单会话（配置级完全跳过）
            if self._is_blacklisted_conversation(title, chat_type):
                logger.debug("[同步] 跳过黑名单会话: %s", (title or conv_id)[:20])
                skipped_conv += 1
                continue
            if not msgs:
                continue

            # 更新会话缓存；单聊回填对方 openDingTalkId（取第一条非自己的消息 sender）
            peer_oid = ""
            if is_single:
                for raw_msg in msgs:
                    sid = raw_msg.get("senderOpenDingTalkId") or raw_msg.get("senderId") or ""
                    if sid and not self._is_self_sender(sid):
                        peer_oid = sid
                        break
            self.store._conversation_repo.upsert_conversation(
                conv_id, title, chat_type, peer_open_dingtalk_id=peer_oid,
            )

            for raw in msgs:
                total += 1
                msg = self._raw_to_message(raw, conv_id, chat_type, title)
                if not msg.msg_id:
                    continue

                # 去重：已处理过的消息跳过（避免重复保存/标记）
                if self.store._message_repo.is_message_processed(msg.msg_id):
                    skipped_dup += 1
                    continue

                # 角色判定：自己发的 → assistant（右侧，is_bot 区分 AI/真人）；别人发的 → user（左侧）
                if self._is_self_message(msg):
                    # 手动同步历史时本地可能尚未保存该消息，_check_if_bot_message 兜底可能误判为真人。
                    # 安全做法：自己发送的消息统一 role=assistant，is_bot 交给 _check_if_bot_message 判断。
                    msg.is_bot = self._check_if_bot_message(msg)
                    role = "assistant"
                else:
                    role = "user"

                try:
                    self.store._message_repo.save_message(msg, role)
                    saved += 1
                except Exception as e:
                    logger.warning("[同步] 保存消息失败(msg_id=%s): %s",
                                  msg.msg_id[:20] if msg.msg_id else "", e)
                    continue

                # 标记为已处理：避免下次轮询重复保存/触发 AI 回复历史消息
                self._mark_msg_processed(msg.msg_id, msg.chat_id, msg=msg)

        return {
            "total": total,
            "saved": saved,
            "skipped_dup": skipped_dup,
            "skipped_block": skipped_conv,
        }


    def _handle_edit_message(self, msg: Message) -> None:
        """处理消息编辑事件：更新本地消息记录。

        编辑消息通常包含原始消息的引用信息或新内容，需要从中提取被编辑的原消息ID
        和新内容，然后更新本地数据库中的对应记录。
        """
        raw = msg.raw or {}

        original_msg_id = raw.get("originalMsgId") or raw.get("targetMsgId") or ""
        if not original_msg_id:
            original_msg_id = msg.msg_id

        new_content = raw.get("newContent") or raw.get("content") or msg.content

        if original_msg_id and new_content:
            success = self.store._message_repo.update_message(original_msg_id, new_content)
            if success:
                logger.info("[轮询器] 消息编辑已处理，更新消息 %s", original_msg_id[:20])
            else:
                logger.warning("[轮询器] 消息编辑处理失败，未找到消息 %s", original_msg_id[:20])
        else:
            logger.debug("[轮询器] 消息编辑：缺少原消息ID或新内容，忽略")


    def _handle_recall_message(self, msg: Message) -> None:
        """处理消息撤回事件：删除本地消息记录。

        撤回消息通常包含被撤回消息的引用信息，需要从中提取被撤回的消息ID，
        然后删除本地数据库中的对应记录。
        """
        raw = msg.raw or {}

        recalled_msg_id = raw.get("recalledMsgId") or raw.get("targetMsgId") or raw.get("originalMsgId") or ""
        if not recalled_msg_id:
            recalled_msg_id = msg.msg_id

        if recalled_msg_id:
            success = self.store._message_repo.delete_message(recalled_msg_id)
            if success:
                logger.info("[轮询器] 消息撤回已处理，删除消息 %s", recalled_msg_id[:20])
            else:
                logger.warning("[轮询器] 消息撤回处理失败，未找到消息 %s", recalled_msg_id[:20])
        else:
            logger.debug("[轮询器] 消息撤回：缺少被撤回消息ID，忽略")

        # 标记撤回消息本身为已处理，防止轮询器重复拉取导致反复告警
        raw_id = raw.get("openMessageId") or raw.get("msgId") or ""
        if raw_id:
            self.store._message_repo.mark_message_processed(raw_id, msg.chat_id)


