from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging


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



class LLMReplyMixin(EngineMixinBase):
    """运行时：llm_reply 相关方法（从 runtime.py 抽离，零行为变更）。"""
    def _track_llm_reply(self, message: Message, result, reply_text) -> None:
        """记录 LLM 回复的质量/成本指标（Roadmap ③ 看板用）。"""
        # 质量标记：低置信转人工 / RAG 命中
        # cited（引文页脚命中）在发送分支判定后置位（见 _append_citation_footer 结果）
        _handoff = bool(self._should_handoff_low_confidence(message, reply_text))
        _conf = getattr(reply_text, "confidence", None)
        _rag_grounded = 1 if _conf is not None else 0
        tracker.record(
            sender_id=message.sender_id or "",
            sender=message.sender_name or "",
            conversation_id=message.chat_id or "",
            chat=message.chat_name or message.chat_id,
            content=(message.content or "")[:80],
            intent=result.intent or "business",
            action="llm",
            routing_mode=getattr(reply_text, "routing_mode", None),
            routed_tools=getattr(reply_text, "routed_tools", None),
            skill_name=getattr(reply_text, "skill_name", None),
            skill_source=getattr(reply_text, "skill_source", None),
            reply_preview=(reply_text.text or "")[:80],
            platform_id=_active_platform_ctx.get(),
            handoff=_handoff,
            rag_grounded=_rag_grounded,
        )
    def _mark_agent_self_replied(self, message: Message) -> None:
        """agent 已通过 send_message 工具自回复：补做用户消息去重标记。

        【修复#1】工具自回复同样必须标记去重，否则下一轮轮询仍会把该消息
        当作新消息返回→再次自回复→循环刷屏，直到消息老化出窗。
        与 _send_reply 成功路径对称地标记用户消息已处理。
        """
        try:
            msg_key = message.msg_id or (message.raw.get("alt_id") if isinstance(message.raw, dict) else "") or ""
            if msg_key:
                self.store._conversation_repo.update_last_replied_msg_id(message.chat_id, msg_key)
                self.poller._mark_msg_processed(msg_key, message.chat_id)
                logger.info("[去重] 工具自回复后已标记用户消息为已处理: %s", msg_key[:30])
        except Exception as de:
            logger.warning("[去重] 工具自回复标记失败: %s", de)
    def _deliver_llm_reply(self, message: Message, reply_text, history) -> None:
        """LLM 文本回复的出站分支：内部确认拦截 → 低置信转人工 → 引文页脚 → 发送+记忆。"""
        if self._is_internal_confirmation(reply_text.text):
            logger.info("[内部确认] LLM 回复为纯内部确认，跳过发送: %s", reply_text.text[:40])
            return
        # Feature A：低置信度转人工（弱 RAG 命中不自动硬答）
        if self._should_handoff_low_confidence(message, reply_text):
            logger.info("[转人工] 低置信度回复转为草稿推送给主人，不自动发送")
            self._notify_owner_draft(message, reply_text)
            return
        # Phase 2：按配置追加引文溯源+置信度页脚（默认关，异常回退无页脚）
        outgoing = self._append_citation_footer(reply_text.text, reply_text, message)
        # 质量标记（Roadmap ③）：是否实际追加了引文页脚
        _cited = 1 if outgoing != reply_text.text else 0
        self._mark_decision_cited(message, _cited)
        if not self._send_reply(message, outgoing):
            logger.warning("[发送失败] LLM回复发送失败，但已在tracker记录: %s", reply_text.text[:40])
        else:
            # 对话结束后自动提炼重要信息存入记忆（存原文，不含页脚）
            self._auto_save_memory(message, reply_text.text, history)
    def _process_llm_reply(self, message: Message, result) -> None:
        """LLM 兜底处理链：取历史 → LLM 生成 → 记录指标 → 分支发送；异常分级处理。"""
        try:
            history = self.store._message_repo.get_conversation_history(
                message.chat_id,
                limit=self.config.poller.history_window,
                days=self.config.poller.history_days,
                session_gap_minutes=self.config.poller.history_session_gap_minutes,
            )
            reply_text = self.llm_agent.process_message(
                message, history,
                disposition=result.intent or "",
                intent_action="llm",
            )
            self._track_llm_reply(message, result, reply_text)
            if getattr(reply_text, "already_sent", False):
                # LLM 已通过 send_message 工具直接回复当前会话，跳过二次发送
                logger.info("[防双重回复] agent 已自回复当前会话，poller 不再发送")
                self._mark_agent_self_replied(message)
            elif reply_text.text:
                logger.info("LLM 回复: %s", reply_text.text[:100])
                self._deliver_llm_reply(message, reply_text, history)
            else:
                logger.warning("LLM返回了空回复")

        except LLMRateLimitExhaustedError as e:
            # ⑤ 全模型限频(429)耗尽：临时性故障，**不**向用户回复（避免刷屏/误导），
            # 改为打印日志并计入死信队列（DLQ），由管理员在管理台手动重放。
            self._handle_rate_limit_exhausted(message, e)
        except (LLMProcessingError, ConnectionError, TimeoutError) as e:
            # 窄化捕获：仅「LLM 处理失败」与「网络/超时」这类可预期/可恢复故障走兜底，
            # 不再用裸 except Exception 把 AttributeError/TypeError 等真代码错误
            # 伪装成「正常兜底回复」静默吞掉（MED#2）。
            if isinstance(e, LLMProcessingError) and self.config.dead_letter.enabled:
                self._enqueue_dead_letter(message, stage=e.stage, error=str(e.original or e))
                logger.warning("[死信队列] LLM处理失败已入死信，不再发送fallback回复: stage=%s", e.stage)
                # 死信入队后必须标记消息已处理，否则每轮轮询重复拉取→重复入死信刷屏。
                self._mark_inbound_processed(message)
            else:
                # 非 DLQ 的 LLMProcessingError，或网络/超时类瞬时故障：发送通用兜底。
                if not self._send_reply(message, self.config.safety.default_fallback):
                    logger.warning("[发送失败] 默认回退回复发送失败: msg_id=%s。"
                                   "因 poller 安全网会在 handler 正常返回后自动标记，"
                                   "改为显式标记（避免「以为会重试」但实际被安全网拦截）。",
                                   message.msg_id[:20])
                    # 显式标记：原因有二：
                    # ① poller 的 finally 块对 handler_ok=True 一律调用 _mark_msg_processed，
                    #    即使此处 return 也会被安全网拦截，消息不会真正进入下一轮重试；
                    # ② 如果 DWS 本身不可达（持续故障），每 5s 重试只是刷日志，徒增噪音。
                    self._mark_inbound_processed(message)
                    return
                # 兜底已发送，标记已处理避免每轮重复发兜底刷屏。
                self._mark_inbound_processed(message)
        except Exception as e:
            # 其余为疑似代码 bug（AttributeError/TypeError/KeyError 等）：绝不当作
            # 「正常兜底」静默回复误导用户，改为显形记录并落死信（若启用）。
            logger.error("LLM代理未预期异常（疑似代码 bug，已显形，未发送兜底）: %s", e, exc_info=True)
            if self.config.dead_letter.enabled:
                try:
                    self._enqueue_dead_letter(message, stage="unexpected_error", error=str(e))
                except Exception as dl_err:
                    logger.warning("[死信队列] 未预期异常入队失败: %s", dl_err)
            # 标记已处理，避免每轮轮询重复崩溃刷屏（bug 未修前不再空耗 LLM）。
            self._mark_inbound_processed(message)
    def _handle_rate_limit_exhausted(self, message: Message, exc: "LLMRateLimitExhaustedError") -> None:
        """全模型限频(429)耗尽：临时性故障，**不**向用户回复。

        改为打印日志并计入死信队列（DLQ），由管理员在管理台手动重放；
        并标记用户消息已处理，避免每轮轮询重复拉取→重复入死信/重复日志刷屏。
        """
        logger.warning("LLM 全模型限频耗尽，已记录死信（不向用户回复）: %s", exc)
        if self.config.dead_letter.enabled:
            try:
                self._enqueue_dead_letter(message, stage=exc.stage, error=str(exc.original or exc))
            except Exception as dl_err:
                logger.warning("[死信队列] 限频消息入队失败（仅记录日志）: %s", dl_err)
        else:
            logger.warning(
                "[死信队列] 未启用，限频消息仅记录日志、不回复、不可重放: msg_id=%s",
                message.msg_id[:20],
            )
        # 标记已处理，避免每轮轮询重复拉取→重复入死信/重复日志刷屏
        self._mark_inbound_processed(message)
    def _enqueue_dead_letter(self, message: Message, *, stage: str, error: str) -> None:
        """将彻底失败的消息写入死信队列（P0-2）。

        即使落库失败也不影响主流程：仅记录警告，仍回 fallback 文本给用户。
        """
        try:
            self.store._draft_repo.add_dead_letter(
                msg_id=message.msg_id,
                chat_id=message.chat_id,
                chat_name=message.chat_name or "",
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                content=message.content or "",
                msg_type=message.msg_type,
                stage=stage,
                error=error[:500],
                raw=message.raw,
            )
            logger.warning(
                "[死信队列] 消息 %s 已入队（stage=%s），管理台可重放",
                message.msg_id, stage,
            )
        except Exception as dl_err:
            logger.error("[死信队列] 落库失败（消息仍将回复 fallback）: %s", dl_err)
    def replay_dead_letter(self, dl_id: int, platform: str | None = None) -> dict:
        """重放一条死信消息（P0-2）。

        从 DLQ 取出原文，重新走 _handle_message_impl 处理。

        【Phase 3 多平台】platform 指定死信所属平台（由 Web 端 ?platform= 透传），
        进入时设置运行期上下文，确保重放走对应平台 store/dws/llm_agent。
        """
        # 还原平台上下文：优先用调用方显式传入的平台，否则回退当前运行期上下文。
        # 重放在 Web 请求线程执行，须显式设置日志平台上下文（与防抖 flush 同理），
        # 故走统一入口 platform_scope 一次性对齐三套上下文。
        from src.memory.platform_context import platform_scope
        pid = platform or _active_platform_ctx.get()
        with platform_scope(pid):
            dl = self.store._draft_repo.get_dead_letter(dl_id)
            if not dl:
                return {"success": False, "error": "not_found"}
            if dl["status"] != "pending":
                return {"success": False, "error": f"status={dl['status']}"}
            try:
                raw = dl.get("raw")
                chat_type = raw.get("chat_type", "single") if isinstance(raw, dict) else "single"
                # 使用唯一的 replay msg_id，避免与入 DLQ 时 _mark_inbound_processed
                # 设置的 last_replied_msg_id 冲突，导致 _has_replied_after 误判拦截。
                orig_msg_id = dl["msg_id"] or ""
                replay_msg_id = f"replay_{dl_id}" if not orig_msg_id else f"replay_{dl_id}_{orig_msg_id}"
                msg = Message(
                    msg_id=replay_msg_id,
                    chat_id=dl["chat_id"],
                    chat_type=chat_type,
                    chat_name=dl["chat_name"],
                    sender_id=dl["sender_id"],
                    sender_name=dl["sender_name"],
                    content=dl["content"],
                    msg_type=dl["msg_type"] or "text",
                    timestamp=datetime.now(),
                    raw=raw if isinstance(raw, dict) else {},
                )
                self._handle_message_impl(msg)
                self.store._draft_repo.resolve_dead_letter(dl_id, status="replayed", note="手动重放成功")
                return {"success": True, "id": dl_id}
            except Exception as e:
                # 【P0-3 伴生修复】重放失败时将状态标为 failed 并记录原因，避免反复点重放一直拖入 handler。
                err_msg = str(e)[:200]
                logger.error("重放死信 %d 失败: %s", dl_id, e)
                try:
                    self.store._draft_repo.resolve_dead_letter(
                        dl_id, status="failed", note=f"重放失败: {err_msg}"
                    )
                except Exception as store_err:
                    logger.warning("更新 DLQ %d 状态为 failed 也失败: %s", dl_id, store_err)
                return {"success": False, "id": dl_id, "error": err_msg, "status": "failed"}
