from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging

logger = logging.getLogger(__name__)


class MemoryMixin(EngineMixinBase):
    def _auto_save_memory(self, user_msg: Message, ai_reply: str, history: list[Message] | None = None) -> None:
        """对话结束后用 LLM 自动提炼重要信息存入长期记忆。

        策略：
        1. 触发条件：对话有实质内容（非纯寒暄）
        2. 用 LLM 从完整对话中提取值得记住的事实
        3. 去重：与已有记忆比对，避免重复保存
        4. 保存时生成 embedding 便于后续语义召回
        """
        try:
            # === embedding 未启用则直接返回（提取了也无法被召回）===
            if not self.embedding_client or not self.embedding_client.enabled:
                return

            # === 触发条件检查 ===
            user_content = (user_msg.content or "").strip()
            # 太短的消息不值得提取记忆
            if len(user_content) < 8:
                return
            # 纯寒暄/问候不触发（快速过滤，避免浪费 LLM 调用）
            greeting_patterns = [
                r'^(在吗|在不在|你好|您好|嗨|hi|hello|hey|谢谢|感谢|好的|嗯|哦|ok|thanks)[\s!！。.？?]*$',
                r'^(再见|拜拜|bye|晚安|辛苦|麻烦了)[\s!！。.？?]*$',
                r'^(早|早上好|上午好|下午好|晚上好|哈喽|嘿|收到|明白|了解|知道了|没问题|可以的|好滴|嗯呢|行)[\s!！。.？?]*$',
            ]
            import re
            if any(re.match(p, user_content, re.IGNORECASE) for p in greeting_patterns):
                return

            # 纯语气词、无意义重复（如"哈哈哈哈"、"嗯嗯嗯嗯"、"哦哦哦"）
            stripped = user_content.replace(" ", "").replace("！", "").replace("。", "").replace("？", "").replace("?", "")
            if len(set(stripped)) <= 2 and len(stripped) >= 4:
                return  # 只有1-2种字符的重复

            # === 记忆提取节流：避免每条回复都额外调一次 LLM（每轮 2 次→按需）===
            # 1) 同会话冷却：距上次提取不足冷却时长则跳过；
            # 2) 新增内容过短：本轮用户消息+AI 回复合计太短，不值得提取。
            th = self.config.llm_throttle
            now = time.time()
            # 全局限流退避：免费主模型近期限流(429/超时)时，后台记忆提取同样暂停，
            # 与摘要/意图生成共享同一把 _bg_throttle，避免 429 风暴下继续轰炸免费额度。
            # （此前此路径只做同会话冷却+最小新增字符判断，绕过了 429 退避保护。）
            if th.enabled and not self._bg_throttle.acquire():
                logger.info("[记忆] 主模型近期限流，本轮跳过记忆提取（429 退避）")
                return
            with self._last_extract_time_lock:
                last_extract = self._last_extract_time.get(user_msg.chat_id, 0.0)
                if th.enabled and (now - last_extract) < th.extract_memory_cooldown_seconds:
                    logger.debug("[记忆] 冷却中（%.0fs/%ds），跳过提取: chat=%s",
                                 now - last_extract, th.extract_memory_cooldown_seconds,
                                 (user_msg.chat_name or user_msg.chat_id)[:20])
                    return
                new_content = (user_content + (ai_reply or "")).strip()
                if th.enabled and len(new_content) < th.extract_memory_min_new_chars:
                    logger.debug("[记忆] 新增内容过短（%d 字 < %d），跳过提取",
                                 len(new_content), th.extract_memory_min_new_chars)
                    return
                # check-and-set 原子完成：在同一把锁内读取并写入，避免竞态条件
                self._last_extract_time[user_msg.chat_id] = now

            # === 异步执行记忆提取（LLM 调用耗时数秒，不阻塞消息处理主流程）===
            def _extract_worker():
                try:
                    # === 构建完整对话上下文 ===
                    conversation = list(history or [])
                    # 加入当前轮次
                    conversation.append(user_msg)
                    # 把 AI 回复也加入（必须标记 role="assistant"，否则会被当成对方说的话提取）
                    ai_msg = Message(
                        msg_id="_auto_ai",
                        chat_id=user_msg.chat_id,
                        chat_type=user_msg.chat_type,
                        chat_name=user_msg.chat_name,
                        sender_id=self.current_open_dingtalk_id or self.current_user_id or "ai",
                        sender_name=self.current_user_name or "AI",
                        content=ai_reply,
                        msg_type="text",
                        timestamp=datetime.now(),
                        raw={},
                        role="assistant",
                    )
                    conversation.append(ai_msg)

                    # === 调用 LLM 提取记忆 ===
                    extracted = self.llm_agent.extract_memories_from_conversation(conversation)
                    if not extracted:
                        logger.debug("[记忆] LLM 未提取到值得记住的信息")
                        return

                    saved_count = 0
                    for memory_text in extracted:
                        # === 自动判定范围（个人 / 公共）===
                        try:
                            from src.memory.classifier import classify_memory_scope
                            scope, scope_reason, _ = classify_memory_scope(
                                memory_text,
                                sender_id=user_msg.sender_id or "",
                                chat_type=user_msg.chat_type,
                                source="auto_extract",
                            )
                        except Exception as cls_err:
                            logger.warning("[记忆] 范围分类失败，默认个人: %s", cls_err)
                            scope = "personal"

                        # === 去重检查（异常时保守跳过，避免重复入库）===
                        try:
                            is_dup = self.store._memory_repo.check_memory_duplicate(
                                memory_text, embedding_client=self.embedding_client,
                                sender_id=user_msg.sender_id or "", scope=scope)
                        except Exception as dup_err:
                            logger.warning("[记忆] 去重检查异常，保守跳过: %s", dup_err)
                            continue
                        if is_dup:
                            logger.debug("[记忆] 跳过重复: %s", memory_text[:40])
                            continue

                        # === 生成 embedding（失败则跳过，避免无法被 recall_memory 召回）===
                        embedding = None
                        try:
                            embedding = self.embedding_client.embed(memory_text)
                        except Exception as emb_err:
                            logger.debug("[记忆] 无法生成嵌入: %s", emb_err)
                        if not embedding:
                            logger.debug("[记忆] embedding 为空，跳过保存（无法被召回）: %s", memory_text[:40])
                            continue

                        # === 保存 ===
                        import hashlib
                        key = "auto_" + hashlib.md5(memory_text.encode("utf-8")).hexdigest()[:12]
                        self.store._memory_repo.save_memory(
                            key=key,
                            content=memory_text,
                            source="auto_extract",
                            chat_id=user_msg.chat_id,
                            embedding=embedding,
                            sender_id=user_msg.sender_id or "",
                            sender_name=user_msg.sender_name or "",
                            scope=scope,
                        )
                        saved_count += 1
                        logger.info("[记忆] 已保存[%s]: %s", scope, memory_text[:60])

                    if saved_count > 0:
                        logger.info("[记忆] 本轮对话保存了 %d 条新记忆", saved_count)
                except Exception as e:
                    logger.warning("[记忆] 异步记忆提取失败: %s", e)

            # ThreadPoolExecutor worker 不继承父线程的 platform ContextVar，
            # 复制当前上下文带入回调，否则 save_memory 走 conv_conn("") 落到幽灵库
            # （表现为飞书/企微记忆静默写入钉钉库）。与 poller_core_ocr.py:175 一致。
            import contextvars as _cv
            self._memory_executor.submit(_cv.copy_context().run, _extract_worker)

        except Exception as e:
            logger.warning("[记忆] 自动保存调度失败: %s", e)

    def _start_memory_cleanup_scheduler(self) -> threading.Thread:
        """启动记忆清理调度器（使用配置的时间间隔）。"""
        check_interval_days = self.config.memory.cleanup.get("check_interval_days", 7)
        max_age_days = self.config.memory.cleanup.get("max_age_days", 90)
        min_similarity = self.config.memory.cleanup.get("min_similarity_threshold", 0.3)

        def cleanup_loop():
            while self._running:
                # 【Phase 3 多平台】遍历所有平台，逐库清理旧记忆。
                # daemon 线程不会自动继承 main thread 的 ContextVar，必须显式
                # with_platform(pid)，否则 _memory_repo 内调 conv_conn 拿空 platform。
                from src.memory.platform_context import with_platform
                for ctx in self.platforms.values():
                    if ctx.store is None:
                        continue
                    try:
                        with with_platform(ctx.id):
                            deleted = ctx.store._memory_repo.cleanup_old_memories(
                                max_age_days=max_age_days,
                                min_similarity_threshold=min_similarity,
                            )
                        if deleted > 0:
                            logger.info("[记忆] 平台 %s 已移除 %d 条旧记忆", ctx.id, deleted)
                    except Exception as e:
                        logger.error("[记忆] 平台 %s 清理失败: %s", ctx.id, e)

                # 每配置的天数执行一次（等待期间可被关闭信号立即唤醒）
                # 周期取整；<=0 视为禁用清理调度（避免 range(0) 忙循环 / 浮点 TypeError 致线程静默退出）
                cleanup_interval_min = int(round(check_interval_days * 24 * 60))
                if cleanup_interval_min <= 0:
                    logger.warning("[记忆] 清理间隔<=0，清理调度已禁用（check_interval_days=%r）", check_interval_days)
                    break
                for _ in range(cleanup_interval_min):
                    if not self._running:
                        break
                    if self._shutdown_event.wait(60):  # 关闭信号到达立即唤醒
                        break

        thread = threading.Thread(target=cleanup_loop, daemon=True, name="memory-cleanup")
        return thread

    def _start_decision_cleanup_scheduler(self) -> threading.Thread:
        """启动决策记录清理调度器（每24小时执行一次）。"""
        retention_days = self.config.storage.decisions_retention_days

        def cleanup_loop():
            while self._running:
                # 【Phase 3 多平台】遍历所有平台，逐库清理过期决策记录。
                from src.memory.platform_context import with_platform
                for ctx in self.platforms.values():
                    if ctx.store is None:
                        continue
                    try:
                        with with_platform(ctx.id):
                            result = ctx.store._decisions_repo.cleanup_old_records(
                                decisions_retention_days=retention_days
                            )
                        deleted = result.get("decisions_deleted", 0)
                        remaining = result.get("decisions_remaining", 0)
                        if deleted > 0:
                            logger.info(
                                "[决策清理] 平台 %s 已清理 %d 条过期决策记录，剩余 %d 条",
                                ctx.id, deleted, remaining,
                            )
                    except Exception as e:
                        logger.error("[决策清理] 平台 %s 定时清理失败: %s", ctx.id, e)

                # 每24小时执行一次（等待期间可被关闭信号立即唤醒）
                for _ in range(24 * 60):  # 分钟
                    if not self._running:
                        break
                    if self._shutdown_event.wait(60):
                        break

        thread = threading.Thread(target=cleanup_loop, daemon=True, name="decision-cleanup")
        return thread

    def _start_messages_cleanup_scheduler(self) -> threading.Thread:
        """启动消息记录清理调度器（每24小时执行一次）。

        清理超过保留期的旧消息记录，防止 messages 表无限增长。
        默认保留 90 天，用户接管检测仅查近 30 天的消息，90 天完全足够。
        """
        retention_days = self.config.storage.messages_retention_days

        def cleanup_loop():
            while self._running:
                # 【Phase 3 多平台】遍历所有平台，逐库清理旧消息记录。
                from src.memory.platform_context import with_platform
                for ctx in self.platforms.values():
                    if ctx.store is None:
                        continue
                    try:
                        with with_platform(ctx.id):
                            result = ctx.store._message_repo.cleanup_old_messages(retention_days=retention_days)
                        deleted = result.get("deleted_count", 0)
                        if deleted > 0:
                            logger.info(
                                "[消息清理] 平台 %s 已清理 %d 条旧消息记录（保留 %d 天前）",
                                ctx.id, deleted, retention_days,
                            )
                    except Exception as e:
                        logger.error("[消息清理] 平台 %s 定时清理失败: %s", ctx.id, e)

                for _ in range(24 * 60):
                    if not self._running:
                        break
                    if self._shutdown_event.wait(60):
                        break

        thread = threading.Thread(target=cleanup_loop, daemon=True, name="messages-cleanup")
        return thread

    def _start_conversation_summary_scheduler(self) -> threading.Thread:
        """启动对话摘要调度器（定期压缩历史记录）。

        已加入后台 LLM 限速：
        - 按 last_summary_at 跳过 24h 内已摘要的会话（防永续重摘要/启动轰炸）；
        - 每次摘要前经 _bg_throttle 校验（最小间隔 + 空闲降频 + 429 退避）；
        - 只取未归档的近期窗口（get_recent_unarchived_messages）减少 token；
        - 单轮上限 max_summaries_per_cycle，避免一次性排空。
        """
        cs = self.config.memory.conversation_summary
        enabled = cs.get("enabled", True)
        max_messages = cs.get("max_messages_per_conversation", 50)
        interval_hours = cs.get("summary_interval_hours", 24)
        summary_ratio = cs.get("summary_ratio", 0.4)
        th = self.config.llm_throttle

        # P1-7: 连续失败计数器，防止 LLM 故障时持续占用资源
        consecutive_failures = 0
        max_consecutive_failures = 3

        def summary_loop():
            nonlocal consecutive_failures
            while self._running:
                if not enabled:
                    time.sleep(60)
                    continue

                # 【Phase 3 多平台】遍历所有平台，逐库压缩历史会话。
                from src.memory.platform_context import with_platform
                for ctx in self.platforms.values():
                    if ctx.store is None or ctx.llm_agent is None:
                        continue
                    try:
                        with with_platform(ctx.id):
                            conversations = ctx.store._message_repo.get_conversations_needing_summary(
                                max_messages, summary_interval_hours=interval_hours
                            )
                            if not conversations:
                                logger.debug("[摘要] 平台 %s 暂无需要摘要的会话", ctx.id)
                                continue
                            logger.info(
                                "[摘要] 平台 %s 发现 %d 个会话需要摘要（本轮上限 %d）",
                                ctx.id, len(conversations), th.max_summaries_per_cycle,
                            )
                            processed = 0
                            # P1-7: 重置连续失败计数（新会话批次开始）
                            consecutive_failures = 0
                            for conv in conversations:
                                chat_id = conv["chat_id"]
                                chat_name = conv.get("chat_name", "")

                                # 只取未归档的近期窗口，避免把已压缩历史反复重喂（省 token）
                                history = ctx.store._message_repo.get_recent_unarchived_messages(
                                    chat_id, limit=th.summary_history_limit
                                )
                                if len(history) < 5:
                                    # 无需摘要：直接标记并跳过（无 LLM 调用，不消耗限速配额，瞬间完成）
                                    ctx.store._message_repo.mark_conversation_summarized(chat_id)
                                    continue

                                # 仅对真正需要 LLM 摘要的会话做限速/退避/最小间隔检查；
                                # 退避期直接停止本轮剩余摘要，避免无偿刷免费接口
                                if not self._bg_throttle.acquire():
                                    break

                                summary_text = ctx.llm_agent.summarize_conversation(
                                    history, max_messages=th.summary_history_limit
                                )
                                if summary_text:
                                    deleted_count = ctx.store._message_repo.summarize_and_compress(
                                        chat_id, summary_text, keep_ratio=summary_ratio
                                    )
                                    logger.debug("[摘要] 平台 %s 会话 %s (%s) 已压缩，删除 %d 条消息",
                                                 ctx.id, chat_id[:20], chat_name, deleted_count)
                                    ctx.store._message_repo.mark_conversation_summarized(chat_id)
                                else:
                                    # LLM 未产出摘要（瞬时失败/空响应）：本轮不标记，下个周期重试；
                                    # 若此处也标记，该会话会永久跳过、再无机会压缩（LOW#4 修复）。
                                    consecutive_failures += 1
                                    logger.debug("[摘要] 平台 %s 会话 %s 无法生成摘要，连续失败 %d/%d，本轮跳过",
                                                 ctx.id, chat_id[:20], consecutive_failures, max_consecutive_failures)
                                    if consecutive_failures >= max_consecutive_failures:
                                        logger.error("[摘要] 平台 %s 连续 %d 次摘要失败，暂停本轮",
                                                     ctx.id, max_consecutive_failures)
                                        break
                                    continue
                                # LLM 调用成功，重置失败计数
                                consecutive_failures = 0
                                processed += 1

                                if processed >= th.max_summaries_per_cycle:
                                    logger.info("[摘要] 平台 %s 已达本轮上限 %d，剩余留待下轮",
                                                ctx.id, th.max_summaries_per_cycle)
                                    break
                    except Exception as e:
                        logger.error("[摘要] 平台 %s 摘要调度器执行失败: %s", ctx.id, e)

                # 每配置的小时数执行一次（等待期间可被关闭信号立即唤醒）
                # 周期取整；<=0 视为禁用摘要调度（避免 range(0) 忙循环 / 浮点 TypeError 致线程静默退出）
                summary_interval_min = int(round(interval_hours * 60))
                if summary_interval_min <= 0:
                    logger.warning("[摘要] 摘要间隔<=0，摘要调度已禁用（interval_hours=%r）", interval_hours)
                    break
                for _ in range(summary_interval_min):
                    if not self._running:
                        break
                    if self._shutdown_event.wait(60):
                        break

        thread = threading.Thread(target=summary_loop, daemon=True, name="conversation-summary")
        return thread
