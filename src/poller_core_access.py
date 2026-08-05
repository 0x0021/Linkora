from __future__ import annotations

import logging
import time

from src.memory.platform_context import get_current_platform


logger = logging.getLogger(__name__)


from src.poller_mixins_base import PollerMixinBase


class AccessControlMixin(PollerMixinBase):
    """MessagePoller 子系统萃取（mixin，经多继承组合回主类）。"""

    def clear_cross_org_skips(self) -> int:
        """清空跨组织/无效会话跳过名单，下一轮重新探测所有会话。

        用于：用户在设置页切换目标组织、或后续登录了更多组织的 DWS 账号后，
        希望重新尝试之前被跳过的会话。返回被清除的会话数量。
        同时清空持久化黑名单表（与内存跳过集合保持一致）。
        """
        count = len(self._inaccessible_conversations)
        self._inaccessible_conversations.clear()
        try:
            n = self.store._blacklist_repo.clear_blocked_conversations()
            logger.info("[轮询器] 已清除 %d 个持久化黑名单会话", n)
        except Exception as e:
            logger.warning("[轮询器] 清除持久化黑名单失败: %s", e)
        logger.info("[轮询器] 已清除 %d 个跨组织/无效会话跳过记录，下一轮将重新探测", count)
        return count

    def _is_blocked(self, open_id: str) -> bool:
        """判断会话是否在不遍历黑名单中（持久化 + 内存）。

        open_id 统一去掉末尾 '=' 查询，与 _block_conversation / store 写入规范一致，
        避免 dws 返回带 padding '=' 的 open_id 时内存集合命中失败。
        """
        open_id = (open_id or "").rstrip("=")
        return bool(open_id) and open_id in self._inaccessible_conversations

    def _classify_inaccessible_reason(self, error: Exception) -> tuple[str, str]:
        """将权限错误归类为可读的「无法访问原因」，用于动态监控与日志。

        Returns:
            (reason_code, reason_text)
            - not_in_conversation: 对方已不在该会话（离职/被移出/退群/已非好友）
            - confidential: 保密群，无权限
            - no_permission: 无权限访问该会话（离开组织或群）
            - org_cli_disabled: 该组织未开启 CLI 权限或已无访问（跨组织）
            - permission_denied: 其它权限不足
        """
        err_str = str(error)
        if "130003" in err_str or "OpendId is not in conversation" in err_str or "is not in conversation" in err_str:
            return ("not_in_conversation", "对方已不在该会话中（可能已离职 / 被移出 / 已退群，或已不是好友）")
        if "保密群" in err_str or "保密" in err_str:
            return ("confidential", "保密群，无权限访问")
        if "AUTH_PERMISSION_DENIED" in err_str:
            return ("no_permission", "无权限访问该会话（可能已离开该组织或群）")
        if "TOKEN_VERIFIED_FAILED" in err_str or "该组织尚未开启 CLI 数据访问权限" in err_str:
            return ("org_cli_disabled", "该组织未开启 CLI 数据访问权限或已无访问（跨组织）")
        if "no permission" in err_str.lower():
            return ("no_permission", "无权限访问该会话")
        return ("permission_denied", "无权限访问该会话")

    def _block_conversation(self, open_id: str, title: str, chat_type: str,
                            error: Exception, source: str = "runtime_error") -> None:
        """将无权限/不可达的会话加入不遍历黑名单（持久化 + 内存，保留会话行）。

        **这是「动态监控」的核心**：谁离职了、哪个群把我踢了、我退了哪个群、
        哪个外部好友已不是好友 —— 一旦在某次遍历中触发权限错误，立即跳过该
        会话，重启后也不复发，从而彻底避免 dws 反复弹出无效验证窗。

        ⚠️ 不再删除会话行（delete_conversation）：黑名单检查本身已能跳过该会话，
        删行会丢失元数据，并导致会话从 DB 兜底层消失、即便后续访问恢复也无法自愈
        （如本次 cidWcq 被瞬时全局错误误杀后彻底失联）。保留行还能让
        _reconcile_blocklist 在访问恢复时自动解除黑名单。
        """
        reason_code, reason_text = self._classify_inaccessible_reason(error)
        # 归一化: 去掉末尾 '=' (钉钉 open_id 偶发 padding)，保证 DB 与内存集合一致
        open_id = (open_id or "").rstrip("=")
        if not open_id:
            return
        try:
            self.store._blacklist_repo.add_blocked_conversation(
                chat_id=open_id,
                chat_name=title or "",
                chat_type=chat_type or "",
                reason=reason_code,
                source=source,
                last_error=str(error),
            )
        except Exception as e:
            logger.warning("[轮询器] 写入黑名单失败（不影响内存跳过）: %s", e)
        # ⚠️ 不再调用 store.delete_conversation 删除会话行：黑名单检查（_is_blocked
        # + 内存 _inaccessible_conversations）已能在轮询时跳过该会话，删行是破坏性
        # 副作用——会丢失会话元数据，且删行后该会话从 DB 兜底层消失、即便访问恢复
        # 也无法自愈。保留行还能让 _reconcile_blocklist 在访问恢复时自动解除黑名单。
        already = open_id in self._inaccessible_conversations
        self._inaccessible_conversations.add(open_id)
        if already:
            # 已拉黑（如每轮重复命中）：避免重复 WARN 刷屏，降为 debug
            logger.debug(
                "[轮询器] 会话已在黑名单中（来源=%s）：%s (%s) %s",
                source, title or "未知", chat_type, open_id
            )
        else:
            logger.warning(
                "[轮询器] 已加入不遍历黑名单（来源=%s）：%s (%s)\n"
                "  会话 ID: %s\n  原因: %s",
                source, title or "未知", chat_type, open_id, reason_text
            )

    def _block_chats_from_list_all(self, result, source: str = "feishu_permission") -> int:
        """消费 chat_message_list_all 返回的 blocked_chats，把遍历中命中永久权限错误的
        会话拉入当前账号黑名单（blocked_conversations，已 per-account 隔离），
        后续轮询直接跳过、不再遍历消息。

        这是「不反复锤无效会话」的核心：跨租户 / 跨 app / 已退群等会话在当前账号下
        永远不可能成功，拉黑后轮询不再请求其消息，自然不再刷权限错误日志。

        Returns:
            本次处理后待拉黑的会话数（已拉黑的不重复计数）。
        """
        blocked = (result or {}).get("blocked_chats") or []
        n = 0
        for b in blocked:
            cid = (b.get("chat_id") or "").strip()
            if not cid:
                continue
            err = Exception(b.get("error") or "permanent permission error")
            self._block_conversation(
                cid, b.get("title", ""), b.get("chat_type", ""), err, source
            )
            n += 1
        return n

    def _should_skip_longtail_fetch(self, open_id: str, forced: bool) -> bool:
        """长尾会话是否应跳过本轮抓取（限频）。

        forced(未读会话)=False 永不跳过；interval<=0 关闭限频；从未抓取过也不跳过。
        仅当距上次真实抓取不足 min_conversation_poll_interval_seconds 时跳过。
        """
        if forced:
            return False
        interval = getattr(self.config, "min_conversation_poll_interval_seconds", 60) or 0
        if interval <= 0:
            return False
        last = self._last_fetch_time.get(open_id, 0.0)
        if not last:
            return False
        return (time.time() - last) < interval

    def _get_cached_top_conversations(self) -> list:
        """获取置顶/最近会话列表（带 TTL 缓存）。

        会话列表极少变化，无需每轮(默认5s)都打 DWS 的 chat_list_top_conversations。
        缓存有效期内直接返回内存副本；过期或首次才真正请求。请求失败则抛出，
        交由调用方的 try/except 处理（与直接调用行为一致）。
        """
        ttl = getattr(self.config, "top_convs_cache_ttl_seconds", 120) or 120
        now = time.time()
        if self._top_convs_cache and (now - self._top_convs_cache_ts) < ttl:
            self._top_cache_hit_flag = True
            return self._top_convs_cache
        self._top_cache_hit_flag = False
        fresh = self.dws.chat_list_top_conversations(limit=100)
        self._top_convs_cache = fresh or []
        self._top_convs_cache_ts = now
        return self._top_convs_cache

    def _reconcile_blocklist(self) -> int:
        """动态对账：把已恢复访问的会话从黑名单移除（自愈）。

        双路自愈：
        1) list-top（快、不弹窗）：被黑名单会话若出现在可访问置顶集合中，直接解除；
        2) 直接探测：置顶集合未包含的被黑名单会话，逐个用 chat_message_list 轻量探测
           （limit=1），能拉到（哪怕空列表）即视为访问已恢复并解除。这避免「未被置顶
           的群（如部门群）永远不出现在 list-top 而无故永久拉黑」的隐患——
           之前依赖 list-top 判断恢复，对没置顶的群等于无法自愈。
        """
        try:
            accessible = self.dws.chat_list_top_conversations(limit=100)
            accessible_ids = {c.get("openConversationId") for c in accessible
                             if c.get("openConversationId")}
        except Exception as e:
            logger.warning("[轮询器] 黑名单对账获取可访问会话失败（改用直接探测）: %s", e)
            accessible_ids = set()
        blocked = self.store._blacklist_repo.load_blocked_conversations()
        if not blocked:
            return 0
        # 1) list-top 命中：免费解除（无探测成本），先全部过一遍
        unblocked = 0
        remaining = []
        skipped_irrecoverable = 0
        for b in blocked:
            cid = b.get("chat_id", "")
            name = (b.get("chat_name") or cid)[:24]
            if cid in accessible_ids:
                self.store._blacklist_repo.remove_blocked_conversation(cid)
                self._inaccessible_conversations.discard(cid)
                self._perm_fail_streak.pop(cid, None)
                unblocked += 1
                logger.info("[轮询器] 黑名单自愈(list-top)：%s 已恢复访问，重新纳入遍历", name)
                continue
            # 跳过已知不可恢复的会话（工作通知、空 title 等），避免无效探测触发 DWS 重试
            full_name = b.get("chat_name", "")
            if not full_name or full_name.startswith("工作通知"):
                skipped_irrecoverable += 1
                logger.debug("[轮询器] 黑名单对账跳过不可恢复会话: %s", cid)
                continue
            remaining.append((cid, name))
        if skipped_irrecoverable:
            logger.debug("[轮询器] 黑名单对账跳过 %d 个不可恢复会话", skipped_irrecoverable)
        # 2) 直接探测分批轮转：每轮只探测最多 batch_size 个，跨轮覆盖全部
        #    （自愈是恢复机制，不要求即时；避免黑名单较多时一次性打爆 DWS 接口）
        #    改用 chat_conversation_info 轻量探测：原 chat_message_list(cid, "2020-01-01", 1)
        #    内部走 chat_message_list_all 分窗全扫，对保密/大群会触发 dws 分页长时间挂起，
        #    导致启动/对账卡死。conversation-info 是单次调用、不翻页、不扫历史，可快速判断
        #    会话是否仍对当前身份可见。
        if remaining:
            batch_size = getattr(self.config, "reconcile_probe_batch_size", 5) or 5
            start = self._reconcile_probe_idx % len(remaining)
            batch = remaining[start:start + batch_size]
            for cid, name in batch:
                try:
                    probe = self.dws.chat_conversation_info(cid)
                    if not isinstance(probe, dict):
                        logger.debug("[轮询器] 黑名单对账探测返回非 dict，保持拉黑: %s", name)
                        continue
                    self.store._blacklist_repo.remove_blocked_conversation(cid)
                    self._inaccessible_conversations.discard(cid)
                    self._perm_fail_streak.pop(cid, None)
                    unblocked += 1
                    logger.info("[轮询器] 黑名单自愈(探测)：%s 已恢复访问，重新纳入遍历", name)
                except AttributeError:
                    # 非钉钉适配器没有 chat_conversation_info；保持拉黑，由该平台自身逻辑处理
                    logger.debug("[轮询器] 当前适配器不支持 chat_conversation_info，跳过探测: %s", name)
                except Exception as e:
                    # 仍不可达，保持拉黑（保密群 / 已退群 / 被踢等）
                    logger.debug("[轮询器] 黑名单对账探测失败: %s | %s", name, e)
            self._reconcile_probe_idx = (start + len(batch)) % len(remaining)
        if unblocked:
            logger.info("[轮询器] 黑名单对账完成：解除 %d 个已恢复访问的会话", unblocked)
        return unblocked

    def _warn_permission_once(self, key: str, message: str) -> None:
        """权限相关警告只打印一次，避免日志刷屏。"""
        if key not in self._perm_warned:
            self._perm_warned.add(key)
            logger.warning("[轮询器] %s", message)

    def _is_permission_error(self, error: Exception) -> bool:
        """判断是否是"无法访问某个会话"的错误（应跳过该会话，避免反复重试）。

        覆盖的错误类型：
        - 130003：OpendId is not in conversation（用户不在该群/会话中）
        - 1001 + 特定 message：会话级权限/属性错误（保密群、无法获取消息等）
        - "no permission" 但非 TOKEN_VERIFIED_FAILED
        注意：单纯的 TOKEN_VERIFIED_FAILED、AGENT_CODE_NOT_EXISTS 是组织级问题。
        """
        err_str = str(error)

        # 全局问题不算会话级
        if self._is_global_permission_error(error):
            return False

        has_1001 = "1001" in err_str
        has_130003 = "130003" in err_str
        is_not_in_conversation = "OpendId is not in conversation" in err_str or "is not in conversation" in err_str
        has_no_permission = "no permission" in err_str
        has_no_access = "无法获取" in err_str or "无法访问" in err_str
        is_confidential = "保密群" in err_str or "保密" in err_str
        is_forbidden = "forbidden" in err_str.lower() or "禁止" in err_str
        is_auth_perm_denied = "AUTH_PERMISSION_DENIED" in err_str  # 会话级权限不足
        # 参数级错误：DWS CLI 的 chat message list --group <cid> 对部分会话
        # 无法正确映射 openCid/cid 参数，API 返回 "openCid or cid is required"。
        # 这类错误不会自愈（非瞬时抖动），应拉黑该会话、改由 list-all 通道覆盖。
        is_param_required = "openCid or cid is required" in err_str

        return (
            has_130003
            or is_not_in_conversation
            or (has_1001 and (is_not_in_conversation or has_no_access or is_confidential))
            or (has_no_permission and "TOKEN_VERIFIED_FAILED" not in err_str)
            or is_confidential
            or (is_forbidden and "TOKEN_VERIFIED_FAILED" not in err_str)
            or is_auth_perm_denied
            or is_param_required
        )

    def _is_global_permission_error(self, error: Exception) -> bool:
        """判断是否是全局/组织级别的权限问题（影响所有会话，不应删除单个会话）。

        这些错误不是某个会话的权限问题，而是整个 dws 的认证/组织权限问题。
        """
        err_str = str(error)
        return (
            "TOKEN_VERIFIED_FAILED" in err_str
            or "该组织尚未开启 CLI 数据访问权限" in err_str
            or "AGENT_CODE_NOT_EXISTS" in err_str
        )

    def _register_perm_failure(self, open_id: str) -> tuple[bool, int]:
        """记录一次群聊权限失败，返回 (是否达到拉黑阈值, 当前连续失败次数)。

        达到阈值时返回 True 并清除该会话计数（已确认不可达，交给 _block_conversation
        处理）；未达返回 False，计数保留供下轮累计。正常拉取成功时应调用
        ``self._perm_fail_streak.pop(open_id, None)`` 清除计数（瞬时错误自愈）。
        """
        threshold = getattr(self.config, "blacklist_min_consecutive_failures", 3) or 0
        self._perm_fail_streak[open_id] = self._perm_fail_streak.get(open_id, 0) + 1
        streak = self._perm_fail_streak[open_id]
        if threshold <= 0 or streak >= threshold:
            self._perm_fail_streak.pop(open_id, None)
            return True, streak
        return False, streak

    _SYSTEM_SENDER_KEYWORDS = [
        # 钉钉官方系统
        "OA审批", "智能人事", "钉钉人事", "智能招聘", "智能会议室", "智能云打印",
        "考勤打卡", "钉钉客服", "钉钉小秘书", "钉钉管理助手", "钉钉AI表格", "钉钉-云瑞", "钉钉365会员",
        "服务小钉", "小钉", "公告", "员工服务助手", "文件小助手", "日志", "固定资产管理",
        "有成报销", "访客预约系统", "群晖告警", "七牛CDN", "MySQL连接器", "Jenkins",
        "AI助理", "AI小钉", "项目小助手", "委外通知", "OnlineDocu", "官网服务器告警",
        "工作通知",
        # 第三方应用推送
        "易快报", "魔点门禁", "易盘点",
        # 飞书官方系统 / 机器人 / 应用通知
        "飞书提醒", "飞书快译", "飞书活动", "飞书团队", "飞书机器人", "飞书绩效",
        "飞书人力", "飞书日历", "飞书审批", "飞书通知", "飞书小助手", "飞书管理后台",
        "飞书社", "飞书开放平台", "飞书安全", "飞书文档", "飞书云盘", "飞书多维表格",
        "飞书妙记", "飞书审批中心", "飞书订阅号", "飞书公告",
        "红包助手", "账号安全中心", "管理员小助手",
        "审批", "日历助手", "工作台", "GitHub 助手", "GitHub助手",
        "服务台", "伙伴云", "内容管理后台",
        "联系人助手", "联系人申请",
    ]

    def _is_system_sender(self, sender_name: str) -> bool:
        """判断发送者是否为系统账号/应用推送（非真人对话）。"""
        if not sender_name:
            return False
        for kw in self._SYSTEM_SENDER_KEYWORDS:
            if kw in sender_name:
                return True
        return False

    def _is_blacklisted_conversation(self, chat_name: str, chat_type: str) -> bool:
        """判断会话是否在规则引擎的黑名单中（配置级黑名单，应完全跳过轮询）。

        优先级：群聊检查群名黑名单，单聊检查发送者/对方名黑名单。
        rule_engine 未传入时返回 False（向后兼容，不过滤）。
        """
        if not self._rule_engine:
            return False
        if not chat_name:
            return False
        engine = self._rule_engine
        if chat_type == "single":
            return engine._matches_any(chat_name, engine._blacklist_users)
        else:
            return engine._matches_any(chat_name, engine._blacklist_groups)

    def _detect_chat_type(self, conv: dict) -> str:
        """判定会话类型：single（单聊）、group（群聊）、other（与系统账号的单聊）。

        判定优先级：
        1. singleChat 字段：由各平台适配器根据 API 返回值设置，poller 直接信任。
           飞书适配器以 ``chat_mode`` 字段为准（无二次推断），钉钉适配器以 API 的
           ``singleChat`` 字段为准。
        2. singleChat=True → 进一步判断发送者是否为系统/应用账号（other vs single）。
        3. singleChat=False → 直接归为 group。
        4. singleChat 缺失时 → 默认按 single 处理（避免外部好友漏消息），
           但消息中有 ≥3 个不同发送者时归为 group。
        """
        single_chat = conv.get("singleChat")
        title = conv.get("title", "")
        sender = conv.get("sender") or conv.get("senderName") or ""

        # 明确有 singleChat 字段时，直接用它判断
        if single_chat is True:
            # 单聊：根据对方名称/发送者判定是否是系统/应用账号
            if self._is_system_sender(title) or self._is_system_sender(sender):
                return "other"
            return "single"

        if single_chat is False:
            # 群聊不做系统账号过滤——群里的机器人消息仍属于群聊范畴
            return "group"

        # singleChat 缺失：从消息中推断（兜底路径，正常情况下不应走到这里）
        msgs = conv.get("messages", [])
        if msgs:
            senders = set()
            sender_ids = set()
            for m in msgs:
                s = m.get("sender") or m.get("senderName") or ""
                sid = m.get("senderOpenDingTalkId") or m.get("senderId") or ""
                if s:
                    senders.add(s)
                if sid:
                    sender_ids.add(sid)
            if len(senders) > 2 or len(sender_ids) > 2:
                return "group"

        # 默认单聊（避免外部好友、组织内成员等场景漏消息）
        if self._is_system_sender(title) or self._is_system_sender(sender):
            return "other"
        return "single"

    def _reclassify_existing_conversations(self, platform: str = "") -> None:
        """重新分类数据库中已有的会话（修复历史数据的 chat_type）。

        遍历所有会话，根据 peer 信息和原有类型决定是否为单聊。
        仅在启动时执行一次，修复旧数据的分类错误。

        判定逻辑：
        - 有 peer 信息或原类型为 single → 按 single 处理（_detect_chat_type 区分 single/other）
        - 原类型为 group → 保持 group，不做二次推断（飞书适配器已重构为以 chat_mode 为准，
          不再需要 sender count / 群名关键词等辅助判定）

        会话表已按账号隔离（per-account 会话库），故需传入 platform 以定位正确的库文件；
        未传入时回退到当前平台上下文。
        """
        try:
            plat = platform or get_current_platform()
            cur = self.store.conv_conn(plat).cursor()
            cur.execute("SELECT chat_id, chat_name, chat_type, peer_user_id, peer_open_dingtalk_id FROM conversations")
            rows = cur.fetchall()
            if not rows:
                return

            updates = []
            for row in rows:
                chat_id = row["chat_id"]
                chat_name = row["chat_name"] or ""
                old_type = row["chat_type"]
                has_peer = bool(row["peer_user_id"] or row["peer_open_dingtalk_id"])

                # "other" 类型（系统通知/应用会话）：保持原类型不变
                if old_type == "other":
                    continue

                # 有 peer 信息或原类型为 single → 可能是单聊
                is_single = has_peer or old_type == "single"

                # 原类型为 group 且无 peer → 保持 group
                if not is_single:
                    continue

                # 调用统一判定方法：根据名称/发送者区分 single vs other
                conv = {"title": chat_name, "singleChat": is_single}
                new_type = self._detect_chat_type(conv)

                if new_type != old_type:
                    updates.append((chat_id, new_type))

            if updates:
                count = self.store._conversation_repo.batch_update_chat_types(updates, plat)
                logger.info("[轮询器] 启动时重新分类会话：%d 个会话类型已更新", count)
        except Exception as e:
            logger.warning("[轮询器] 重新分类会话失败：%s", e)
