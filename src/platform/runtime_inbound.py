from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging
import threading
import time
import uuid
from datetime import timedelta

from src.poller_utils import match_notification_signature

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

# === 回复锁健壮性参数 ===
# 单条回复处理若卡死（如上游 LLM/工具调用挂死）超过该秒数，下次锁竞争时强制释放，
# 避免会话被「假正在回复中」永久阻塞、用户消息永远收不到回复。
_REPLY_LOCK_MAX_SECONDS = 180
# 锁竞争时有限次重试上限：同一会话确有回复在途时，不静默丢弃，而是延迟重试，
# 等上一条回复完成、锁释放后再处理，避免双轮询器重复投递导致的消息丢失。
_REPLY_LOCK_MAX_RETRIES = 4
# 锁竞争重试间隔（秒）。
_REPLY_LOCK_RETRY_DELAY = 8.0



class InboundMixin(EngineMixinBase):
    """运行时：inbound 相关方法（从 runtime.py 抽离，零行为变更）。"""
    def _has_replied_after(self, message: Message) -> bool:
        """判断是否需要跳过 AI 回复（防止对同一消息重复回复）。

        2026-07-13 重写为基于「消息 ID」的判定，取代旧的「最后回复时间 vs 消息时间戳」比较。

        旧逻辑缺陷：依赖 last_reply_time 与 message.timestamp 的大小比较。当用户消息的
        钉钉时间戳早于 AI 回复时间时（消息在 AI 处理期间才到达，时间戳取自客户端发送时刻），
        会被误判为「已回复」，导致用户的真实新需求被静默吞掉（刘芬「开CRM号」请求即此案例）。

        新逻辑（每条独立消息最多回复一次）：
          - 若本条消息的 msg_id 已被记录为「最后回复过的消息」→ 重复投递，跳过；
          - 否则 → 这是一条尚未回复的独立消息，正常处理（无论时间戳先后）。
        用户的追问（不同 msg_id）天然会被当作新消息继续回复；轮询重复投递的同一条
        消息（相同 msg_id）则被跳过。跨投递去重由 dedup_messages 表兜底。
        """
        # 使用 msg_id 或 alt_id 作为去重比对键（兼容 msg_id 为空的场景）
        msg_key = message.msg_id or (message.raw.get("alt_id") if isinstance(message.raw, dict) else "")
        if not msg_key:
            return False
        last_replied = self.store._conversation_repo.get_last_replied_msg_id(message.chat_id)
        if last_replied and last_replied == msg_key:
            logger.debug("[已回复] 同一条消息(key=%s)已回复过，跳过重复投递", msg_key[:20])
            return True
        return False
    def _has_user_taken_over(self, message: Message) -> bool:
        """检查用户是否已手动接管会话（在入站消息之后手动回复了对方）。

        场景：对方发来消息 → 用户很快自己手动回复了 → 此时若 AI 再产出
        回复并发送，会造成干扰。本方法在 AI 发回复前做最终检查。

        判定依据：会话的消息表中，是否存在 timestamp > 入站消息 timestamp
        的、sender_id 匹配当前用户的、非机器人代发的消息。
        """
        ts = message.timestamp.isoformat() if message.timestamp else ""
        if not ts:
            return False
        sender_ids = []
        if getattr(self, "current_open_dingtalk_id", ""):
            sender_ids.append(self.current_open_dingtalk_id)
        if getattr(self, "current_user_id", ""):
            sender_ids.append(self.current_user_id)
        if not sender_ids:
            if not getattr(self, "_no_owner_identity_warned", False):
                self._no_owner_identity_warned = True
                logger.warning(
                    "[门控] 当前账号未解析到人工身份(current_open_dingtalk_id/current_user_id 为空)，"
                    "接管检测失效，可能漏拦 AI 穿插——请检查账号身份解析")
            return False
        return self.store._conversation_repo.has_user_message_from(message.chat_id, ts, sender_ids)
    def _is_owner_present(self, message: Message) -> bool:
        """真人在场检测（human-in-the-loop）：本会话最近一段时间内是否有真人手动消息。

        与 _has_user_taken_over（被动：仅检查「对方这条消息之后真人是否回过」）不同，
        本方法基于时间窗主动判断「真人当前是否正参与该会话」。

        场景：真人和对方正在来回互动时，机器人轮询到对方消息会抢先生成回复并发送，
        真人往往慢于机器人，导致 _has_user_taken_over 竞速必输、AI 穿插插嘴。
        本方法只要窗口内出现过真人消息即抑制 AI 回复，真人离场超过窗口后 AI 才接管。

        判定：conversation_repo.has_user_message_from 已用 is_bot=0 过滤，
        故 AI 分身自己代发的回复不会刷新「在场」计时（避免机器人回一句后永久静默）。
        """
        cooldown = getattr(self.config.poller, "owner_present_cooldown_seconds", 0)
        # 防御：配置缺失/被 mock/非数值时视为未启用，放行回复（不静默）
        if not isinstance(cooldown, (int, float)) or cooldown <= 0:
            return False
        sender_ids = []
        if getattr(self, "current_open_dingtalk_id", ""):
            sender_ids.append(self.current_open_dingtalk_id)
        if getattr(self, "current_user_id", ""):
            sender_ids.append(self.current_user_id)
        if not sender_ids:
            if not getattr(self, "_no_owner_identity_warned", False):
                self._no_owner_identity_warned = True
                logger.warning(
                    "[门控] 当前账号未解析到人工身份(current_open_dingtalk_id/current_user_id 为空)，"
                    "在场检测失效，可能漏拦 AI 穿插——请检查账号身份解析")
            return False
        since = (datetime.now() - timedelta(seconds=cooldown)).isoformat()
        try:
            return self.store._conversation_repo.has_user_message_from(
                message.chat_id, since, sender_ids
            )
        except Exception as e:  # P1-3: 区分错误类型，避免 DB 抖动误杀正常回复
            # 临时错误（连接超时、busy）→ 保守放行，不抑制
            # 持久错误（schema 损坏、权限）→ 记录告警，保守放行
            err_str = str(e).lower()
            if any(k in err_str for k in ("database is locked", "busy", "timeout")):
                logger.debug("[真人在场] DB 临时繁忙，保守放行: %s", type(e).__name__)
                return False
            elif any(k in err_str for k in ("no such table", "schema", "permission")):
                logger.error("[真人在场] DB schema 异常，需人工介入: %s: %s", type(e).__name__, e)
                return False
            else:
                logger.warning("[真人在场] 查询失败，保守放行: %s: %s", type(e).__name__, e)
                return False

    def _reply_gate_reason(self, message: Message,
                           taken_over: "bool | None" = None,
                           owner_present: "bool | None" = None) -> "str | None":
        """返回当前应抑制 AI 自动回复的闸门原因；无闸门命中返回 None。

        供「前置过滤」与「发送前复核」两道校验共用，保证逻辑完全一致：
          - 前置过滤（_handle_message_with_rid，进入 LLM 前）：命中即跳过 LLM，省 Token；
          - 发送前复核（_should_reply_now，_send_reply 投递前）：并发兜底，
            处理期间状态变化（人工回复/在场）仍能被拦截。
        优先级短路：来自自身 → 人工已接管 → 真人在场 → DWS 已读。

        Args:
            taken_over / owner_present: 可选，由前置过滤一次性计算后传入，避免对
                has_user_message_from 重复查库（H2-2026-08-08）。发送前复核不传参
                → 实时重算，保留生成期竞态保护（绝不跨 pre-LLM / send-time 复用旧值）。
        """
        # 1) 自己发的消息
        if self._is_message_from_self(message):
            return "消息来自自身"
        # 2) 人工已在对方消息之后手动回复（接管）——关闭生成期竞态的关键
        if (self._has_user_taken_over(message) if taken_over is None else taken_over):
            return "人工已接管（消息后已手动回复）"
        # 3) 真人在场（human-in-the-loop）——避免穿插真人对话
        if (self._is_owner_present(message) if owner_present is None else owner_present):
            return "真人当前在场"
        # 4) 已读闸门（DWS）：会话被 DWS 判定为“已读(无未读)”才抑制。
        #    新到的未读消息会让会话重新进入未读列表 → 不抑制（照常回复），
        #    从而规避“漏回追问”旧事故。仅当 DWS 未读状态失真时才可能漏回，
        #    可用 config.poller.suppress_when_owner_read=false 关闭。
        if getattr(self.config.poller, "suppress_when_owner_read", False) \
                and self._owner_conversation_is_read(message):
            return "DWS 判定会话已读"
        return None

    def _record_gate_decision(self, message: Message, reason: str) -> None:
        """记录被门控/已回复等原因跳过的消息到决策追踪。

        保持「最近消息」与「决策追踪」同步：这些消息虽未进入 LLM/规则处理，
        但同样是系统对入站消息的处置决策，应纳入追踪避免面板显示陈旧数据。
        """
        tracker.record(
            sender_id=message.sender_id or "",
            sender=message.sender_name or "",
            conversation_id=message.chat_id or "",
            chat=message.chat_name or message.chat_id,
            content=(message.content or "")[:80],
            intent="gate",
            action="skip",
            reply_preview=reason[:80],
            platform_id=_active_platform_ctx.get(),
        )

    def _should_reply_now(self, message: Message) -> bool:
        """发送前最后一刻的权威裁决（后置兜底）：此刻是否允许 AI 自动回复。

        前置过滤（_handle_message_with_rid 进入 LLM 前）已先省一轮 Token；
        此处为并发兜底——LLM 生成耗数秒~数十秒，人工完全可能在该窗口内回复/在场，
        故在 _send_reply 真正发送前必须再判一次（_reply_gate_reason 共用同套闸门）。
        返回 True = 允许发送；任一闸门返回“不回复”即整体放弃发送。
        """
        reason = self._reply_gate_reason(message)
        if reason is not None:
            logger.info("[门控] 发送前复核：%s，放弃发送", reason)
            return False
        return True

    def _owner_conversation_is_read(self, message: Message) -> bool:
        """DWS 已读闸门：本会话当前是否被 DWS 判定为“已读(无未读)”。

        通过 chat_message_list_unread_conversations 取未读会话集合，若本会话不在其中
        （或无未读计数）即视为 owner 已读全部消息。结果按会话缓存（TTL 30s）避免热路径
        频繁调 DWS。异常时保守返回 False（不抑制，照常回复），避免 DWS 抖动误杀正常回复。
        """
        try:
            unread_ids = self._unread_conversation_ids()
        except Exception as e:
            logger.warning("[已读闸门] 查询未读会话失败，保守放行回复: %s", e)
            return False
        cid = message.chat_id or ""
        # 未知（DWS 结构异常）时不抑制：让人工/正常回复照常进行
        if getattr(self, "_unread_conv_unknown", False):
            return False
        return cid != "" and cid not in unread_ids

    def _unread_conversation_ids(self) -> set[str]:
        """取 DWS 未读会话 ID 集合（openConversationId），带 30s TTL 缓存。"""
        now = time.time()
        # 每次进入先清「未知」哨兵；仅当本次查询结构异常才重新置位，
        # 避免上一轮的未知状态污染后续成功的真实查询/缓存命中。
        self._unread_conv_unknown = False
        cache = getattr(self, "_unread_conv_cache", None)
        if cache is not None and (now - cache[1]) < 30:
            return cache[0]
        try:
            convs = self.dws.chat_message_list_unread_conversations(
                getattr(self.config.poller, "unread_conversation_count", 20))
        except Exception:
            # 查询失败：不让缓存被空结果污染，保留旧缓存（若有）
            if cache is not None:
                return cache[0]
            raise
        # 防御：DWS 返回的未读结构异常（非 list）时，无法判定会话已读，保守放行（不抑制）。
        # 注意：此处返回空集合会被上层视为「全部已读」而抑制回复，方向错误——
        # 故用哨兵标记，让 _owner_conversation_is_read 据此判定「不抑制」。
        if not isinstance(convs, list):
            logger.warning("[已读闸门] 未读会话接口返回非 list 结构，保守放行回复")
            self._unread_conv_unknown = True
            return set()
        ids: set[str] = set()
        for c in (convs or []):
            if isinstance(c, dict):
                cid = c.get("openConversationId") or c.get("chat_id") or ""
                if cid:
                    ids.add(cid)
        self._unread_conv_cache = (ids, now)
        return ids
    def _is_message_from_self(self, message: Message) -> bool:
        """安全网：判断消息是否是自己发出的，防止漏过 poller 的自我过滤导致 AI 回复自己。

        poller._is_self_message 是主防线，本方法是兜底——多一层防护。
        极端情况（openDingTalkId 未解析成功、sender 字段为空等）下 poller
        可能漏判，此处基于 main 进程自身的用户标识再做一次确认。
        """
        # 防御性取值：发送前复核（_should_reply_now）也会调用本方法，消息对象可能
        # 来自非标准构造（如测试桩），缺字段时按“非自身”处理，不抛异常。
        sender_id = getattr(message, "sender_id", "") or ""
        sender_name = getattr(message, "sender_name", "") or ""
        raw = getattr(message, "raw", None) or {}
        # 匹配 sender_id：openDingTalkId 或 userId
        if sender_id:
            oid = getattr(self, "current_open_dingtalk_id", "")
            uid = getattr(self, "current_user_id", "")
            if (oid and sender_id == oid) or (uid and sender_id == uid):
                return True
        # 匹配 sender_name（strip 后比较，兼容首尾空格）
        # 注意：此匹配为弱兜底，若群内有同名用户可能导致误过滤。
        # sender_id 匹配已覆盖主体场景，name 匹配仅作为辅助手段。
        user_name = getattr(self, "current_user_name", "")
        if sender_name and user_name:
            if sender_name.strip() == user_name.strip():
                logger.warning(
                    "检测到同名用户 %s，sender_id 匹配优先，name 匹配仅作弱兜底",
                    sender_name,
                )
                return True
        # 兜底：raw 消息里的 senderOpenDingTalkId / senderId
        raw_sid = raw.get("senderOpenDingTalkId") or raw.get("senderId") or ""
        if raw_sid:
            oid = getattr(self, "current_open_dingtalk_id", "")
            uid = getattr(self, "current_user_id", "")
            if (oid and raw_sid == oid) or (uid and raw_sid == uid):
                return True
        return False
    def _is_internal_confirmation(self, text: str) -> bool:
        """检查回复是否为纯内部操作确认（如记忆写入的"已记录"），不应对外发送。

        仅当整个回复就是一句无实质内容的确认短语时返回 True。
        只要包含任何超出确认范围的额外信息（主语、宾语、说明等），均视为正常回复。
        """
        t = text.strip().rstrip("。！!.,;；，…")

        # 纯中文确认短语（精确匹配，不含任何实质信息）
        pure_zh = {
            "已记录", "已保存", "已添加", "已更新", "已写入", "已存储", "已归档",
            "已记住", "已备注", "已录入", "已处理", "已办妥", "已完成", "已删除",
            "记忆已保存", "已存入记忆", "已保存到记忆", "已添加至记忆",
        }
        if t in pure_zh:
            return True

        # 纯英文确认（精确匹配）
        pure_en = {"OK", "ok", "Okay", "okay", "Done", "done", "Got it", "got it"}
        if t in pure_en:
            return True

        return False
    def _is_oa_approval_message(self, message: Message) -> bool:
        """判断是否为别人转来的 OA 审批消息（msgType=oa）。

        系统/机器人账号发的 OA 通知会被 _is_system_sender 判为 system 类型，
        不会以 oa 类型到达此处；这里再兜底排除自己发出的消息。
        """
        if self._is_message_from_self(message):
            return False
        if message.msg_type == "oa":
            return True
        # 容错：极少数情况下 msg_type 未被正确识别，但内容已被解析为 OA 审批文本
        return (message.content or "").startswith("[OA审批]")
    def _oa_message_blob(self, message: Message) -> str:
        """拼接 OA 审批消息的可判定文本（content + raw 附言），统一小写。"""
        raw = message.raw if isinstance(message.raw, dict) else {}
        raw_text = " ".join(
            str(v) for v in (raw.get("text"), raw.get("content")) if v
        )
        return f"{(message.content or '')} {raw_text}".lower()
    def _oa_approval_is_question(self, message: Message) -> bool:
        """判断 OA 审批消息是否「针对审批的提问」（而非催审批）。

        含问号/怎么/为什么等标记时视为提问，应交给 LLM 处理而非固定话术。
        """
        markers = self.config.oa_approval.question_markers
        if not markers:
            return False
        blob = self._oa_message_blob(message)
        return any(m.lower() in blob for m in markers)
    def _oa_approval_is_action(self, message: Message) -> bool:
        """判断 OA 审批消息是否携带「动作指令」（转交/离职交接等）。

        如「这个流程的 xx 离职了，帮我转给 yy」——应交给 LLM 调用
        transfer_approval 等审批工具处理，而非误判为催审批回固定话术。
        """
        markers = getattr(self.config.oa_approval, "action_markers", None)
        if not markers:
            return False
        blob = self._oa_message_blob(message)
        return any(m.lower() in blob for m in markers)
    def _handle_message_impl(self, message: Message) -> None:
        # === request_id 贯穿：给整条入站消息分配一个 rid，后续所有日志/LLM/tool/store 都能 grep ===
        with request_id_scope(prefix="msg") as rid:
            return self._handle_message_with_rid(message, rid)
    def _should_skip_inbound(self, message: Message) -> bool:
        """入站预过滤：自发消息 / 配置跳过类型 / 通知签名命中时返回 True（内部已打日志）。"""
        # === 安全网：过滤自己发出的消息，防止 AI 回复自己 ===
        # poller._is_self_message 是主防线，此处兜底——防止极端情况下自己发的
        # 消息（文字、图片、文件等）误入 AI 处理管道并自动回复对方。
        if self._is_message_from_self(message):
            logger.info(
                "[自我过滤] 跳过自己发的消息（%s, 类型=%s）：%s",
                message.sender_name or "(未知)", message.msg_type,
                (message.content or "")[:40],
            )
            return True

        # === 过滤不需要回复的系统/自动消息类型 ===
        skip_types = set(self.config.poller.skip_msg_types) - set(
            getattr(self.config.poller, "graceful_fallback_msg_types", []))
        if message.msg_type in skip_types:
            logger.info(
                "跳过 %s 类型消息（来自 %s，内容: %s）",
                message.msg_type, message.sender_name, message.content[:40],
            )
            return True
        # 窄签名层：仅拦截「以真人身份推送的纯文本机器通知」（结构层已处理其余通知）
        _sig = match_notification_signature(
            message.content, message.sender_id,
            self.config.poller.skip_notification_patterns,
            self.config.poller.skip_notification_sender_ids,
        )
        if _sig:
            logger.info("跳过通知(命中签名: %s)（来自 %s）", _sig, message.sender_name)
            return True
        return False
    def _handle_media_fallback(self, message: Message) -> None:
        """P2-G 媒体类型优雅回退：语音/视频等无法处理的消息，回复引导文字而非静默忽略。"""
        fb = getattr(self.config.safety, "media_fallback_text", "") or \
            "请发文字，我暂时无法处理语音/视频消息，谢谢理解～"
        logger.info("[媒体回退] %s 发来 %s 消息，回复引导文字",
                    message.sender_name, message.msg_type)
        if not self._send_reply(message, fb):
            logger.warning("[媒体回退] 引导文字发送失败: msg_id=%s", message.msg_id[:20])
    def _handle_message_with_rid(self, message: Message, rid: str) -> None:
        # 入站预过滤（自发消息 / 跳过类型 / 通知签名）
        if self._should_skip_inbound(message):
            return

        # 标记一次真实消息到达（用于后台 LLM 任务的空闲判断：有消息=活跃，否则空闲降频）
        self._bg_throttle.note_real_message()

        # === 回复冷却检查：防止短时间内频繁回复 ===
        # 【顺序修复】冷却检查必须在获取回复锁之前，否则冷却期内的消息会先占用锁、
        # 阻塞同一会话的后续正常消息，且锁释放后仍因冷却被 return（白白加锁/解锁）。
        if self._reply_cooldown_active(message):
            return

        # === P2-G 媒体类型优雅回退：语音/视频等无法处理的消息，回复引导文字而非静默忽略 ===
        _graceful = set(getattr(self.config.poller, "graceful_fallback_msg_types", []))
        if message.msg_type in _graceful:
            self._handle_media_fallback(message)
            return

        # === 会话级回复锁：防止同一会话同时有多个回复在处理 ===
        # 惰性初始化（兼容测试用裸实例）：让锁看门狗/重试计数缺省存在。
        if not hasattr(self, "_replying_since"):
            self._replying_since: dict[str, float] = {}
        if not hasattr(self, "_reply_lock_retries"):
            self._reply_lock_retries: dict[str, int] = {}

        # 本线程持锁令牌：acquire 时登记，finally 释放时校验，防止误删他人锁
        my_token = uuid.uuid4().hex
        _now = time.time()
        with self._replying_lock:
            if message.chat_id in self._replying_chats:
                _held = self._replying_since.get(message.chat_id, 0.0)
                # 防死锁看门狗：锁已持有过久（疑似上一轮回复卡死），强制释放陈旧锁，
                # 避免会话被「假正在回复中」永久阻塞。
                if _held and (_now - _held) > _REPLY_LOCK_MAX_SECONDS:
                    logger.warning(
                        "[回复锁] 会话 %s 锁持有超 %ds（疑似卡死），强制释放陈旧锁后继续处理",
                        message.chat_name or message.chat_id[:20], _REPLY_LOCK_MAX_SECONDS,
                    )
                    # 仅移除陈旧锁条目；其持有线程仍在跑，finally 持旧令牌，
                    # 下方用新令牌重登记 → 旧线程 finally 令牌不匹配不会误删新锁
                    self._replying_chats.pop(message.chat_id, None)
                    self._replying_since.pop(message.chat_id, None)
                else:
                    # 同一会话确有回复在途：不静默丢弃，而是有限次延迟重试，
                    # 等上一条回复完成、锁释放后再处理，避免用户消息永久丢失
                    # （此前表现为「明明没回，却说正在回复中」）。
                    _retries = self._reply_lock_retries.get(message.chat_id, 0)
                    if _retries < _REPLY_LOCK_MAX_RETRIES:
                        self._reply_lock_retries[message.chat_id] = _retries + 1
                        logger.info(
                            "[回复锁] 会话 %s 正在回复中，%d/%d 秒后重试本条消息（不丢弃）",
                            message.chat_name or message.chat_id[:20],
                            _retries + 1, _REPLY_LOCK_MAX_RETRIES,
                        )
                        # Timer 在新线程触发，不继承父线程的 platform ContextVar。
                        # 复制当前（已在正确平台上下文内）的 context 带入回调，
                        # 否则重试的回复会用默认 dingtalk 适配器发送、写错库。
                        import contextvars as _cv
                        _ctx = _cv.copy_context()
                        _retry = threading.Timer(
                            _REPLY_LOCK_RETRY_DELAY,
                            _ctx.run, args=(self._handle_message_impl, message),
                        )
                        _retry.daemon = True
                        _retry.start()
                        return
                    logger.warning(
                        "[回复锁] 会话 %s 重试 %d 次仍被锁占用，放弃本条消息: %s",
                        message.chat_name or message.chat_id[:20],
                        _REPLY_LOCK_MAX_RETRIES, message.msg_id[:20],
                    )
                    return
            # 成功持锁：用本线程令牌登记（令牌用于 finally 精确释放，避免误删他人锁）
            self._replying_chats[message.chat_id] = my_token
            self._replying_since[message.chat_id] = time.time()
        # 成功持锁：清零该会话的重试计数（无论之前是否重试过）
        self._reply_lock_retries[message.chat_id] = 0

        try:
            reply_slot_acquired = False  # 防御：acquire 异常时 finally 也能安全跳过释放
            sem = None  # 防御：异常提前跳出时 finally 不崩
            # === P1-E 背压：平台级回复并发上限 ===
            # 限制同时进行的 LLM 调用数，避免重启/突发时大量请求并发打爆接口。
            # 超时则降级为串行执行（不丢弃消息，仅失去并发保护）。
            # ⚠️ 严禁嵌套获取：_send_reply 及其内部调用链（含工具调用）不得再次
            # 获取 reply_semaphore，否则内层释放后外层 finally 会
            # 二次 release 导致槽位计数错误。
            sem = getattr(self._active_ctx, 'reply_semaphore', None)
            if sem is not None:
                acquired = sem.acquire(
                    timeout=self.config.poller.reply_concurrency_timeout_seconds)
                reply_slot_acquired = acquired
            if not reply_slot_acquired:
                logger.warning(
                    "[背压] 等待回复并发槽位超时（%ds），跳过本次回复以保护并发上限：%s",
                    self.config.poller.reply_concurrency_timeout_seconds, message.msg_id[:20])
                return

            # === 检查：我是否已经回复过这条消息（在对方消息之后我有发过消息） ===
            if self._has_replied_after(message):
                logger.info("[已回复] 我已在 %s 的消息之后回复过，AI 不再回复",
                            message.sender_name)
                self._record_gate_decision(message, "消息已有回复")
                # 终态标记：该 msg_id 已被记录为「最后回复过的消息」→ _has_replied_after
                # 永久返回 True，再叠加 poller._mark_msg_processed 这层去重，真正止住轮询
                # 反复拉取→重复处理/重复入死信刷屏。
                self._mark_inbound_processed(message)
                return

            # === 门控前置：一次性算出接管/在场（H2-2026-08-08 避免重复查库），后续复用 ===
            taken_over = self._has_user_taken_over(message)
            owner_present = self._is_owner_present(message)

            # === 检查：用户是否已手动接管会话（在入站消息之后自己手动回复了对方） ===
            if taken_over:
                logger.info("[用户接管] %s 已手动回复 %s，跳过 AI 回复",
                            self.current_user_name, message.sender_name)
                self._record_gate_decision(message, "人工已接管")
                # 终态标记：接管判定单调且一旦成立永不回退，标记后 _has_replied_after
                # 对该 msg_id 永久返回 True，叠加 poller 去重，止住重复投递。
                self._mark_inbound_processed(message)
                return

            # === 检查：真人是否正参与该会话（human-in-the-loop 防穿插） ===
            # 区别于上面的被动接管：只要窗口内有真人消息即抑制 AI，真人离场超时后 AI 接管。
            # 注意：此处刻意不标记入站已处理（不调 _mark_inbound_processed）。原因：
            # _mark_inbound_processed 会 update_last_replied_msg_id，使 _has_replied_after
            # 对该 msg_id 永久返回 True，从而让 AI 永远不再回复这条消息；而真人在场是「时间窗」
            # 态（owner_present_cooldown_seconds 内真人活跃才抑制），真人离场超过窗口后 AI 应当
            # 接管。若在此标记，会直接掐死已文档化的「离场后接管」行为，故不可补。
            if owner_present:
                logger.info("[真人在场] %s 最近在会话 %s 活跃，AI 暂不出声",
                            self.current_user_name, message.chat_name or message.chat_id[:20])
                self._record_gate_decision(message, "真人在场")
                return

            logger.info(
                "来自 %s 在%s的新消息: %s",
                message.sender_name,
                message.chat_name or message.chat_id,
                message.content[:50],
            )

            # === OA 审批转发：催审批 → 固定话术（不调 LLM）；提问 → 交 LLM ===
            if self._handle_oa_approval_urge(message):
                return

            result = self.rule_engine.check(message)
            if self._apply_rule_result(message, result):
                return

            # === 前置过滤（双重校验·第一道）：进入 LLM 前再判一次门控 ===
            # 共用 _reply_gate_reason 与发送前复核完全相同的闸门（自身/接管/在场/已读）。
            # 命中即直接返回、不调 LLM，避免无效 Token 消耗；并标记入站已处理，
            # 避免轮询反复重试刷屏。发送前复核(_should_reply_now)保留为并发兜底，
            # 防止 LLM 生成期间人工状态变化导致重复回复。
            _gate_reason = self._reply_gate_reason(
                message, taken_over=taken_over, owner_present=owner_present)
            if _gate_reason is not None:
                logger.info("[门控] 前置过滤命中：%s，跳过 LLM 处理（来自 %s）",
                            _gate_reason, message.sender_name)
                self._record_gate_decision(message, _gate_reason)
                self._mark_inbound_processed(message)
                return

            logger.info("没有规则匹配，调用LLM代理...")
            self._process_llm_reply(message, result)
        finally:
            # 释放平台级回复并发槽位（背压）
            if reply_slot_acquired and sem is not None:
                sem.release()
            # 释放会话级回复锁：仅当仍是本线程令牌时才释放，
            # 杜绝「陈旧锁看门狗释放 + 旧线程 finally 误删新锁」导致的同会话并发重复回复
            with self._replying_lock:
                if self._replying_chats.get(message.chat_id) == my_token:
                    self._replying_chats.pop(message.chat_id, None)
                    self._replying_since.pop(message.chat_id, None)
            # 周期性清理过期的发送退避键（每处理 50 条消息清一次）
            with self._metrics_lock:
                self._backoff_cleanup_counter += 1
                if self._backoff_cleanup_counter >= 50:
                    self._backoff_cleanup_counter = 0
                    need_backoff_cleanup = True
                else:
                    need_backoff_cleanup = False
            if need_backoff_cleanup:
                self._cleanup_backoff()
    def _handle_oa_approval_urge(self, message: Message) -> bool:
        """OA 审批转发处理：催审批 → 固定话术并返回 True；提问/动作指令 → 返回 False 交 LLM。"""
        if not (self.config.oa_approval.enabled and self._is_oa_approval_message(message)):
            return False
        if self._oa_approval_is_question(message):
            logger.info("[OA审批] 含提问语义，交由 LLM 处理")
            return False
        if self._oa_approval_is_action(message):
            logger.info("[OA审批] 含动作指令（转交/交接等），交由 LLM 调用审批工具处理")
            return False
        reply_text = self.config.oa_approval.urge_reply_text
        logger.info("[OA审批] 催审批 → 固定话术回复（不调 LLM）：%s", reply_text[:40])
        tracker.record(
            sender_id=message.sender_id or "",
            sender=message.sender_name or "",
            conversation_id=message.chat_id or "",
            chat=message.chat_name or message.chat_id,
            content=(message.content or "")[:80],
            intent="oa.approval.urge",
            action="reply-rule",
            reply_preview=reply_text[:80],
            platform_id=_active_platform_ctx.get(),
        )
        if not self._send_reply(message, reply_text):
            logger.warning("[OA审批] 固定话术发送失败: msg_id=%s，显式标记防安全网拦截",
                           message.msg_id[:20])
            self._mark_inbound_processed(message)
        return True
    def _apply_rule_result(self, message: Message, result) -> bool:
        """处理规则引擎结果：skip / 固定回复时完成处理并返回 True；返回 False 表示交 LLM。"""
        if result.action == "skip":
            logger.info("跳过: %s", result.reason)
            tracker.record(
                sender_id=message.sender_id or "",
                sender=message.sender_name or "",
                conversation_id=message.chat_id or "",
                chat=message.chat_name or message.chat_id,
                content=(message.content or "")[:80],
                intent=result.intent or "business",
                action="skip",
                platform_id=_active_platform_ctx.get(),
            )
            return True

        if result.action == "reply" and result.reply_text:
            reply_text = result.reply_text.replace("{now}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            tracker.record(
                sender_id=message.sender_id or "",
                sender=message.sender_name or "",
                conversation_id=message.chat_id or "",
                chat=message.chat_name or message.chat_id,
                content=(message.content or "")[:80],
                intent=result.intent or "business",
                action="reply-rule",
                reply_preview=reply_text[:80],
                platform_id=_active_platform_ctx.get(),
            )
            if self._is_internal_confirmation(reply_text):
                logger.info("[内部确认] 规则引擎回复为纯内部确认，跳过发送: %s", reply_text[:40])
                return True
            if not self._send_reply(message, reply_text):
                logger.warning("[发送失败] 规则引擎回复发送失败，显式标记: %s", reply_text[:40])
                self._mark_inbound_processed(message)
            return True

        return False
