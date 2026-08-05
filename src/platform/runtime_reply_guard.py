from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .reply_shard import REPLY_SHARD_LIMIT_DEFAULT, shard_reply_text  # F15：超长回复分片
import logging
from src.im_adapter.errors import IMAdapterRateLimitError  # F14：回复发送限频退避


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



class ReplyGuardMixin(EngineMixinBase):
    """运行时：reply_guard 相关方法（从 runtime.py 抽离，零行为变更）。"""
    def _mark_inbound_processed(self, message: Message) -> None:
        """标记入站用户消息为已处理（基于消息 ID 的防重复回复）。

        在「已确定无需/无法再回复」（永久跳过、死信、限频、未预期异常）等终态统一调用，
        避免该消息每轮轮询被重复拉取→重复处理/重复入死信刷屏。
        """
        try:
            msg_key = message.msg_id or (message.raw.get("alt_id") if isinstance(message.raw, dict) else "") or ""
            if msg_key:
                self.store._conversation_repo.update_last_replied_msg_id(message.chat_id, msg_key)
                self.poller._mark_msg_processed(msg_key, message.chat_id)
                logger.info("[去重] 已标记用户消息为已处理: %s", msg_key[:30])
        except Exception as de:
            logger.warning("[去重] 标记用户消息失败: %s", de)
    def _cleanup_backoff(self) -> None:
        """清理过期的发送退避键，防止 _send_backoff_until 字典无限增长。"""
        now = time.time()
        expired = [k for k, v in self._send_backoff_until.items() if now >= v]
        for k in expired:
            del self._send_backoff_until[k]
    def _reply_cooldown_active(self, message: Message) -> bool:
        """回复冷却检查：True 表示仍在冷却期（或检查异常时保守拦截），应跳过回复。

        【修复 2026-07-31】新增追问旁路：冷却期内若收到明显的追问/澄清消息
        （如"？""展开""为什么"等简短追问），放行回复；否则合理追问被冷却拦截后
        用户的消息永久丢失，形成"发消息不回"的体验黑洞。
        """
        cooldown_seconds = self.config.poller.reply_cooldown_seconds
        if cooldown_seconds <= 0:
            return False
        # 追问旁路：明显是追问/澄清/要求展开的短消息，冷却内也放行
        if message.content and self._is_followup_message(message.content):
            logger.debug("[冷却] 追问旁路放行：%s", (message.content or "")[:30])
            return False
        try:
            last_reply_str = self.store._conversation_repo.get_last_reply_time(message.chat_id)
            if last_reply_str:
                last_reply = datetime.fromisoformat(last_reply_str)
                elapsed = (datetime.now() - last_reply).total_seconds()
                if elapsed < cooldown_seconds:
                    logger.info("[冷却] 跳过回复：%s 在 %.1f 秒前刚回复过（冷却 %.1f 秒）",
                                message.chat_name or message.chat_id, elapsed, cooldown_seconds)
                    return True
        except Exception as e:
            # 冷却检查异常时保守拦截（不发送），避免 DB 抖动导致重复回复刷屏；
            # 提升为 warning 让运维可见（原 debug 静默吞错会掩盖存储层故障）。
            logger.warning("[冷却] 检查回复时间失败，保守跳过回复以防重复发送: %s", e)
            return True
        return False
    @staticmethod
    def _is_followup_message(content: str) -> bool:
        """判断是否为追问/澄清类短消息，放行冷却旁路。"""
        stripped = content.strip()
        if len(stripped) > 15:
            return False
        followup_patterns = (
            "？", "?", "展开", "详细", "继续说", "然后呢", "为什么",
            "详细点", "具体点", "再展开", "展开说说", "展开一下",
            "多说点", "接着说", "然后", "继续", "细节", "再详细",
            "能展开吗", "详细说说", "具体说说", "什么意思",
        )
        return stripped in followup_patterns
    def _handle_sensitive_blocked_reply(self, message: Message, reply_text: str) -> None:
        """敏感词命中处理：发兜底回复、记死信、标记入站消息已处理（永久跳过）。"""
        logger.info("回复被敏感词过滤器屏蔽了")
        # 【H3修复】敏感词命中时发送兜底回复，而非静默丢弃
        fallback = getattr(self.config.safety, "default_fallback", "").strip()
        if fallback:
            try:
                fb_uuid = str(uuid.uuid4())
                if message.chat_type == "group":
                    self.dws.chat_message_send(
                        group=message.chat_id, text=fallback, uuid=fb_uuid)
                else:
                    # 单聊：优先用 peer 的 openDingTalkId
                    conv = self.store._conversation_repo.get_conversation(message.chat_id)
                    peer_oid = (conv or {}).get("peer_open_dingtalk_id", "")
                    peer_uid = (conv or {}).get("peer_user_id", "")
                    if peer_oid:
                        self.dws.chat_message_send(
                            open_dingtalk_id=peer_oid, text=fallback, uuid=fb_uuid)
                    elif peer_uid:
                        self.dws.chat_message_send(
                            user=peer_uid, text=fallback, uuid=fb_uuid)
                    elif message.sender_id:
                        self.dws.chat_message_send(
                            open_dingtalk_id=message.sender_id, text=fallback, uuid=fb_uuid)
                logger.info("[敏感词] 已发送兜底回复代替被屏蔽内容")
            except Exception as e:
                logger.warning("[敏感词] 发送兜底回复失败: %s", e)
        # 记录死信
        try:
            self.store._draft_repo.add_dead_letter(
                msg_id=message.msg_id or "",
                chat_id=message.chat_id,
                chat_name=message.chat_name or "",
                sender_id=message.sender_id or "",
                sender_name=message.sender_name or "",
                content=reply_text[:500],
                msg_type="text",
                stage="sensitive_word_filter",
                error="回复命中敏感词被屏蔽",
                raw=None,
            )
        except Exception:
            logger.warning("[resilience] silent exception in _send_reply", exc_info=True)
        # 敏感词命中属「永久跳过」：标记入站消息已处理，避免每轮轮询重复拉取→
        # 重跑 LLM 命中敏感词→反复入死信/反复发兜底刷屏。
        self._mark_inbound_processed(message)
    def _prepare_outgoing_text(self, filtered: str, message: Message) -> tuple[str, str] | None:
        """出站文本规整：剥卡片标题标记、抽取标题、归一化换行与控制字符。

        返回 (reply_title, text)；正文为空时标记入站消息已处理并返回 None。
        """
        # 仅匹配卡片标题标记 [Title]: 格式，不误伤 Markdown 引用式链接 [label]: https://...
        filtered = re.sub(r'^\[[^\]]+\]:(?!\s*(?:https?://|[\(<]))\s*', '', filtered)
        filtered = filtered.strip()
        if not filtered:
            logger.warning("回复内容为空，跳过发送")
            # 空回复属「永久跳过」（同输入必得空输出，重跑纯浪费）：标记已处理防重轮询。
            self._mark_inbound_processed(message)
            return None

        # 若回复以 markdown 标题开头，将其作为卡片标题，并从正文移除
        reply_title, filtered = extract_card_title(filtered, message.chat_name or "回复")

        filtered = filtered.replace('\r\n', '\n').replace('\r', '\n')
        filtered = re.sub(r'\n+', '\n', filtered)
        filtered = ''.join(c for c in filtered if ord(c) >= 32 or c == '\n')
        return reply_title, filtered
    def _mark_read_before_reply(self, message: Message) -> None:
        """标记已读：在即将回复的时刻标记，而非轮询到消息时立即标记。

        避免对方看到"已读"但迟迟收不到回复的糟糕体验。
        """
        if message.msg_id and message.chat_id:
            try:
                self.dws.mark_read(
                    conversation_id=message.chat_id,
                    message_id=message.msg_id,
                )
            except Exception as e:
                logger.warning("[main] 标记已读失败（不影响主流程）: %s", e)
    def _reply_shard_limit(self) -> int:
        """单条回复的字符上限（F15）。可用 config.poller.reply_shard_limit 覆盖。

        钉钉/飞书/企业微信在本项目里都走 markdown 通道，实测上限约 4096；
        默认取 4000 留余量。配置为 0 / 负数 / 非法值时回落默认值。
        """
        poller_cfg = getattr(getattr(self, "config", None), "poller", None)
        try:
            v = int(getattr(poller_cfg, "reply_shard_limit", 0) or 0)
        except (TypeError, ValueError) as _exc:
            logger.debug(f"_reply_shard_limit: swallowed exception: {_exc}")
            v = 0
        return v if v > 0 else REPLY_SHARD_LIMIT_DEFAULT
    def _reply_send_min_interval(self) -> float:
        """连续回复最小间隔（秒）。可用 config.poller.reply_send_min_interval 覆盖。
        0 / 负数 / 非法值回落代码内置默认（不是「不限制」）。"""
        poller_cfg = getattr(getattr(self, "config", None), "poller", None)
        try:
            v = float(getattr(poller_cfg, "reply_send_min_interval", 0) or 0)
        except (TypeError, ValueError) as _exc:
            logger.debug(f"_reply_send_min_interval: swallowed exception: {_exc}")
            v = 0.0
        return v if v > 0 else REPLY_SEND_MIN_INTERVAL_DEFAULT
    def _reply_send_rate_limit_backoff_seconds(self) -> float:
        """命中平台限频后的退避时长（秒）。可用
        config.poller.reply_send_rate_limit_backoff_seconds 覆盖。"""
        poller_cfg = getattr(getattr(self, "config", None), "poller", None)
        try:
            v = float(getattr(poller_cfg, "reply_send_rate_limit_backoff_seconds", 0) or 0)
        except (TypeError, ValueError) as _exc:
            logger.debug(f"_reply_send_rate_limit_backoff_seconds: swallowed exception: {_exc}")
            v = 0.0
        return v if v > 0 else REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT
    def _is_rate_limit_exception(self, exc: BaseException) -> bool:
        """判断异常是否为平台限频信号。

        钉钉把 429/"rate limit exceeded" 归类为不可重试错误（非 IMAdapterRateLimitError），
        飞书/企微则可能直接抛 IMAdapterRateLimitError；两者都属「限频」，统一在此识别，
        供 _send_reply 做退避处理。
        """
        if isinstance(exc, IMAdapterRateLimitError):
            return True
        msg = str(exc).lower()
        return any(h in msg for h in _RATE_LIMIT_HINTS)
    def _throttle_reply_send(self) -> None:
        """连续回复最小间隔护栏：两条回复间隔不足时补足 sleep，避免同轮连发触发限流。

        - dry_run 不 sleep（无真实发送，且单测依赖 dry_run 免睡）。
        - 仅读「上次发送时间戳」做门控，正常单条回复零延迟。
        - 跨线程安全（_reply_send_throttle_lock）。
        """
        if getattr(self.dws, "dry_run", False):
            return
        interval = self._reply_send_min_interval()
        if interval <= 0:
            return
        with self._reply_send_throttle_lock:
            now = time.time()
            gap = now - self._last_reply_send_ts
            if gap < interval:
                time.sleep(interval - gap)
    def _mark_reply_sent(self) -> None:
        """回复真正发完后更新「上次发送时间戳」（_send_possibly_sharded 的 finally 调用）。"""
        with self._reply_send_throttle_lock:
            self._last_reply_send_ts = time.time()
    def _reply_rate_limited(self) -> bool:
        """平台级限频护栏：若处于限频退避期则返回 True（暂停本轮剩余回复）。"""
        return time.time() < self._reply_rate_limited_until
    def _handle_reply_rate_limited(self, msg_key: str, exc: BaseException) -> None:
        """F14：命中平台限频后的退避记账（供 _send_reply 在捕获限频异常时调用）。

        置平台级限频护栏（暂停本轮剩余回复）+ 按消息的退避窗口（下轮再试）。
        """
        backoff = self._reply_send_rate_limit_backoff_seconds()
        logger.warning("[退避] 命中平台限频(429/流控)，暂停本轮剩余回复 %.0fs：%s",
                       backoff, exc)
        if msg_key:
            self._send_backoff_until[msg_key] = time.time() + backoff
        self._reply_rate_limited_until = time.time() + backoff
    def _mark_shard_processed(self, result: object, chat_id: str) -> None:
        """把已发出的续片标记为「已处理」，防止下一轮轮询把自己的分片当新消息。

        仅用于「非最后一片」；最后一片的 openTaskId 由 _record_reply_success
        走原有链路统一标记。任何异常只告警，不影响后续分片继续发送。
        """
        if getattr(self.dws, "dry_run", False) or not isinstance(result, dict):
            return
        mid = (result.get("result") or {}).get("openTaskId")
        if not mid:
            return
        try:
            self.poller._mark_msg_processed(mid, chat_id)
        except Exception as e:
            logger.warning("[分片] 标记续片已处理失败: %s", e)
    def _send_possibly_sharded(self, *, chat_id: str, reply_title: str,
                               filtered: str, reply_uuid: str, **target) -> object:
        """按平台长度上限分片后顺序发送，返回**最后一片**的 DWS 返回值。

        - 未超限（绝大多数情况）：行为与分片前完全一致，单次调用、原样文本。
        - 超限：切成 N 片，每片带「（i/N）」续发标记；标题只跟首片（避免每片
          重复标题刷屏）；每片用独立 uuid（``{reply_uuid}-{i}``），否则 DWS/
          平台会按幂等键把续片当重复消息丢弃。

        ``target`` 为发送目标 kwargs（group= / open_dingtalk_id= / user=），
        原样透传给 DwsAdapter.chat_message_send。
        """
        self._throttle_reply_send()
        try:
            shards = shard_reply_text(filtered, self._reply_shard_limit())
            if len(shards) <= 1:
                return self.dws.chat_message_send(
                    title=reply_title,
                    text=filtered,
                    uuid=reply_uuid,
                    **target,
                )

            logger.info("[分片] 回复超长（%d 字 > %d），切为 %d 片顺序发送",
                        len(filtered), self._reply_shard_limit(), len(shards))
            result: object = None
            last = len(shards) - 1
            for i, part in enumerate(shards):
                result = self.dws.chat_message_send(
                    title=(reply_title if i == 0 else ""),
                    text=part,
                    uuid=reply_uuid if i == 0 else f"{reply_uuid}-{i + 1}",
                    **target,
                )
                logger.info("[分片] 第 %d/%d 片已发送（%d 字）", i + 1, len(shards), len(part))
                if i < last:
                    self._mark_shard_processed(result, chat_id)
                    if SHARD_SEND_INTERVAL_SECONDS > 0:
                        time.sleep(SHARD_SEND_INTERVAL_SECONDS)
            return result
        finally:
            self._mark_reply_sent()
