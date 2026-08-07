from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
import functools
import logging

from src.poller_utils import is_read_receipt_content

logger = logging.getLogger("src.platform.runtime")

# F15：分片之间的发送间隔（秒）。DWS CLI 是同步调用，理论上顺序有保证，
# 但服务端入库时间戳粒度可能相同导致客户端乱序展示，留一个极小的间隔更稳。
SHARD_SEND_INTERVAL_SECONDS = 0.2

# F14：回复发送退避相关回落默认（config 缺字段时）。一般不应走到这里。
REPLY_SEND_MIN_INTERVAL_DEFAULT = 0.2
REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT = 60.0
# 限频信号文本嗅探（钉钉把 429/"rate limit exceeded" 归类为不可重试错误，
# 而非 IMAdapterRateLimitError，故需文本兜底才能识别流控）。
_RATE_LIMIT_HINTS = ("rate limit", "ratelimit", "429", "rate_limit",
                     "频控", "too many requests", "throttl", "quota exceeded")



class ReplyDispatchMixin(EngineMixinBase):
    """运行时：dispatch 相关方法（从 runtime.py 抽离，零行为变更）。"""
    def _dispatch_reply_send(self, message: Message, reply_title: str,
                             filtered: str, reply_uuid: str) -> tuple[bool, object]:
        """按会话类型分发实际 DWS 发送。返回 (是否已发出, DWS 返回值)。

        护栏拦截 / peer 信息缺失时内部完成「标记已处理」并返回 (False, None)；
        DWS 异常原样上抛，由 _send_reply 统一做退避处理。
        """
        if message.chat_type == "group":
            logger.info("[发送] 群聊回复: group=%s, text=%s", message.chat_id, filtered[:50])
            # === 原生引用回复（群聊 UX 优化）===
            # 与单聊对称：用 dws 原生 chat message reply 在群内展示「引用气泡」，
            # 让群里的人直观看到 AI 在回复哪条消息。仅当被回复消息的 id + 发送者 +
            # 会话 id 齐全且非富媒体时启用；原生失败（参数缺失/接口异常）时
            # fallback_to_send=False 抛异常，落到下方 _send_possibly_sharded 分片发送，
            # 避免重复发送、保证回复永不丢失。
            _ref_msg_id = message.msg_id or ""
            _ref_sender = message.sender_id or ""
            _conv_id = message.chat_id or ""
            if _ref_msg_id and _ref_sender and _conv_id:
                try:
                    logger.info("[发送] 尝试原生引用回复（群聊）: chat_id=%s ref=%s",
                                _conv_id, _ref_msg_id[:20])
                    native_result = self.dws.chat_message_reply(
                        text=filtered, title=reply_title, uuid=reply_uuid,
                        ref_msg_id=_ref_msg_id, ref_sender=_ref_sender,
                        conversation_id=_conv_id, fallback_to_send=False,
                    )
                    logger.info("[发送] 原生引用回复 DWS 返回: %s", native_result)
                    return True, native_result
                except Exception as e:
                    logger.warning("[发送] 原生引用回复（群聊）失败，降级为分片发送: %s", e)
                    # 落到下方 _send_possibly_sharded 分支
            result = self._send_possibly_sharded(
                chat_id=message.chat_id,
                reply_title=reply_title,
                filtered=filtered,
                reply_uuid=reply_uuid,
                group=message.chat_id,
            )
            logger.info("[发送] DWS 返回: %s", result)
            return True, result
        return self._send_single_chat_reply(message, reply_title, filtered, reply_uuid)
    def _send_single_chat_reply(self, message: Message, reply_title: str,
                                filtered: str, reply_uuid: str) -> tuple[bool, object]:
        """单聊发送：解析 peer、防自发护栏、脏数据纠错、按优先级选择发送通道。"""
        # 单聊：需要用 userId 或 openDingTalkId，从数据库缓存取
        conv = self.store._conversation_repo.get_conversation(message.chat_id)
        peer_user_id = (conv or {}).get("peer_user_id", "") or ""
        peer_oid = (conv or {}).get("peer_open_dingtalk_id", "") or ""

        # 【P1护栏】防「自发自」：peer_oid 命中机器人自己的 ID 时拒绝发送。
        # chat.py 的 SendMessageTool 已有此护栏，但 _send_reply 是另一条
        # 独立发送路径（非 agent tool 调用），且 DB 中的 peer_open_dingtalk_id
        # 可能在外部好友注册前被 contact_user_search 误写，缺乏纠错机会。
        # 此护栏作为最后防线：即使 DB 被污染，也绝不发给自己形成回声死循环。
        self_ids = [x for x in [getattr(self, "current_open_dingtalk_id", ""),
                                getattr(self, "current_user_id", "")] if x]
        if peer_oid and self_ids and peer_oid in self_ids:
            logger.warning(
                "[护栏 _send_reply] 拒绝发往自身：peer_oid=%s 命中机器人自己 ID"
                "（conversations.peer_open_dingtalk_id 可能被污染，chat_id=%s，"
                "sender_id=%s）。跳过本次回复并标记消息为已处理。",
                peer_oid, message.chat_id, message.sender_id,
            )
            self._mark_inbound_processed(message)
            return False, None

        # 如果缓存中没有 peer 信息，动态查询
        if not peer_oid and not peer_user_id and hasattr(self.poller, '_resolve_single_chat_peer'):
            resolved = self.poller._resolve_single_chat_peer(message.chat_id, message.chat_name)
            if resolved:
                peer_user_id = resolved.get("user_id", "")
                peer_oid = resolved.get("open_dingtalk_id", "")
                if peer_oid or peer_user_id:
                    logger.info("[发送] 动态查询到 peer: user_id=%s, oid=%s", peer_user_id, peer_oid)

        # 【P2纠错】单聊时 sender_id 就是消息发送者，回复理应发给ta。
        # 若 peer_oid（来自DB缓存）与 sender_id 不一致，说明 DB 的
        # peer_open_dingtalk_id 被污染（如 contact_user_search 误匹配到
        # 机器人自己）。此时以 sender_id 为准覆盖 peer_oid，并修复 DB。
        sender_id = message.sender_id or ""
        if (peer_oid and sender_id
                and sender_id.startswith("ou_")
                and sender_id != peer_oid
                and self_ids and sender_id not in self_ids):
            logger.warning(
                "[纠错 _send_reply] peer_oid=%s 与 sender_id=%s 不一致，"
                "以 sender_id 为准覆盖（DB中chat_id=%s的peer_open_dingtalk_id可能被污染）",
                peer_oid, sender_id, message.chat_id,
            )
            peer_oid = sender_id
            # 同步修复 DB，防止后续继续读到脏数据
            try:
                self.store._conversation_repo.upsert_conversation(
                    message.chat_id,
                    message.chat_name or "",
                    "single",
                    peer_open_dingtalk_id=sender_id,
                )
            except Exception as e:
                logger.warning("[纠错] DB 修复失败（不影响发送）: %s", e)

        logger.info("[发送] 单聊回复: chat_id=%s, chat_name=%s, peer_user_id=%s, peer_oid=%s, sender_id=%s",
                    message.chat_id, message.chat_name, peer_user_id, peer_oid, message.sender_id)

        # === 原生引用回复（单聊 UX 优化）===
        # 用 dws 原生 chat message reply 在会话内展示「引用气泡」。
        # 仅当被回复消息的 id + 发送者 + 会话 id 齐全时启用；原生失败（参数缺失 /
        # 接口异常）时 fallback_to_send=False 让其抛异常，落到下方 _send 分片发送
        # 分支（含外部好友 oc_ 处理 / peer 纠错），避免重复发送、保证回复永不丢失。
        _ref_msg_id = message.msg_id or ""
        _ref_sender = message.sender_id or ""
        _conv_id = message.chat_id or ""
        if _ref_msg_id and _ref_sender and _conv_id:
            try:
                logger.info("[发送] 尝试原生引用回复（单聊）: chat_id=%s ref=%s",
                            _conv_id, _ref_msg_id[:20])
                native_result = self.dws.chat_message_reply(
                    text=filtered, title=reply_title, uuid=reply_uuid,
                    ref_msg_id=_ref_msg_id, ref_sender=_ref_sender,
                    conversation_id=_conv_id, fallback_to_send=False,
                )
                logger.info("[发送] 原生引用回复 DWS 返回: %s", native_result)
                return True, native_result
            except Exception as e:
                logger.warning("[发送] 原生引用回复失败，降级为分片发送: %s", e)
                # 落到下方 _send 分支

        # 外部好友（跨租户）必须用 --chat-id oc_xxx 发送，而不能用
        # --user-id ou_xxx。后者触发飞书 230038 "cross tenant p2p chat operate forbid"。
        is_external = False
        if peer_oid:
            try:
                ef = self.store._external_friend_repo.get_external_friend_by_id(peer_oid)
                is_external = bool(ef)
            except Exception:
                logger.warning("[resilience] silent exception in _send_reply", exc_info=True)
        _send = functools.partial(
            self._send_possibly_sharded,
            chat_id=message.chat_id,
            reply_title=reply_title,
            filtered=filtered,
            reply_uuid=reply_uuid,
        )
        if is_external and message.chat_id.startswith("oc_"):
            logger.info("[发送] 外部好友（跨租户），使用 chat_id=%s 发送", message.chat_id)
            result = _send(group=message.chat_id)
            logger.info("[发送] DWS 返回: %s", result)
        elif peer_oid:
            logger.info("[发送] 使用 peer_oid=%s 发送", peer_oid)
            result = _send(open_dingtalk_id=peer_oid)
            logger.info("[发送] DWS 返回: %s", result)
        elif peer_user_id:
            logger.info("[发送] 使用 peer_user_id=%s 发送", peer_user_id)
            result = _send(user=peer_user_id)
            logger.info("[发送] DWS 返回: %s", result)
        else:
            # peer 信息为空，尝试用 sender_id 兜底（从收到消息里取对方 ID）
            sender_id = message.sender_id or ""
            if sender_id:
                logger.info("[发送] peer 信息为空，使用 sender_id=%s 兜底发送", sender_id)
                result = _send(open_dingtalk_id=sender_id)
                logger.info("[发送] DWS 返回: %s", result)
            else:
                logger.warning("无法发送回复：单聊 %s 的对方信息和 sender_id 均为空，跳过",
                               message.chat_name or message.chat_id)
                # peer 信息缺失属「永久跳过」：标记已处理防重轮询刷屏。
                self._mark_inbound_processed(message)
                return False, None
        return True, result
    def _record_reply_success(self, message: Message, filtered: str,
                              reply_uuid: str, result) -> None:
        """发送成功后的记账：去重标记、冷却时间、防重复 msg_id、持久化 AI 回复、合并消息补标。"""
        # 提取真实 msg_id（DWS 返回的 openTaskId）并标记去重
        real_msg_id = None
        if not self.dws.dry_run and result and isinstance(result, dict):
            real_msg_id = (result.get("result") or {}).get("openTaskId", None)
            if real_msg_id:
                try:
                    self.poller._mark_msg_processed(real_msg_id, message.chat_id)
                    logger.info("[去重] 已标记 AI 回复为已处理: %s", real_msg_id[:30])
                except Exception as me:
                    logger.warning("[去重] 标记失败: %s", me)

        # 更新最后回复时间（用于回复冷却）
        try:
            self.store._conversation_repo.update_last_reply_time(message.chat_id, message.chat_type)
        except Exception as e:
            logger.warning("[冷却] 更新回复时间失败: %s", e)

        # 记录「最后回复过的用户消息 msg_id」（基于消息 ID 的防重复回复）
        # 同时立即标记用户消息的去重（与防重复原子化，避免 handler 后续异常导致漏标）
        try:
            msg_key = message.msg_id or (message.raw.get("alt_id") if isinstance(message.raw, dict) else "") or ""
            if msg_key:
                self.store._conversation_repo.update_last_replied_msg_id(message.chat_id, msg_key, message.chat_type)
                try:
                    self.poller._mark_msg_processed(msg_key, message.chat_id)
                    logger.info("[去重] 回复成功后已标记用户消息为已处理: %s", msg_key[:30])
                except Exception as de:
                    logger.warning("[去重] 标记用户消息失败: %s", de)
        except Exception as e:
            logger.warning("[防重复] 更新已回复消息 ID 失败: %s", e)

        # 【H4修复】移除 time.sleep(2) 阻塞验证。原逻辑在发送后 sleep 2s 再查消息列表验证，
        # 但 2s 阻塞整个回复线程代价过高（拖慢回复速度、占用线程池），且验证结果仅打日志
        # 不影响流程。改为依赖 DWS 返回值判断成功与否（result 含 openTaskId 即为发送成功）。
        # 如需确认真伪，可后续在轮询中自然拉到自己的回复来验证。

        # 持久化 AI 回复到数据库
        from src.models import Message as MessageModel
        ai_message = MessageModel(
            msg_id=reply_uuid,
            chat_id=message.chat_id,
            chat_type=message.chat_type,
            chat_name=message.chat_name,
            sender_id=self.current_open_dingtalk_id or self.current_user_id or "ai",
            sender_name=self.current_user_name or "AI助手",
            content=filtered,
            msg_type="text",
            timestamp=datetime.now(),
            raw={},
            is_bot=True,
        )
        self.store._message_repo.save_message(ai_message, role="assistant")

        # 【关键修复】立即标记 AI 回复为已处理，避免下一轮轮询拉取自己的消息
        try:
            self.poller._mark_msg_processed(reply_uuid, message.chat_id)
            logger.info("[去重] 已标记 AI 回复为已处理: %s", reply_uuid[:20])
        except Exception as e:
            logger.warning("[去重] 标记 AI 回复为已处理失败: %s", e)

        # 【合并消息修复】如果是合并消息，仅 message.msg_id（最后一条）会被
        # 常规流程标记。这里补标 original_ids 中所有原始 msg_id，防止下一轮
        # 轮询再次拉取合并批次中较早的消息导致重复回复。
        original_ids = None
        if isinstance(message.raw, dict):
            # 兼容两套合并路径的 key：poller._combine_message_group 用 original_ids，
            # poller_utils.merge_consecutive_messages 用 merged_original_ids。
            original_ids = message.raw.get("original_ids") or message.raw.get("merged_original_ids")
        if original_ids:
            for oid in original_ids:
                if oid:
                    try:
                        self.poller._mark_msg_processed(oid, message.chat_id)
                    except Exception as e:
                        logger.warning("[去重] 标记合并消息原始ID失败: %s", e)
            logger.info("[去重] 合并消息已批量标记 %d 条原始消息为已处理", len(original_ids))
    def _send_reply(self, message: Message, reply_text: str) -> bool:
        # 硬闸门：入站为「已读」回执消息时绝不回复。结构层（_detect_msg_type +
        # skip_msg_types 已优先拦截并丢弃），此处作为发送前最后兜底，防止任何绕过
        # 分类层的消息（如合并批次、工具自回复旁路）触达对方。标记已处理避免重轮询刷屏。
        _mt = getattr(message, "msg_type", None)
        _content = getattr(message, "content", None)
        if _mt == "read_receipt" or is_read_receipt_content(_content):
            logger.info("[已读闸门] 入站为已读回执消息，跳过发送（来自 %s）", message.sender_name)
            self._mark_inbound_processed(message)
            return False

        # 瞬时发送失败退避：DWS 异常后短时间内不再对同一条消息硬刷重发，
        # 避免每轮轮询(5s)反复调用 DWS 失败刷日志。
        msg_key = message.msg_id or (message.raw.get("alt_id") if isinstance(message.raw, dict) else "") or ""
        if msg_key and msg_key in self._send_backoff_until and time.time() < self._send_backoff_until[msg_key]:
            return False

        # F14：平台级限频护栏。若刚命中限频（_reply_rate_limited_until 未过期），
        # 暂停本轮剩余回复，避免继续高频触达加剧限流。下轮越过窗口后自动恢复。
        if self._reply_rate_limited():
            return False

        # === 回复冷却检查 ===
        # 防止短时间内对同一会话反复回复（比如对方连发多条消息时）
        if self._reply_cooldown_active(message):
            return False

        filtered = self._filter_sensitive_words(reply_text)
        if filtered is None:
            self._handle_sensitive_blocked_reply(message, reply_text)
            return False

        prepared = self._prepare_outgoing_text(filtered, message)
        if prepared is None:
            return False
        reply_title, filtered = prepared

        reply_uuid = str(uuid.uuid4())

        # === 发送前最后一刻门控复核 ===
        # 入站已判过一次（_handle_message_with_rid），但 LLM 生成耗时长，人工可能
        # 在此期间回复/在场。此处再判，杜绝穿插真人对话（核心门控修复）。
        # 不通过则标记已处理并放弃发送，避免该消息被反复轮询重试而刷屏。
        if not self._should_reply_now(message):
            logger.info("[门控] 发送前复核未通过，标记已处理并放弃发送（来自 %s）",
                        message.sender_name)
            self._mark_inbound_processed(message)
            return False

        self._mark_read_before_reply(message)

        # AI 标记（--ai-tag）由 DwsAdapter.ai_tag_default 统一控制（来自 config.poller.ai_tag_enabled）
        try:
            sent, result = self._dispatch_reply_send(message, reply_title, filtered, reply_uuid)
            if not sent:
                return False
            # 【关键修复】非 dry_run 模式下校验 DWS 返回值。若返回 None 或非 dict，
            # 说明发送静默失败，不应标记已处理（否则用户消息被永久丢弃）。
            if not self.dws.dry_run and not (result and isinstance(result, dict)):
                logger.error("[发送] DWS 返回异常结果: %s，不标记已处理，下轮重试", result)
                if msg_key:
                    self._send_backoff_until[msg_key] = time.time() + SEND_RETRY_BACKOFF_SECONDS
                return False

            logger.info("回复已发送至 %s (%s)", message.chat_name or message.chat_id,
                        "dry-run" if self.dws.dry_run else "real")

            self._record_reply_success(message, filtered, reply_uuid, result)
            return True
        except Exception as e:
            if self._is_rate_limit_exception(e):
                # F14：命中平台限频（含钉钉把 429 归类为不可重试的情况）。置较长退避
                # + 全局限频护栏（暂停本轮剩余回复），避免在已被限流时继续高频触达、
                # 加剧限流与失败刷屏。消息不标记已处理，下轮越过退避窗口后自动重试。
                self._handle_reply_rate_limited(msg_key, e)
                return False
            logger.error("回复发送失败: %s", e)
            # 瞬时发送失败（DWS/网络异常）：不标记入站消息（允许下一轮重试），
            # 但置退避窗口避免每轮轮询(5s)硬刷重发刷日志。
            if msg_key:
                self._send_backoff_until[msg_key] = time.time() + SEND_RETRY_BACKOFF_SECONDS
            return False
