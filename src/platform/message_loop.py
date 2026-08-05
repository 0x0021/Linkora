from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging

logger = logging.getLogger(__name__)


class MessageLoopMixin(EngineMixinBase):
    def _is_incomplete_message(self, message: Message) -> bool:
        """单条消息是否为「不完整/纯数据」型（结构化数据、但本条无请求动词）。

        调度层使用**批次级**判定（见 _batch_has_structured_data /
        _batch_has_request）：只要整批已有请求动词，即便其中某条是纯数据也不再延迟。
        本方法仅用于单条特征识别与测试。
        """
        c = (message.content or "").strip()
        if not c:
            return False
        if not self._INCOMPLETE_STRUCT_RE.search(c):
            return False
        if any(v in c for v in self._INCOMPLETE_REQUEST_VERBS):
            return False
        return True

    def _batch_has_structured_data(self, messages: list) -> bool:
        """批次中是否含结构化数据特征（任一条命中即可）。"""
        return any(self._INCOMPLETE_STRUCT_RE.search((m.content or "").strip()) for m in messages)

    def _batch_has_request(self, messages: list) -> bool:
        """批次中是否已出现请求动词（任一条命中即可）。"""
        return any(
            any(v in (m.content or "") for v in self._INCOMPLETE_REQUEST_VERBS)
            for m in messages
        )

    def _compute_debounce_delay(self, key: tuple[str, str], pending: list) -> tuple[float, bool]:
        """计算防抖窗口时长（秒）与「是否因纯数据而拉长」标志 -> (delay, incomplete_pending)。

        - 批次有结构化数据但整批尚无请求动词 -> 拉长到至少 60s，等后续请求合并。
        - 含图片但尚无文字 -> 拉长到 base+20，等字幕/指令合并。
        - 否则 base+5。
        施加**硬性上限**：即便用户持续发消息使定时器被反复重置，批次也必在
        first_seen + HARD_CAP 内触发，绝不会无限等待（timeout 必触发）。
        """
        base = self.config.poller.reply_cooldown_seconds

        def _is_image_msg(m):
            c = m.content or ""
            return (m.msg_type == "image") or ("[图片识别中...]" in c) or c.startswith("【图片内容】")

        has_image = any(_is_image_msg(m) for m in pending)
        has_text = any(not _is_image_msg(m) for m in pending)

        # 批次级不完整判定：整批有结构化数据、但整批尚无请求动词 -> 等后续请求。
        incomplete_pending = self._batch_has_structured_data(pending) and not self._batch_has_request(pending)

        if incomplete_pending:
            delay = max(base + 5, 60)
        elif has_image and not has_text:
            delay = base + 20
        else:
            delay = base + 5

        # 硬性上限：避免极端情况下定时器被反复重置导致永远不回复。
        first_seen = self._pending_first_seen.get(key, time.time())
        hard_cap = max(base + 5, 60) + 60  # 至少 120s
        age = time.time() - first_seen
        delay = min(delay, max(1.0, hard_cap - age))
        return delay, incomplete_pending

    def get_debounce_metrics(self) -> dict:
        """返回「纯数据/不完整」批次防抖的监控指标（P1-C），供 /api/debounce-metrics 读取。

        - delay_count: 触发拉长窗口的次数
        - extra_sec: 累计额外等待秒数（相对正常窗口）
        - fired_with_request: 拉长窗口内收到后续请求（合并回复，策略有效）
        - fired_without_request: 拉长窗口内未收到后续请求（直接回复纯数据，偏慢）
        """
        return {
            "delay_count": self._incomplete_delay_count,
            "extra_sec": round(self._incomplete_extra_sec, 1),
            "fired_with_request": self._incomplete_fired_with_request,
            "fired_without_request": self._incomplete_fired_without_request,
            "pending_batches": len(self._pending_messages),
        }

    def get_backpressure_metrics(self) -> dict:
        """返回积压回放背压监控指标（P1-E），供 /api/backpressure-metrics 读取。

        多平台聚合：汇总所有启用平台的 poller 背压指标。
        """
        platforms = getattr(self, "platforms", {})
        if not platforms:
            return {
                "dispatched_total": 0,
                "deferred_total": 0,
                "last_cycle_dispatched": 0,
                "last_cycle_deferred": 0,
                "cold_start_pending": True,
                "max_dispatch_per_cycle": self.config.poller.max_dispatch_per_cycle,
                "max_concurrent_replies": self.config.poller.max_concurrent_replies,
                "platforms": {},
            }

        aggregated = {
            "dispatched_total": 0,
            "deferred_total": 0,
            "last_cycle_dispatched": 0,
            "last_cycle_deferred": 0,
            "cold_start_pending": False,
            "max_dispatch_per_cycle": 0,
            "max_concurrent_replies": 0,
            "platforms": {},
        }
        for platform_id, ctx in platforms.items():
            poller = getattr(ctx, "poller", None)
            if poller is None:
                continue
            metrics = poller.get_backpressure_metrics()
            aggregated["platforms"][platform_id] = metrics
            aggregated["dispatched_total"] += metrics.get("dispatched_total", 0)
            aggregated["deferred_total"] += metrics.get("deferred_total", 0)
            aggregated["last_cycle_dispatched"] += metrics.get("last_cycle_dispatched", 0)
            aggregated["last_cycle_deferred"] += metrics.get("last_cycle_deferred", 0)
            aggregated["max_dispatch_per_cycle"] = max(
                aggregated["max_dispatch_per_cycle"],
                metrics.get("max_dispatch_per_cycle", 0),
            )
            aggregated["max_concurrent_replies"] = max(
                aggregated["max_concurrent_replies"],
                metrics.get("max_concurrent_replies", 0),
            )
            if metrics.get("cold_start_pending"):
                aggregated["cold_start_pending"] = True
        return aggregated

    def get_poller_status(self) -> dict:
        """返回轮询器综合可观测指标（供 /api/poller-status 读取）。

        多平台聚合：汇总所有启用平台的 poller 状态。
        """
        platforms = getattr(self, "platforms", {})
        if not platforms:
            return {
                "last_poll_at": None,
                "last_error": None,
                "last_error_at": None,
                "queue_depth": 0,
                "poll_count": 0,
                "running": False,
                "platforms": {},
            }

        aggregated = {
            "last_poll_at": None,
            "last_error": None,
            "last_error_at": None,
            "queue_depth": 0,
            "poll_count": 0,
            "running": False,
            "platforms": {},
        }
        for platform_id, ctx in platforms.items():
            poller = getattr(ctx, "poller", None)
            if poller is None:
                continue
            status = poller.get_observability()
            aggregated["platforms"][platform_id] = status
            aggregated["queue_depth"] += status.get("queue_depth", 0)
            aggregated["poll_count"] += status.get("poll_count", 0)
            if status.get("running"):
                aggregated["running"] = True
            last_poll = status.get("last_poll_at")
            if last_poll and (
                aggregated["last_poll_at"] is None or last_poll > aggregated["last_poll_at"]
            ):
                aggregated["last_poll_at"] = last_poll
            last_error_at = status.get("last_error_at")
            if last_error_at and (
                aggregated["last_error_at"] is None or last_error_at > aggregated["last_error_at"]
            ):
                aggregated["last_error_at"] = last_error_at
                aggregated["last_error"] = status.get("last_error")
        return aggregated

    def _compute_tool_whitelist_drift(self) -> dict:
        """计算 config.yaml 的 tools.available 白名单与代码实际注册工具的差异。

        用于启动自检：当有人往 BUILTIN_TOOL_MANIFEST 加了新工具却忘了同步
        config.yaml 的 tools.available 时，yaml 会整段覆盖默认白名单、静默把
        新工具屏蔽掉（即此前 26→23 的 P0 漂移）。此处把差异暴露出来。

        技能自动包装工具（source='skill'）有意绕过白名单，从「缺失」告警中排除，
        改以 skill_auto_wrapped 字段保留可见性，避免误报噪音削弱告警可信度。
        实际计算委托给 ToolRouter.compute_whitelist_drift（可单测）。
        """
        whitelist = set(self.config.tools.available or [])
        if getattr(self, "tool_router", None):
            return self.tool_router.compute_whitelist_drift(whitelist)
        return {
            "registered_count": 0,
            "whitelist_count": len(whitelist),
            "missing_in_whitelist": [],
            "stale_in_whitelist": sorted(whitelist),
            "skill_auto_wrapped": [],
        }

    def get_tool_whitelist_drift(self) -> dict:
        """返回最近一次（启动 / 重载）计算的白名单漂移结果。"""
        return getattr(self, "_tool_whitelist_drift", {
            "registered_count": 0, "whitelist_count": 0,
            "missing_in_whitelist": [], "stale_in_whitelist": [], "skill_auto_wrapped": [],
        })

    def handle_message(self, message: Message) -> None:
        """消息防抖调度：收到消息不立即处理，等 cooldown+5 秒后合并处理。

        按 (chat_id, sender_id) 分组，同一发送者在冷却期内的多条消息合并为一条处理。

        【Phase 3 多平台】记录本条消息所属平台（来自派发回调设置的运行期上下文），
        供 _process_pending_messages 的 Timer 线程恢复上下文——Timer 在独立线程触发，
        不继承派发线程的 ContextVar，必须显式还原，否则会用错平台 store/dws/llm_agent。
        """
        key = (message.chat_id, message.sender_id)
        # 容错：裸实例（测试）可能未运行 __init__，确保运行时状态存在。
        if not hasattr(self, "_pending_platform"):
            self._pending_platform = {}
        platform_id = _active_platform_ctx.get()

        # 【并发安全】入队（去重+append）与定时器操作必须在同一把 _timer_lock 下。
        # 派发线程（poller run_loop 调用 handle_message）与 Timer 守护线程
        # （_process_pending_messages 里 pop(key)）会并发读写 _pending_messages。
        # 若 append 不加锁：poller 在「读 pending[key] 去重」后、「append」前，Timer 恰好 pop(key)，
        # 则 append 时 self._pending_messages[key] 抛 KeyError → 消息被 run_loop 兜底丢弃
        # （表现为「能收到消息但偶发不回复」）。
        with self._timer_lock:
            # 加入待处理缓冲区（按 msg_id 去重，防止 list-all 和 per-conversation 路径重复）
            if key not in self._pending_messages:
                self._pending_messages[key] = []
                # 新批次起始时间（首次消息到达），用于硬性超时上限
                self._pending_first_seen[key] = time.time()
                # 记录本批次所属平台，供 Timer 线程恢复上下文
                self._pending_platform[key] = platform_id
            existing_ids = {m.msg_id for m in self._pending_messages[key]}
            if message.msg_id in existing_ids:
                logger.debug("[防抖] 去重：忽略重复消息 %s", message.msg_id[:20])
                return

            # === 内容级去重：防止 list-all 与 per-conversation 路径对同一条消息
            # 生成不同 msg_id 导致跨路径 id 去重失效。两条路径先后入队时，
            # 第二条会取消旧定时器 → 重建新定时器 → 旧批次被丢弃；
            # 若此时第一条已触发回复且持有 _replying_lock，新定时器触发时
            # 会被锁挡掉 → 整条消息静默丢弃，用户永远收不到回复。
            # 解决：同 key 下已有相同内容的消息时直接跳过，且不取消旧定时器。
            for pending_msg in self._pending_messages[key]:
                if pending_msg.content == message.content:
                    logger.info(
                        "[防抖] 内容去重：%s 已有相同内容待处理，跳过（不取消定时器）",
                        message.msg_id[:20],
                    )
                    return

            # === 跨通道去重（双轮询器根因）：list-all 与 wecom 等轮询器可能给同一条
            # 物理消息生成不同的 (chat_id, sender_id) key（如对话级 ID 与消息级 ID 格式
            # 不一致），导致同一条消息在 _pending_messages 里建出两个独立缓冲区 + 两个
            # 定时器。两个定时器先后触发，先到的持锁处理中、后到的撞回复锁被静默丢弃
            # → 用户消息永久丢失，日志却显示「正在回复中」。
            # 解决：若同一 chat_id 下已有相同内容待处理（无论落在哪个 key），直接跳过，
            # 合并到已存在的那一条，避免重复投递与锁竞争。
            for _pend_key, _pend_list in self._pending_messages.items():
                for _pm in _pend_list:
                    if _pm.chat_id == message.chat_id and _pm.content == message.content:
                        logger.info(
                            "[防抖] 跨通道去重：chat_id=%s 已有相同内容待处理，跳过重复投递: %s",
                            message.chat_id[:20], message.msg_id[:20],
                        )
                        return

            # 【关键修复】将消息加入待处理缓冲区——定时器触发时 _process_pending_messages
            # 会从该列表 pop 消息并处理。缺失 append 会导致列表永远为空、消息被静默丢弃，
            # 表现为「能收到消息但 AI 永远不回复」（所有会话均受影响）。
            self._pending_messages[key].append(message)

            # 取消旧定时器
            if key in self._pending_timers:
                old_timer = self._pending_timers[key]
                old_timer.cancel()

            # === 防抖窗口自适应（关键修复）===
            # 用户常先发图片、紧接着补一句指令（如「识别图片内容」）。若窗口过短，
            # 图片批次会在文字到达前触发，导致图片与文字被拆成两批：图片先被 OCR 回复、
            # 文字随后被「已回复」判定压制，用户真正的需求得不到回答。
            # - 含图片且尚无文字说明：拉长窗口（cooldown+20）等待字幕/指令合并进同一批；
            # - 一旦批次里出现非图片消息（字幕/指令已到）：用较短窗口（cooldown+5）尽快合并回复。
            base = self.config.poller.reply_cooldown_seconds
            pending = self._pending_messages.get(key, [])

            # 计算防抖窗口：批次级「纯数据/不完整」判定，等后续请求合并后再回复。
            delay, incomplete_pending = self._compute_debounce_delay(key, pending)
            if incomplete_pending:
                if not self._pending_incomplete_wait.get(key):
                    with self._metrics_lock:
                        self._incomplete_delay_count += 1
                        self._incomplete_extra_sec += max(0.0, max(base + 5, 60) - (base + 5))
                self._pending_incomplete_wait[key] = True
            timer = threading.Timer(delay, self._process_pending_messages, args=(key,))
            timer.daemon = True
            timer.start()
            self._pending_timers[key] = timer
            # 在锁内快照计数，避免锁外读时 key 已被 Timer pop 导致 KeyError
            pending_count = len(self._pending_messages[key])

        logger.info("[防抖] 已缓存消息（共 %d 条等待处理，%d 秒后触发）: %s@%s",
                    pending_count, delay, message.chat_id[:20], message.sender_name)

    def _process_pending_messages(self, key: tuple[str, str]) -> None:
        """定时器触发：合并消息并调用 _handle_message_impl。"""
        # 【并发安全】定时器删除、出队 pop 及三个共享 dict 的 pop 必须在同一把
        # _timer_lock 下，与 handle_message 的入队 append 互斥。否则旧批次 Timer
        # 在锁外 pop _pending_platform 时可能偷走新批次写入的 platform_id，导致
        # 飞书消息用钉钉组件处理（跨平台上下文错乱）。
        with self._timer_lock:
            if key in self._pending_timers:
                del self._pending_timers[key]
            if key not in self._pending_messages:
                return
            messages = self._pending_messages.pop(key)
            # 取出 handle_message 记录的本批次平台，供下方 platform_scope 还原上下文
            platform_id = getattr(self, "_pending_platform", {}).pop(key, "dingtalk")
            # 读取并清理本批次的「纯数据等待」标记（P1-C 监控）
            was_incomplete = self._pending_incomplete_wait.pop(key, False)
            self._pending_first_seen.pop(key, None)
        # 【Phase 3 多平台】Timer 线程不继承父线程 ContextVar，须经统一入口
        # platform_scope 一次性还原三套平台上下文（唯一真源：
        # src.memory.platform_context），退出时自动全部复位，避免 Timer 线程池
        # 长寿命泄漏到下次。三者缺一不可：
        # - runtime 组件路由：否则自处理全程误用 dingtalk 的 store/dws/llm_agent
        #   （表现为飞书/企微消息被写到钉钉库、用错适配器发送）；
        # - 日志归属：Timer 线程名是通用的（Thread-N），线程名推断覆盖不到，须显式
        #   设置，使防抖自处理链路的所有日志在 Web 视图按平台精确归属；
        # - 仓储会话库路由：否则 update_message_*/save_message 会调 conv_conn("")
        #   落到 __<digest>.db 幽灵库，表现为「AI 回复写入后，前端消息记录页取不到
        #   『我』发的消息」。
        from src.memory.platform_context import platform_scope
        with platform_scope(platform_id):
            if not messages:
                return

            # 【关键修复】OCR 异步完成，防抖队列里的图片消息可能还是占位符。
            # 等待所有图片消息的 OCR 完成（每条约 10s 超时），确保用完整内容回复。
            refreshed = []
            for m in messages:
                if "[图片识别中...]" in m.content:
                    ocr_result = self.poller.wait_for_ocr(m.msg_id, timeout=15.0)
                    if ocr_result and ocr_result.strip():
                        logger.info(
                            "[防抖] 等待OCR完成: %s '%s' -> '%s'",
                            m.msg_id[:20], m.content[:30], ocr_result[:30]
                        )
                        # 【关键修复】OCR 结果只替换占位符，保留已有随图文字(caption)。
                        # 原 content=ocr_result 会整体覆盖，导致「图片+文字」混合消息里
                        # 用户手打的文字被静默丢弃（AI 只看到图片识别内容，看不到指令）。
                        ocr_text = ocr_result.strip()
                        preserved = m.content.replace("[图片识别中...]", "").strip()

                        # 【OCR 后处理管线】在投喂 LLM 前做可配置的多步清洗。
                        # Pipeline 步骤独立开关受 config.yaml [ocr_postprocess] 控制。
                        try:
                            from src.ocr_postprocess import run_ocr_postprocess
                            ocr_text, skip = run_ocr_postprocess(ocr_text)
                            if skip:
                                logger.info(
                                    "[防抖] OCR后处理跳过（文本过短）: %s",
                                    m.msg_id[:20]
                                )
                                # 不丢弃消息，保留随图文字 + 兜底说明
                                new_content = preserved if preserved else "[图片，文字识别结果过短]"
                                new_msg = Message(
                                    msg_id=m.msg_id,
                                    chat_id=m.chat_id,
                                    chat_type=m.chat_type,
                                    chat_name=m.chat_name,
                                    sender_id=m.sender_id,
                                    sender_name=m.sender_name,
                                    content=new_content,
                                    msg_type=m.msg_type,
                                    timestamp=m.timestamp,
                                    raw=m.raw,
                                )
                                refreshed.append(new_msg)
                                continue
                        except Exception as e:
                            logger.debug("[防抖] OCR后处理异常，使用原始OCR文本: %s", e)

                        if preserved:
                            # 保留用户随图文字（指令），图片识别内容作为独立区块接在其后
                            new_content = (
                                f"{preserved}\n\n———— 图片识别内容 ————\n{ocr_text}\n"
                                "———— 图片识别内容结束 ————"
                            )
                        else:
                            new_content = ocr_text
                        new_msg = Message(
                            msg_id=m.msg_id,
                            chat_id=m.chat_id,
                            chat_type=m.chat_type,
                            chat_name=m.chat_name,
                            sender_id=m.sender_id,
                            sender_name=m.sender_name,
                            content=new_content,
                            msg_type=m.msg_type,
                            timestamp=m.timestamp,
                            raw=m.raw,
                        )
                        refreshed.append(new_msg)
                        continue
                    # OCR 失败或超时：从数据库尝试读取最新内容
                    try:
                        db_msg = self.store._message_repo.get_message_by_id(m.msg_id)
                        if db_msg and db_msg.content and "[图片识别中...]" not in db_msg.content:
                            logger.info(
                                "[防抖] 从DB刷新OCR结果: %s '%s' -> '%s'",
                                m.msg_id[:20], m.content[:30], db_msg.content[:30]
                            )
                            new_msg = Message(
                                msg_id=m.msg_id,
                                chat_id=m.chat_id,
                                chat_type=m.chat_type,
                                chat_name=m.chat_name,
                                sender_id=m.sender_id,
                                sender_name=m.sender_name,
                                content=db_msg.content,
                                msg_type=db_msg.msg_type,
                                timestamp=m.timestamp,
                                raw=m.raw,
                            )
                            refreshed.append(new_msg)
                            continue
                    except Exception as e:
                        logger.warning("[防抖] 从DB刷新OCR失败: %s", e)
                    # OCR 与 DB 均未取得真实文本：不要把"[图片识别中...]"占位符喂给 LLM
                    # （会污染当前轮回复与历史上下文，造成答非所问）。仅保留用户随图文字(caption)，
                    # 去掉占位符；若连 caption 都没有，用中性说明替代。
                    logger.warning(
                        "[防抖] OCR结果未就绪，清理占位符: %s content=%s",
                        m.msg_id[:20], m.content[:50]
                    )
                    cleaned = m.content.replace("[图片识别中...]", "").strip()
                    if not cleaned:
                        cleaned = "[图片，文字识别未完成]"
                    refreshed.append(Message(
                        msg_id=m.msg_id,
                        chat_id=m.chat_id,
                        chat_type=m.chat_type,
                        chat_name=m.chat_name,
                        sender_id=m.sender_id,
                        sender_name=m.sender_name,
                        content=cleaned,
                        msg_type=m.msg_type,
                        timestamp=m.timestamp,
                        raw=m.raw,
                    ))
                    continue
                refreshed.append(m)
            messages = refreshed

            # 【关键修复】把 OCR 刷新得到的真实文本回写到数据库。
            # 原因：poller 的异步 OCR 回写（update_message_content）在 debounce 合并流程下
            # 偶尔与落库存在竞态被覆盖/未命中，导致图片消息在 DB 里长期停留在
            # "[图片识别中...]" 占位符。第二轮对话取 get_conversation_history 时拿到的
            # 是占位符而非真实 OCR 文本 → 上下文断裂（"值得投资吗" 接不上前一轮基金分析）。
            # 这里在内存已确认拿到 OCR 结果后主动持久化，确保后续轮次的会话历史包含图片内容。
            for m in refreshed:
                if m.msg_type == "image" and m.content and "[图片识别中...]" not in m.content:
                    try:
                        self.store._message_repo.update_message_content(m.msg_id, m.content)
                    except Exception as e:
                        logger.warning("[防抖] 回写OCR内容失败: %s", e)

            # 合并消息：取最后一条作为主消息，其他追加到 content
            # 【关键修复】把图片相对路径从子消息（按原始图片 msg_id）带到合并后的主消息，
            # 否则图片+文字合并后，路径落在被丢弃的图片 msg_id 行上，消息记录页缩略图失效。
            def _resolve_merge_image_path(msgs: list) -> str:
                for m in msgs:
                    if m.raw.get("merged"):
                        for oid in m.raw.get("original_ids", []):
                            p = self.poller.get_image_path(oid)
                            if p:
                                return p
                    p = self.poller.get_image_path(m.msg_id)
                    if p:
                        return p
                return ""

            if len(messages) == 1:
                merged_message = messages[0]
                merged_message.image_path = _resolve_merge_image_path(messages)
            else:
                # 合并策略：用最后一条消息的元数据，content 合并所有消息
                latest = messages[-1]
                merged_content = "\n\n".join([f"[消息{i+1}] {m.content}" for i, m in enumerate(messages)])

                # 正确检测混合消息类型：如果同时包含图片和其他类型，标记为 mixed
                types = {m.msg_type for m in messages}
                has_image = "image" in types
                has_other = any(t != "image" for t in types)
                merged_type = "mixed" if (has_image and has_other) else latest.msg_type

                merged_message = Message(
                    msg_id=latest.msg_id,
                    chat_id=latest.chat_id,
                    chat_type=latest.chat_type,
                    chat_name=latest.chat_name,
                    sender_id=latest.sender_id,
                    sender_name=latest.sender_name,
                    content=merged_content,
                    msg_type=merged_type,
                    timestamp=latest.timestamp,
                    raw={"merged": True, "count": len(messages), "original_ids": [m.msg_id for m in messages]},
                    image_path=_resolve_merge_image_path(messages),
                )
                logger.info("[防抖] 合并了 %d 条消息 (类型=%s): %s", len(messages), merged_type, merged_content[:100])

            # 【P1-C 监控】若本批次曾被判定「纯数据/不完整」而拉长窗口，记录最终结果：
            # 窗口内是否收到后续请求（合并后整批已含请求动词）→ 判断 60s 等待是否值得。
            if was_incomplete:
                if self._batch_has_request(messages):
                    with self._metrics_lock:
                        self._incomplete_fired_with_request += 1
                    logger.info("[防抖] 纯数据批次在窗口内收到后续请求，已合并回复（拉长窗口有效）")
                else:
                    with self._metrics_lock:
                        self._incomplete_fired_without_request += 1
                    logger.warning("[防抖] 纯数据批次窗口内未收到后续请求，直接回复纯数据（可能偏慢，关注 metrics）")

            # 调用原有处理逻辑。Timer 线程内的兜底：即便 _handle_message_impl
            # 内部异常未捕获，也在此兜住并留痕，避免消息已 pop 却静默丢失且无日志。
            try:
                self._handle_message_impl(merged_message)
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "[防抖] 合并消息处理后异常（消息可能未回复）: %s",
                    e, exc_info=True,
                )
