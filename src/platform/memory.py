from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging
import sqlite3

logger = logging.getLogger(__name__)


class MemoryMixin(EngineMixinBase):
    def _public_topic_key(self, text: str) -> str:
        """把一条公共记忆归入某个主题桶，作为聚合键（group_key）。"""
        from src.memory.memory_repo import public_memory_topic_key
        return public_memory_topic_key(text)

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
                    seen_in_batch: set[str] = set()
                    for memory_text in extracted:
                        # === 自动判定范围（个人 / 公共）===
                        try:
                            from src.memory.classifier import classify_memory_scope
                            scope, _scope_reason, _ = classify_memory_scope(
                                memory_text,
                                sender_id=user_msg.sender_id or "",
                                chat_type=user_msg.chat_type,
                                source="auto_extract",
                            )
                        except sqlite3.Error as cls_err:
                            logger.warning("[记忆] 范围分类失败，默认个人: %s", cls_err)
                            scope = "personal"

                        # 聚合键：个人= sender_id（按人整合）；公共= 主题桶（按主题整合）
                        group_key = (
                            (user_msg.sender_id or "")
                            if scope == "personal"
                            else self._public_topic_key(memory_text)
                        )

                        # === 去重（批次内 + 已落 pending + 已整合摘要）===
                        norm = memory_text.strip()
                        if not norm or norm in seen_in_batch:
                            continue
                        seen_in_batch.add(norm)
                        try:
                            if self.store._memory_repo.pending_fact_exists(
                                scope=scope, group_key=group_key, content=norm
                            ):
                                logger.debug("[记忆] 跳过重复事实: %s", memory_text[:40])
                                continue
                        except sqlite3.Error as dup_err:
                            logger.warning("[记忆] 去重检查异常，保守跳过: %s", dup_err)
                            continue

                        # === 写入待整合缓冲（不再逐条存库；由汇总调度器合并为摘要）===
                        try:
                            self.store._memory_repo.add_pending_fact(
                                scope=scope, group_key=group_key, content=norm,
                                sender_name=user_msg.sender_name or "",
                                chat_id=user_msg.chat_id,
                            )
                            saved_count += 1
                            logger.info("[记忆] 已入缓冲[%s/%s]: %s", scope, group_key, memory_text[:60])
                        except sqlite3.Error as e:
                            logger.warning("[记忆] 写入 pending 失败: %s", e)

                    if saved_count > 0:
                        logger.info("[记忆] 本轮对话提取了 %d 条新事实进入整合缓冲", saved_count)
                except sqlite3.Error as e:
                    logger.warning("[记忆] 异步记忆提取失败: %s", e)

            # ThreadPoolExecutor worker 不继承父线程的 platform ContextVar，
            # 复制当前上下文带入回调，否则 save_memory 走 conv_conn("") 落到幽灵库
            # （表现为飞书/企微记忆静默写入钉钉库）。与 poller_core_ocr.py:175 一致。
            import contextvars as _cv
            self._memory_executor.submit(_cv.copy_context().run, _extract_worker)

        except sqlite3.Error as e:
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
                        # 记忆清理失败不影响主回复链路；区分临时性错误（可重试）与致命错误（需告警）
                        logger.error("[记忆] 平台 %s 清理失败: %s", ctx.id, e)
                        if isinstance(e, sqlite3.OperationalError):
                            logger.warning("[记忆] 疑似 SQLite 锁定/事务冲突，下次周期重试")

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

    def _start_memory_summarize_scheduler(self) -> threading.Thread:
        """启动「记忆汇总调度器」（摘要化记忆的核心）。

        周期性把 memory_pending 中同组（scope, group_key）的待整合事实，与已有整合
        摘要经 LLM 合并去重，upsert 回同一摘要行（kind='summary'），再清空 pending。
        从而把「逐条单条事实」持续整合为「按人/主题的整合摘要」，并支持增量更新。

        失败兜底：合并失败不删 pending（事实留待下轮重试，不丢信息）；LLM 限流(429)
        经 _bg_throttle 退避，暂停本轮剩余合并；embedding 不可用时摘要行 embedding
        留空，由 backfill 在服务启动/后续周期补算。
        """
        cfg = self.config.memory.summary_merge
        enabled = cfg.get("enabled", True)
        interval_min = int(cfg.get("interval_minutes", 10))
        max_facts = int(cfg.get("max_facts_per_merge", 50))
        min_facts = int(cfg.get("min_facts_to_merge", 1))
        th = self.config.llm_throttle

        def summarize_loop():
            while self._running:
                if not enabled:
                    time.sleep(60)
                    continue
                try:
                    repo = self.store._memory_repo
                    groups = repo.get_pending_groups()
                    for scope, group_key in groups:
                        facts = repo.get_pending_facts(scope, group_key)
                        if max_facts and len(facts) > max_facts:
                            facts = facts[:max_facts]
                        if len(facts) < min_facts:
                            continue
                        # 限流/退避：429 期间不轰炸免费额度，pending 留待下轮
                        if th.enabled and not self._bg_throttle.acquire():
                            logger.info("[记忆汇总] 主模型限流退避，跳过本轮剩余合并")
                            break
                        old = repo.get_summary(scope, group_key)
                        old_text = old["content"] if old else ""
                        try:
                            merged = self.llm_agent.merge_memories_into_summary(old_text, facts)
                        except Exception as e:  # 合并异常（如 LLM 故障）→ 保留 pending 重试
                            logger.warning("[记忆汇总] 合并失败，保留 pending 下轮重试: %s", e)
                            continue
                        if not merged or not merged.strip():
                            continue
                        emb = None
                        if self.embedding_client and getattr(self.embedding_client, "enabled", False):
                            try:
                                emb = self.embedding_client.embed(merged)
                            except Exception as e:
                                logger.debug("[记忆汇总] embedding 失败（摘要将回填）: %s", e)
                        sender_id = group_key if scope == "personal" else ""
                        repo.upsert_summary(
                            scope=scope, group_key=group_key, content=merged.strip(),
                            embedding=emb, sender_id=sender_id,
                            sender_name=(old.get("sender_name") or "") if old else "",
                            source="summary",
                        )
                        repo.delete_pending_for(scope, group_key)
                        logger.info("[记忆汇总] 已整合 %s/%s：%d 条事实 → 摘要(%d字)",
                                    scope, group_key, len(facts), len(merged))

                    # 回填缺失 embedding 的摘要行（迁移后 / 合并时无 embedding 的情况）
                    if self.embedding_client and getattr(self.embedding_client, "enabled", False):
                        try:
                            n = repo.backfill_summary_embeddings(self.embedding_client)
                            if n:
                                logger.info("[记忆回填] 补算 %d 条摘要 embedding", n)
                        except Exception as e:
                            logger.warning("[记忆回填] 失败: %s", e)
                except Exception as e:
                    logger.error("[记忆汇总] 调度器执行失败: %s", e)

                # 每配置的分钟数执行一次（等待期间可被关闭信号立即唤醒）
                for _ in range(max(1, interval_min) * 60):
                    if not self._running:
                        break
                    if self._shutdown_event.wait(1):
                        break

        thread = threading.Thread(target=summarize_loop, daemon=True, name="memory-summarize")
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
                        # 决策清理失败不影响主回复链路；区分临时性错误（可重试）与致命错误（需告警）
                        logger.error("[决策清理] 平台 %s 定时清理失败: %s", ctx.id, e)
                        if isinstance(e, sqlite3.OperationalError):
                            logger.warning("[决策清理] 疑似 SQLite 锁定/事务冲突，下次周期重试")

                # 每24小时执行一次（等待期间可被关闭信号立即唤醒）
                for _ in range(24 * 60):  # 分钟
                    if not self._running:
                        break
                    if self._shutdown_event.wait(60):
                        break

        thread = threading.Thread(target=cleanup_loop, daemon=True, name="decision-cleanup")
        return thread

    def _start_global_tables_cleanup_scheduler(self) -> threading.Thread:
        """启动全局表保留期清理调度器（每24小时执行一次）。

        清理三张「无既有保留策略」的全局表（D7），防止无限增长：
        - tool_execution_logs（每次工具调用都写入，增长最快）
        - feedback（用户反馈）
        - message_drafts（待处理草稿）
        这些表仅存在于全局库 linkora.db（非分平台库），故直接对 self.store 清理。
        保留期复用 storage.messages_retention_days（默认 90 天），不引入新配置项。
        """
        retention_days = self.config.storage.messages_retention_days

        def cleanup_loop():
            while self._running:
                store = self.store
                if store is not None:
                    try:
                        n_logs = store._tool_execution_repo.cleanup_old_logs(retention_days)
                        n_feedback = store._feedback_repo.cleanup_old_feedback(retention_days)
                        n_drafts = store._draft_repo.cleanup_old_drafts(retention_days)
                        if n_logs or n_feedback or n_drafts:
                            logger.info(
                                "[全局表清理] 已清理 tool_logs=%d feedback=%d drafts=%d（保留 %d 天前）",
                                n_logs, n_feedback, n_drafts, retention_days,
                            )
                    except Exception as e:
                        logger.error("[全局表清理] 定时清理失败: %s", e)
                        if isinstance(e, sqlite3.OperationalError):
                            logger.warning("[全局表清理] 疑似 SQLite 锁定/事务冲突，下次周期重试")

                for _ in range(24 * 60):
                    if not self._running:
                        break
                    if self._shutdown_event.wait(60):
                        break

        thread = threading.Thread(target=cleanup_loop, daemon=True, name="global-tables-cleanup")
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
                        # 消息清理失败不影响主回复链路；区分临时性错误（可重试）与致命错误（需告警）
                        logger.error("[消息清理] 平台 %s 定时清理失败: %s", ctx.id, e)
                        if isinstance(e, sqlite3.OperationalError):
                            logger.warning("[消息清理] 疑似 SQLite 锁定/事务冲突，下次周期重试")

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
                        # 摘要调度失败不影响主回复链路；区分临时性错误（可重试）与致命错误（需告警）
                        logger.error("[摘要] 平台 %s 摘要调度器执行失败: %s", ctx.id, e)
                        if isinstance(e, sqlite3.OperationalError):
                            logger.warning("[摘要] 疑似 SQLite 锁定/事务冲突，下次周期重试")

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

    def _scan_orphan_conversation_dbs(self) -> None:
        """启动期孤儿会话库检测 + ``tmp_images`` 回收（D4）。

        ``data/conversations/`` 下可能存在不再绑定任何活跃平台的遗留分库（账号解绑 /
        重装 / 迁移残留）。这些库的 ``messages`` 仍引用 ``data/tmp_images`` 图片，但活跃
        清理链路（按 ``ctx.store``）扫不到 → 图片永久累积。

        逻辑（实现见 ``src.platform.orphan_cleanup``）：
        1) 列目录，剔除活跃平台库（``self.platforms`` 的 ``store.conv_db_path``）与备份类文件；
        2) 对每个孤儿库只读打开，按真实 ``tmp_images`` 根回收其引用的孤儿图片；
        3) 记 INFO 汇总（孤儿库名 + 回收图片数），**不删除孤儿库本身**（保留决策空间）。

        两层 fail-closed 护栏（破坏性操作，宁可漏收不可误删）：
        - **活跃集为空**时跳过回收（避免把全部库当孤儿）；
        - **身份不确定**时跳过回收：任一模活跃平台解析出兜底身份键
          （``<platform>:unknown`` / 裸平台名，``account_identity.identity_is_confident``
          为假）→ 推导出的活跃库路径与磁盘真实分库名不匹配 → 全部活跃库会被误判为
          孤儿并删图（2026-09-01 事故的第二种形态）。此时整轮停手，仅告警。
        """
        from src.memory import account_identity
        from src.paths import data_path
        from src.platform.orphan_cleanup import scan_and_reclaim_orphan_tmp_images

        conv_dir = data_path("conversations")
        active_paths: set[str] = set()
        uncertain: list[str] = []
        for pid, ctx in self.platforms.items():
            store = getattr(ctx, "store", None)
            # 必须用会话分库真实路径（conv_db_path），而非全局主库 db_path；
            # 否则 active_paths 永远匹配不上 data/conversations/*.db，会把全部
            # 会话分库（含活跃账号）误判为孤儿并删除其图片（D4 回归，2026-09-01 事故根因）。
            if store is not None and hasattr(store, "conv_db_path"):
                try:
                    active_paths.add(store.conv_db_path(pid))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[孤儿库扫描] 计算平台 %s 活跃会话库路径失败（跳过该平台）: %s", pid, exc)
                    continue
                # 身份可信度：与 conv_db_path 内部同一解析入口（均不带 fallback_corp_id），
                # 保证「用于推导路径的身份」与「此处判定的身份」完全一致。
                try:
                    aid = account_identity.resolve_account_id(pid)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[孤儿库扫描] 解析平台 %s 账号身份失败: %s", pid, exc)
                    uncertain.append(f"{pid}=<解析异常>")
                    continue
                if not account_identity.identity_is_confident(aid):
                    uncertain.append(f"{pid}={aid}")
        if not active_paths:
            logger.info("[孤儿库扫描] 活跃平台集为空，跳过 tmp_images 回收（安全起见）")
            return
        if uncertain:
            logger.warning(
                "[孤儿库扫描] 平台身份未确定（%s），跳过 tmp_images 回收"
                "（fail-closed：避免把活跃库误判为孤儿后删图）",
                ", ".join(uncertain),
            )
            return

        orphan_names, reclaimed = scan_and_reclaim_orphan_tmp_images(
            conv_dir, active_paths, data_path("tmp_images")
        )
        if orphan_names:
            logger.info(
                "[孤儿库扫描] 发现 %d 个孤儿会话库（不删除，保留决策空间）: %s；"
                "回收 tmp_images 图片 %d 个",
                len(orphan_names), ", ".join(orphan_names), reclaimed,
            )
        else:
            logger.info("[孤儿库扫描] 未发现孤儿会话库")

    def _start_wal_checkpoint_scheduler(self) -> threading.Thread:
        """周期 WAL checkpoint（D3 修复）：避免各分库 WAL 长期累积。

        背景：此前全仓仅 ``kb_repo.delete_kb_document`` 删除文档后被动执行一次
        ``PRAGMA wal_checkpoint(PASSIVE)``，缺少周期性合并。实测活跃分库
        ``data/conversations/dingtalk__*.db`` 的 WAL 累积到 4MB 未合并，
        会放大读成本、拖长崩溃恢复时间，也让备份体积虚高。

        PASSIVE 模式不阻塞读写；遇忙（有活跃读持有 WAL）留给下个周期重试，
        因此不追求一次成功，只保证长期不增长。
        """
        interval_min = 30

        def checkpoint_loop():
            while self._running:
                for ctx in self.platforms.values():
                    if ctx.store is None:
                        continue
                    try:
                        # store.conn 是按线程懒创建的连接；checkpoint 是库级操作，
                        # 由本线程自己的连接发起即可，不影响其他线程的读写。
                        row = ctx.store.conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                        # 返回 (busy, log, checkpointed)；busy!=0 表示有活跃读持有 WAL
                        if row and row[0]:
                            logger.debug(
                                "[WAL] 平台 %s checkpoint 遇忙（有活跃读持有 WAL），下个周期重试",
                                ctx.id,
                            )
                    except sqlite3.Error as e:
                        logger.warning("[WAL] 平台 %s checkpoint 失败（可忽略）: %s", ctx.id, e)
                    except RuntimeError as e:
                        # store 已关闭（进程退出中）：不再重试，直接结束本线程
                        logger.debug("[WAL] 平台 %s 存储已关闭，停止 checkpoint: %s", ctx.id, e)
                        return

                # 每 30 分钟执行一次（等待期间可被关闭信号立即唤醒）
                for _ in range(interval_min * 60):
                    if not self._running:
                        break
                    if self._shutdown_event.wait(1):
                        break

        thread = threading.Thread(target=checkpoint_loop, daemon=True, name="wal-checkpoint")
        return thread
