from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.models import Message
from src.poller_mixins_base import PollerMixinBase
from src.poller_utils import match_notification_signature

logger = logging.getLogger(__name__)


class DiscoveryMixin(PollerMixinBase):
    def _get_recent_conversations_from_db(self) -> list[dict]:
        """从数据库获取最近有消息的会话列表（不依赖未读标记）。"""
        try:
            recent = self.store._conversation_repo.get_recent_conversations(limit=20)
            logger.debug("[轮询器] 已从数据库取到 %d 条最近会话", len(recent))

            # 转换为统一格式
            result = []
            for conv in recent:
                chat_id = conv.get("chat_id", "")
                # 会话级 chat_id 前缀因平台而异：飞书是 oc_（openConversationId，带下划线），
                # 钉钉是 cid 前缀（后面直接跟 base64，无下划线，如 cidWBNsDj5f...）；
                # ou_xxx 是飞书用户级 ID，不能作为会话级 chat_id。
                # 原逻辑只放行 oc_，导致钉钉(cid*)会话被全部过滤，DB 兜底轮询层对钉钉
                # 完全失效——表现为"飞书每轮查 35 个会话、钉钉只查 3-4 个（仅置顶）"。
                # 修正：飞书 oc_ / 钉钉 cid* 均放行，仅过滤非会话级 ID（如 ou_）。
                if not (chat_id.startswith("oc_") or chat_id.startswith("cid")):
                    logger.debug("[轮询器] 跳过 DB 中非法 chat_id（需 oc_/cid* 前缀）: %s", chat_id[:24])
                    continue
                result.append({
                    "openConversationId": chat_id,
                    "singleChat": conv.get("chat_type") == "single",
                    "title": conv.get("title", ""),
                })

            return result
        except Exception as e:
            logger.warning("获取最近会话失败：%s", e)
            return []


    def _discover_conversations_via_list_all(self) -> list[tuple[str, str, bool, str]]:
        """用 list-all 按时间范围拉所有消息，自动发现新会话（含外部好友）。

        关键发现：list-all 能返回未读会话列表和置顶列表都找不到的外部好友消息！
        返回: [(openConversationId, title, is_single, sender_open_dingtalk_id), ...]
        """
        # 拉最近配置的时间窗口内的消息（足够覆盖外部好友的新消息）
        now = datetime.now()
        start_time = now - timedelta(minutes=self.config.list_all_time_window_minutes)
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # 窗口钳制：避免 live 配置把 list_all_time_window_minutes 设得过大导致每轮重扫历史。
        max_window = timedelta(days=self.config.list_all_max_window_days)
        if start_time < now - max_window:
            clamped = now - max_window
            start_time = clamped
            start_str = clamped.strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                "[轮询器] list-all(发现) 时间窗过长，钳制为最近 %d 天（起点 %s）",
                self.config.list_all_max_window_days, start_str,
            )

        result = self.dws.chat_message_list_all(
            start_str, end_str, limit=100, max_pages=self.config.list_all_max_pages)
        conv_list = result.get("conversationMessagesList", []) if isinstance(result, dict) else []
        # 发现阶段也把死会话拉黑（如已退群、被踢），避免后续每轮重复请求
        if isinstance(result, dict) and result.get("blocked_chats"):
            nb = self._block_chats_from_list_all(result, source="feishu_discovery")
            if nb:
                logger.info("[轮询器] 发现阶段已将 %d 个不可达会话拉入黑名单", nb)
        logger.debug("[轮询器] list-all 查询在时间范围内返回 %d 条会话", len(conv_list))

        discovered = []
        for conv in conv_list:
            conv_id = conv.get("openConversationId", "")
            if not conv_id:
                continue
            is_single = conv.get("singleChat", False)
            title = conv.get("title", "")
            # 取第一条消息的发送者作为 peer 信息
            msgs = conv.get("messages", [])
            peer_oid = ""
            peer_name = title
            if msgs:
                first_msg = msgs[0]
                peer_oid = first_msg.get("senderOpenDingTalkId") or ""
                if not peer_name:
                    peer_name = first_msg.get("sender", "") or ""

            # 只关注别人发给我的消息（说明这是一个活跃会话）
            if peer_oid and not self._is_self_sender(peer_oid):
                discovered.append((conv_id, peer_name, is_single, peer_oid))
                logger.info("[轮询器] list-all 发现会话：%s（标题=%s，单聊=%s，对方=%s）",
                            conv_id[:30], peer_name, is_single, peer_oid)

        return discovered


    def _fetch_messages_via_list_all(self) -> list[Message]:
        """直接用 list-all 拉最近消息并返回（含外部好友，无需 list-direct）。

        关键发现：外部好友的消息无法通过 list-direct 拉取（no permission），
        但 list-all 可以按时间范围直接返回这些消息！
        这是处理外部好友消息的唯一可靠方式。
        """
        # 飞书无真正"有未读"接口，chat_message_list_unread_conversations 返回的是
        # 最近会话列表而非实际未读状态，闸门不可靠 → 全平台豁免
        is_feishu = type(self.dws).__name__ == 'FeishuCliAdapter'

        # 用上次轮询时间作为起点（避免重复处理）
        # 首次运行时用配置的时间窗口
        now = datetime.now()
        if self._last_list_all_time is not None:
            start_time = self._last_list_all_time
        else:
            start_time = now - timedelta(minutes=self.config.list_all_first_run_minutes)
        start_str = start_time.strftime("%Y-%m-%d %H:%M:%S")
        end_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # 窗口钳制：把可能算出的超宽时间窗（如误配 22 天 / 增量游标卡死在起点）
        # 钳制到最近 list_all_max_window_days 天，避免实时轮询循环每轮重扫全部历史、
        # 永远撞分页上限刷警告。深度历史回填请走 sync_history，不要靠实时轮询循环。
        max_window = timedelta(days=self.config.list_all_max_window_days)
        if start_time < now - max_window:
            clamped = now - max_window
            logger.info(
                "[轮询器] list-all 时间窗过长（起点 %s），钳制为最近 %d 天（起点 %s）以适配实时轮询",
                start_str, self.config.list_all_max_window_days,
                clamped.strftime("%Y-%m-%d %H:%M:%S"),
            )
            start_time = clamped
            start_str = clamped.strftime("%Y-%m-%d %H:%M:%S")

        # 飞书每轮轮询优化：构建「已知相关会话白名单」，避免 +chat-list 全量翻页
        # （封顶 200 个会话 + 逐会话拉消息）。白名单 = 外部好友 + DB 中窗口内有
        # 活动的会话；再叠加适配器内部的单次最近活跃探嗅捕获新活跃会话。
        # 每隔 list_all_full_scan_interval_minutes 分钟做一次完整 +chat-list 翻页
        # （全量扫描），用于发现长期未活跃但重新活跃的新会话。
        whitelist_ids: list[str] = []
        whitelist_meta: dict[str, dict] = {}

        if is_feishu:
            # 1) 外部好友：必拉，否则漏消息（飞书 +chat-list 不含不常联系的好友）
            try:
                for ef in self.store._external_friend_repo.list_external_friends():
                    # 优先使用 external_friends 直接存储的 chat_id（oc_xxx）
                    ef_chat_id = ef.get("chat_id", "")
                    if ef_chat_id and str(ef_chat_id).startswith("oc_") and not self._is_blocked(ef_chat_id):
                        whitelist_ids.append(ef_chat_id)
                        whitelist_meta[ef_chat_id] = {
                            "title": ef.get("name", ""),
                            "chat_mode": "p2p", "singleChat": True,
                        }
                        continue
                    # 兜底：从 conversations 表反查（防 ou_xxx 污染：校验 oc_ 前缀）
                    oid = ef.get("open_dingtalk_id", "")
                    if not oid:
                        continue
                    conv = self.store._conversation_repo.get_conversation_by_peer(oid)
                    if conv:
                        conv_chat_id = str(conv.get("chat_id", ""))
                        if conv_chat_id.startswith("oc_") and not self._is_blocked(conv_chat_id):
                            whitelist_ids.append(conv_chat_id)
                            whitelist_meta[conv_chat_id] = {
                                "title": conv.get("title", ""),
                                "chat_mode": "p2p", "singleChat": True,
                            }
            except Exception as e:
                logger.debug("[轮询器] 飞书构建外部好友白名单失败: %s", e)

        # 2) DB 中窗口内有活动的会话（群 + 单聊），对僵尸会话（起点前 1h 仍无活动）跳过，
        #    避免对大量无新消息的会话空拉。
        try:
            for conv in self.store._conversation_repo.get_recent_conversations(limit=200):
                cid = conv.get("chat_id", "")
                if not cid or not str(cid).startswith("oc_"):
                    continue
                if self._is_blocked(cid):
                    # 已拉黑的会话（退群/被踢/跨租户/跨app）不再请求其消息
                    continue
                if cid in whitelist_meta:
                    continue
                last = conv.get("last_message_time")
                if last:
                    try:
                        lt = datetime.fromisoformat(last) if isinstance(last, str) else last
                        # 起点前 1 小时仍无活动 → 视为僵尸，本轮跳过
                        if lt < start_time - timedelta(hours=1):
                            continue
                    except Exception:
                        logger.warning("[resilience] silent exception in _fetch_messages_via_list_all", exc_info=True)
                ct = conv.get("chat_type", "")
                single = (ct == "single")
                whitelist_ids.append(cid)
                whitelist_meta[cid] = {
                    "title": conv.get("title", ""),
                    "chat_mode": "p2p" if single else "group",
                    "singleChat": single,
                }
        except Exception as e:
            logger.debug("[轮询器] 构建 DB 会话白名单失败: %s", e)

        # 3) 决定本轮走白名单模式还是全量扫描
        #    - 白名单为空（首次运行 / DB 为空）→ 必须全量扫描以种子化
        #    - 间隔 > 0 且距上次全量扫描超过间隔 → 全量扫描发现新会话
        #    - 否则 → 白名单模式（只拉已知相关会话 + 单次最近活跃探嗅）
        #    注：间隔 = 0 表示「永不自动全量扫描」，纯靠白名单 + 每轮探嗅发现新会话，
        #        但仍保留首次（白名单为空）的一次种子化全量扫描。
        use_full_scan = False
        if not whitelist_ids:
            use_full_scan = True
            logger.debug("[轮询器] list-all 白名单为空，走全量扫描（首次种子化）")
        elif self.config.list_all_full_scan_interval_minutes > 0 and (
                self._last_full_scan_time is None
                or (now - self._last_full_scan_time).total_seconds()
                >= self.config.list_all_full_scan_interval_minutes * 60):
            use_full_scan = True
            logger.debug("[轮询器] list-all 触发周期全量扫描（发现新会话），白名单=%d",
                         len(whitelist_ids))

        if use_full_scan:
            result = self.dws.chat_message_list_all(
                start_str, end_str, limit=100, max_pages=self.config.list_all_max_pages)
            self._last_full_scan_time = now
        else:
            logger.debug("[轮询器] list-all 走白名单模式，仅拉 %d 个已知会话（跳过 +chat-list 全量翻页）",
                         len(whitelist_ids))
            result = self.dws.chat_message_list_all(
                start_str, end_str, limit=100,
                chat_ids=whitelist_ids, chat_meta=whitelist_meta,
                max_pages=self.config.list_all_max_pages,
            )
        conv_list = result.get("conversationMessagesList", []) if isinstance(result, dict) else []
        # 把遍历中命中的永久权限错误会话拉入当前账号黑名单，后续轮询直接跳过、不再遍历消息
        if isinstance(result, dict) and result.get("blocked_chats"):
            nb = self._block_chats_from_list_all(result, source="feishu_permission")
            if nb:
                logger.info("[轮询器] list-all 已将 %d 个不可达会话拉入黑名单", nb)
        logger.debug("[轮询器] list-all 查询到 %d 条会话", len(conv_list))

        new_messages = []
        latest_timestamp = None
        # 记录每个会话的最新消息时间，用于处理完后同步更新 _last_poll_time
        conv_latest_time: dict[str, datetime] = {}

        for conv in conv_list:
            conv_id = conv.get("openConversationId", "")
            title = conv.get("title", "")
            chat_type = self._detect_chat_type(conv)
            # 飞书自动纠错：以 API chat_mode 为准修正 DB
            chat_type = self._feishu_correct_chat_type(conv_id, title, chat_type)
            is_single = chat_type == "single"
            msgs = conv.get("messages", [])

            # （对方 openDingTalkId 提取逻辑已随"已读不回复"闸门移除而弃用）

            # P1: list-all 路径也需要检查 inaccessible 黑名单（退群/被踢等），
            # 防御边界场景：退群时 list-all 缓存中仍残留该会话。
            if self._inaccessible_conversations and conv_id in self._inaccessible_conversations:
                logger.debug("[轮询器] list-all 跳过不可访问会话: %s", conv_id[:20])
                continue

            if not msgs:
                continue

            # 规则引擎黑名单：配置级跳过，不处理该会话的消息
            if self._is_blacklisted_conversation(title, chat_type):
                logger.debug("[轮询器] list-all 跳过黑名单会话: %s（类型=%s）",
                             title or conv_id[:20], chat_type)
                continue

            # 更新会话缓存（确保下次轮询时知道这个会话）
            self.store._conversation_repo.upsert_conversation(conv_id, title, chat_type)

            # 处理消息
            for raw in msgs:
                # P2: 统一过滤链顺序：is_message_processed(raw) → 本人消息 → _raw_to_message
                # （与 per-conversation 路径一致：先去重，避免对已处理消息触发 _raw_to_message
                #   中的 _submit_image_for_ocr 造成不必要的 OCR 诊断日志刷屏）
                raw_msg_id = raw.get("openMessageId") or raw.get("msgId") or ""
                if raw_msg_id and self.store._message_repo.is_message_processed(raw_msg_id):
                    ts_str = raw.get("createTime") or raw.get("timestamp") or ""
                    if ts_str:
                        try:
                            ts_parsed = datetime.fromisoformat(ts_str)
                            if conv_id not in conv_latest_time or ts_parsed > conv_latest_time[conv_id]:
                                conv_latest_time[conv_id] = ts_parsed
                        except Exception as e:
                            logger.debug("[轮询器] list-all 时间戳解析失败: %s", e)
                    logger.debug("[轮询器] list-all 跳过已处理消息: %s", raw_msg_id[:20])
                    continue

                msg = self._raw_to_message(raw, conv_id, chat_type, title)

                # 【修复】自己发的消息（含手动发出的，不限于 bot 回复）也要落库，
                # 作为对话上下文保留，但不触发 AI 回复、不进入 new_messages。
                # 原先此处直接 continue 丢弃，导致「我主动发给别人的消息」永远不进记录
                # （尤其对方尚未回复的新会话，如新同事入职首条消息，list-all 全扫能拉到
                # 该会话但消息被丢弃 → 会话空有壳无消息）。对齐 per-conversation 路径
                # （_poll_one_conversation 已用统一的 _store_self_message_if_new 正确落库）。
                if self._is_self_message(msg):
                    self._store_self_message_if_new(msg)
                    if msg.timestamp and (conv_id not in conv_latest_time or msg.timestamp > conv_latest_time[conv_id]):
                        conv_latest_time[conv_id] = msg.timestamp
                    logger.debug("[轮询器] list-all 记录自己发的消息（%s，%s）：%s",
                                msg.sender_name,
                                "AI代发" if getattr(msg, "is_bot", False) else "真人",
                                (msg.content or "")[:30])
                    continue

                # 【无条件年龄门槛】超过 history_days 的远古消息不触发 AI 回复。
                # 与 per-conversation 路径（poller_strategy._process_raw_messages）一致。
                history_days = self.config.history_days
                if history_days > 0 and msg.timestamp:
                    age_days = (datetime.now() - msg.timestamp).total_seconds() / 86400
                    if age_days > history_days:
                        logger.info(
                            "[轮询器] list-all 跳过 %.1f 天前的远古消息（>%d 天阈值，"
                            "来自 %s，时间=%s）",
                            age_days, history_days,
                            msg.sender_name, msg.timestamp,
                        )
                        continue

                # 追踪时间戳（避免下次再拉同一批）
                if msg.timestamp:
                    if conv_id not in conv_latest_time or msg.timestamp > conv_latest_time[conv_id]:
                        conv_latest_time[conv_id] = msg.timestamp

                if not msg.msg_id:
                    continue

                # P2: 统一过滤链顺序：msg.msg_id 级别二次去重
                # （与 per-conversation 路径 L2112 一致，msg_id 可能与 raw 不同）
                if self.store._message_repo.is_message_processed(msg.msg_id):
                    logger.debug("[轮询器] list-all 跳过已处理消息(msg_id): %s", msg.msg_id[:20])
                    continue
                # 单聊：把对方的 openDingTalkId（来自消息 sender）写进会话缓存，
                # 供回复 SendMessageTool 与 list-direct 补拉使用。外部好友的会话详情
                # 接口（chat_conversation_info）拿不到对方信息，但消息 sender 里就带
                # 正确的 openDingTalkId——这是好友单聊可靠收发的关键。
                if is_single and msg.sender_id and not self._is_self_sender(msg.sender_id):
                    self.store._conversation_repo.upsert_conversation(
                        conv_id, title or msg.sender_name, "single",
                        peer_open_dingtalk_id=msg.sender_id,
                    )
                # 【关键修复】list-all 接口可能返回历史消息（时间戳远早于查询窗口），
                # 必须二次过滤，避免老消息混入导致 _has_replied_after 误判
                if msg.timestamp and msg.timestamp < start_time:
                    logger.debug("[轮询器] list-all 跳过时间窗口外的老消息: %s (%s)",
                                 msg.msg_id[:20], msg.timestamp)
                    continue
                # 图片消息：未启用 OCR 时按旧逻辑跳过
                if msg.msg_type == "image" and not self.config.image_ocr_enabled:
                    logger.debug("[轮询器] list-all：跳过图片消息（OCR 未启用，来自 %s）", msg.sender_name)
                    continue
                # 跳过系统/自动消息（OA审批、待办任务、卡片等）
                if msg.msg_type in self._effective_skip_types():
                    logger.debug("[轮询器] list-all：跳过 %s 类型消息（%s）：%s",
                                msg.msg_type, msg.sender_name, msg.content[:40])
                    continue
                # 窄签名层：仅拦截「以真人身份推送的纯文本机器通知」（结构层已处理其余通知）
                _sig = match_notification_signature(
                    msg.content, msg.sender_id,
                    self.config.skip_notification_patterns,
                    self.config.skip_notification_sender_ids,
                )
                if _sig:
                    logger.debug("[轮询器] list-all：跳过通知(命中签名: %s)（来自 %s）：%s",
                                 _sig, msg.sender_name, (msg.content or "")[:40])
                    continue

                # 群消息过滤：只处理@我的消息
                if not is_single and not self._is_at_me(raw):
                    logger.debug("[轮询器] list-all：忽略来自 %s 的群消息（未 @ 我）", msg.sender_name)
                    continue

                # 单聊已读不回复闸门已移除：每条新消息都正常回复（含飞书、外部好友）。
                logger.info("[轮询器] ✅ list-all 收到 %s 的新消息（来自 %s）：%s",
                            title or conv_id[:20], msg.sender_name, msg.content[:50])
                new_messages.append(msg)

                # 追踪全局最新消息时间戳
                if msg.timestamp and (latest_timestamp is None or msg.timestamp > latest_timestamp):
                    latest_timestamp = msg.timestamp

        # 更新 _last_list_all_time 为最新消息时间戳（不是当前时间！）
        # 必须用 conv_latest_time（所有消息的最大时间戳），不能只用未处理消息的 latest_timestamp
        # 否则全部已处理时 latest_timestamp=None，_last_list_all_time 被重置为 now，下一轮又拉同一批
        if conv_latest_time:
            max_ts = max(conv_latest_time.values())
            # 飞书时间戳精度为分钟级（create_time 无秒），直接使用 max_ts 而非 +1s，
            # 避免漏掉同分钟内其他消息。去重机制（is_message_processed）会处理重复。
            if is_feishu:
                self._last_list_all_time = max_ts
            else:
                self._last_list_all_time = max_ts + timedelta(seconds=1)
            logger.debug("[轮询器] list-all 下次起点: %s", self._last_list_all_time)
        else:
            # 这批会话里一条消息都没有，往前推配置的时间避免空转
            self._last_list_all_time = now - timedelta(minutes=self.config.empty_poll_protection_minutes)

        # 同步更新 _last_poll_time，避免单聊轮询再拉同一批消息
        for conv_id, ts in conv_latest_time.items():
            if is_feishu:
                self._last_poll_time[conv_id] = ts
            else:
                self._last_poll_time[conv_id] = ts + timedelta(seconds=1)
            logger.debug("[轮询器] list-all 同步更新 %s 的轮询时间点", conv_id[:30])

        # 合并连续消息
        if new_messages:
            new_messages = self._merge_consecutive_messages(
                new_messages, window_seconds=self.config.merge_window_seconds
            )

        return new_messages


