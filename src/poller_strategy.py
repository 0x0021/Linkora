"""轮询策略 Mixin — 主循环 / 间隔控制 / 会话发现 / 消息拉取。

从 poller.py 拆分出来，包含 poll_once 主循环及其相关联的辅助方法。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from src.dws_adapter import DwsPermissionError
from src.models import Message
from src.poller_utils import match_notification_signature
from typing import Callable

logger = logging.getLogger(__name__)

# 注：早期版本的"单聊已读不回复"闸门已移除——它依赖 DWS 未读接口判断，
# 而 bot 回复后该会话会移出未读列表、对方追问又不回填，导致漏回消息（"为什么不回复我"）。
# 现改为对每条新消息都正常回复（行为见 poll_once / discovery 主流程）。


# 钉钉保留产品名 DWS（大写，与既有日志一致）；其余用真实 CLI 二进制名；未知平台不加后缀。
_PLATFORM_CLI_LABEL: dict[str, str] = {
    "dingtalk": "DWS",
    "feishu": "lark-cli",
    "wecom": "wecom-cli",
}



from src.poller_mixins_base import PollerMixinBase


class PollerStrategyMixin(PollerMixinBase):
    """MessagePoller 子系统萃取（mixin，经多继承组合回主类）。
    
    包含 poll_once 主循环、未读会话发现、list-all 取信、会话聚合、
    飞书特定逻辑（外部联系人同步、chat_type 纠错）。
    """

    def _sync_feishu_external_contacts(self) -> None:
        """飞书启动时自动发现外部联系人并写入 external_friends 表。

        仅在飞书适配器下执行（钉钉/企微走各自的注册流程）。
        调用 feishu.sync_external_contacts() 发现外部联系人，
        将不在 external_friends 表中的联系人自动注册，零人工干预。
        """
        if type(self.dws).__name__ != "FeishuCliAdapter":
            return

        try:
            discovered = self.dws.sync_external_contacts()
        except Exception as e:
            logger.warning("[轮询器] 飞书外部联系人自动发现失败: %s", e)
            return

        if not discovered:
            logger.debug("[轮询器] 飞书外部联系人自动发现：无新联系人")
            return

        # 去重：已有 open_dingtalk_id 的不重复插入
        existing_ids = set()
        try:
            for ef in self.store._external_friend_repo.list_external_friends():
                oid = ef.get("open_dingtalk_id", "")
                if oid:
                    existing_ids.add(oid)
        except Exception:
            logger.warning("[resilience] silent exception in _sync_feishu_external_contacts", exc_info=True)

        registered = 0
        for item in discovered:
            oid = item.get("open_dingtalk_id", "")
            name = item.get("name", "")
            chat_id = item.get("chat_id", "")
            if not oid or not name:
                continue
            if oid in existing_ids:
                continue
            try:
                self.store._external_friend_repo.add_external_friend(
                    name=name,
                    open_dingtalk_id=oid,
                    chat_id=chat_id,
                    notes="自动发现-启动同步",
                )
                existing_ids.add(oid)
                registered += 1
                logger.info(
                    "[轮询器] 自动注册外部联系人: %s (open_id=%s, chat_id=%s)",
                    name, oid[:24], chat_id[:24] if chat_id else "无",
                )
            except Exception as e:
                logger.warning(
                    "[轮询器] 自动注册外部联系人失败: %s | %s", name, e)

        if registered:
            logger.info(
                "[轮询器] 飞书外部联系人自动发现完成: 新注册 %d 人，"
                "总计 %d 人", registered, len(existing_ids))
            # 刷新外部好友 ID 缓存（下一轮 poll_once 开头会重建）
    def _feishu_correct_chat_type(self, conv_id: str,
                                   title: str = "",
                                   current_chat_type: str = "") -> str:
        """飞书会话类型自动纠错：以 API chat_mode 为准，与 DB 对齐。

        调用 chat_conversation_info 获取飞书真实 chat_mode，
        若与当前 chat_type（或 DB 记录）不一致则自动 UPDATE 并日志。

        Returns:
            str: 以飞书 API 为准的 chat_type（"single" / "group" / current_chat_type）
        """
        if not conv_id or type(self.dws).__name__ != "FeishuCliAdapter":
            return current_chat_type

        # 从 DB 取当前记录值（source of truth 用于比较）
        db_type = current_chat_type
        try:
            conv = self.store._conversation_repo.get_conversation(conv_id)
            if conv:
                db_type = conv.get("chat_type") or current_chat_type
        except Exception:
            logger.warning("[resilience] silent exception in _feishu_correct_chat_type", exc_info=True)

        try:
            info = self.dws.chat_conversation_info(conv_id)
        except Exception as e:
            logger.debug(
                "[轮询器] 飞书 chat_type 纠错: 无法获取 %s 会话信息: %s",
                title or conv_id[:24], e)
            return current_chat_type

        chat_mode = (info.get("chat_mode") or "").lower()
        if not chat_mode:
            return current_chat_type

        # 飞书 chat_mode → poller chat_type 映射
        feishu_type = "single" if chat_mode == "p2p" else "group"

        if feishu_type != db_type:
            logger.debug(
                "[轮询器] 飞书 chat_type 自动纠错: %s → %s"
                "（会话=%s, chat_id=%s，以飞书 API chat_mode=%s 为准）",
                db_type, feishu_type, title or conv_id[:24],
                conv_id[:24], chat_mode,
            )
            try:
                self.store._conversation_repo.upsert_conversation(conv_id, title, feishu_type)
            except Exception as e:
                logger.warning(
                    "[轮询器] 飞书 chat_type 纠错写入失败: %s | %s", conv_id, e)

        return feishu_type
    # 单聊"已读不回复"闸门已整体移除：bot 对每条新消息都正常回复，
    # 不再依据会失真的未读状态做跳过判定（见 commit 说明）。

    def _build_group_list_all_cache(self, conversations: list[dict]) -> dict | None:
        """为所有活跃群按并集时间窗预取一次 list-all，供 per-group chat_message_list 复用。

        群消息 chat_message_list 自 F5 改用用户接口 list-all 后，每个活跃群每轮会
        独立跑一次整窗 list-all 全扫再按群过滤（N 个群 = N 次全量扫描）。这里改为：
        取所有群 time_str 的最小值（并集窗起点），只扫一次 list-all，把合并字典返回，
        由主循环作为 ``cached_result`` 传给每个群的 chat_message_list（内存过滤，零额外
        API 调用）。单聊走 list-direct、other 类型走 list-all 主通道，均不参与。

        Returns:
            chat_message_list_all 的合并字典（conversationMessagesList 按
            openConversationId 聚合）；无群可预取或预取失败时返回 None
            （主循环对该群走 chat_message_list 的 fallback 全扫，行为不变）。
        """
        now = datetime.now()
        min_dt: datetime | None = None
        min_ts: str | None = None
        for conv in conversations:
            oid = conv.get("openConversationId", "")
            if not oid or self._is_blocked(oid):
                continue
            # 与 poll_once 主循环口径一致：先 detect 再飞书纠错
            chat_type = self._detect_chat_type(conv)
            chat_type = self._feishu_correct_chat_type(oid, conv.get("title", ""), chat_type)
            # 仅群聊需要 per-group 补拉；单聊走 list-direct、other 走 list-all 主通道
            if chat_type == "single" or chat_type == "other":
                continue
            # 与主循环 time_str 计算口径一致：last_poll 优先，否则 DB last_message_time
            lp = self._last_poll_time.get(oid, now - timedelta(hours=24))
            if oid not in self._last_poll_time:
                db_conv = self.store._conversation_repo.get_conversation(oid)
                if db_conv and db_conv.get("last_message_time"):
                    try:
                        lp = datetime.fromisoformat(db_conv["last_message_time"])
                    except (ValueError, TypeError) as _exc:
                        logger.debug(f"_build_group_list_all_cache: swallowed exception: {_exc}")
                        pass
            ts = lp.strftime("%Y-%m-%d %H:%M:%S")
            if min_dt is None or lp < min_dt:
                min_dt = lp
                min_ts = ts
        if not min_ts:
            return None
        try:
            end_ts = now.strftime("%Y-%m-%d %H:%M:%S")
            return self.dws.chat_message_list_all(
                min_ts, end_ts, limit=self.config.messages_per_conversation
            )
        except Exception as e:
            logger.warning("[轮询器] 群消息批量预取失败，回退逐群扫描: %s", e)
            return None

    def poll_once(self, handler: Callable[[Message], None] | None = None) -> list[Message]:
        """轮询最近消息（六层保障）。

        第5层是 list-all：直接返回最近消息（含外部好友），无需 openConversationId。
        对于外部好友，list-direct 无权限，必须用 list-all 才能拿到消息！

        Args:
            handler: 可选消息回调。若提供，list-all 发现的消息会**在 per-conversation
                同步抓取之前**就通过 handler 即时派发（快通道），避免被后续可能挂死的
                dws CLI 调用阻塞整条派发链（见下方「快通道」注释）。为 None 时退回旧行为
                （list-all 消息随整体 return，由 run_loop 在周期末统一派发），保持测试兼容。
        """
        new_messages = []
        self._last_poll_at = datetime.now()
        logger.debug("[轮询器] poll_once() 已启动")

        # 每 N 轮用 list-top（安全、不弹窗）对账一次黑名单，自动解除已恢复访问的会话
        self._poll_count += 1
        if self._poll_count % self._reconcile_every == 0:
            try:
                self._reconcile_blocklist()
            except Exception as e:
                logger.warning("[轮询器] 周期对账黑名单失败: %s", e)

        # === 拉取未读会话列表（仅用于实时优先强制轮询 forced_ids）===
        # 注意：此处不再做任何"已读不回复"的跳过判定。旧版闸门依赖未读状态判断，
        # 但 DWS 未读接口在 bot 回复后会把会话移出未读列表、对方追问又不回填，
        # 导致漏回追问消息（"为什么不回复我"）。现改为对每条新消息都正常回复。
        unread_convs: list[dict] = []
        try:
            unread_convs = self.dws.chat_message_list_unread_conversations(
                self.config.unread_conversation_count
            )
            logger.debug("[轮询器] 发现 %d 个未读会话（实时优先）", len(unread_convs))
        except DwsPermissionError as e:
            self._warn_permission_once(
                "list_unread",
                f"无权限访问 list-unread-conversations 接口（未读会话不享实时优先）: {e}"
            )
        except Exception as e:
            logger.warning("列出未读会话失败（未读会话不享实时优先）: %s", e)

        # === 直接用 list-all 拉最近消息（含外部好友）===
        # 这是最关键的一层：外部好友的对话无法通过 list-direct 拉取（no permission），
        # 但 list-all 可以按时间范围直接返回这些消息！
        list_all_messages: list = []
        list_all_ok = False
        try:
            list_all_messages = self._fetch_messages_via_list_all()
            list_all_ok = True
            if handler is not None:
                # 【延迟修复·快通道】list-all 发现的消息「抓到即派发」，必须先于下面
                # 的 per-conversation 同步抓取执行。原因：poll_once 在单线程 run_loop
                # 里同步执行，per-conversation 抓取（chat_message_list_direct /
                # _build_group_list_all_cache 等）是同步 dws CLI 调用，一旦某个会话的
                # dws 调用挂死（dws 是 node 进程，subprocess.run(timeout=) 只杀父进程、
                # 孙进程存活，可远超 timeout 挂起），整条派发链被冻结——本已发现的消息
                # 也要等阻塞释放后才被派发，实测延迟可达 ~3 分钟。
                # 快通道在 per-conversation 抓取前就把消息送进防抖队列，保证「发现→回复」
                # 延迟稳定在 ~15s 量级，不受 per-conversation 抓取阻塞影响。
                # handler 即 run_loop 传入的平台回调（内部调 handle_message）。派发后
                # _dispatch_one 会立即标记已处理并落库，per-conversation 抓取再遇到同一条
                # 会被 is_message_processed 跳过，不会重复派发/重复回复。
                _fast_dispatched = 0
                for _m in list_all_messages:
                    try:
                        self._dispatch_one(_m, handler)
                        _fast_dispatched += 1
                    except Exception as _e:
                        logger.error("[轮询器] list-all 快通道派发失败（消息可能延迟）: %s", _e, exc_info=True)
                if _fast_dispatched:
                    logger.info(
                        "[轮询器] list-all 快通道已即时派发 %d 条新消息（不等待 per-conversation 抓取）",
                        _fast_dispatched,
                    )
            else:
                new_messages.extend(list_all_messages)
            logger.debug("[轮询器] list-all 直接返回了 %d 条新消息", len(list_all_messages))
        except Exception as e:
            if self._is_global_permission_error(e):
                # 组织级权限问题：仅按 key 去重警告一次，不阻断后续其他取信通道
                # （list-all 仅是其中一层，未读/置顶/缓存会话层会继续尝试）
                self._warn_permission_once(
                    "global_token_list_all",
                    f"list-all 取信遇到全局权限错误（不影响其他通道）: {e}"
                )
                logger.debug("通过 list-all 获取消息失败（全局权限，已静默）: %s", e)
            else:
                logger.warning("通过 list-all 获取消息失败: %s", e)

        # === list-all 主通道空轮探针 ===
        # 连续 N 轮 list-all 都拉不到任何新消息 → 告警，便于直观确认机器人确实在收信。
        # 失败（异常）不算"空"，不累加计数，避免把网络/权限错误误报成"收不到消息"；
        # 一旦某轮拉到 >=1 条，计数立即归零。0 阈值 = 关闭探针。
        if self.config.list_all_empty_alert_rounds > 0 and list_all_ok:
            if len(list_all_messages) == 0:
                self._list_all_empty_streak += 1
                # 每达到阈值轮次提醒一次（第 N、2N、3N… 轮），既显眼又不每轮刷屏
                if self._list_all_empty_streak % self.config.list_all_empty_alert_rounds == 0:
                    logger.debug(
                        "[收信探针] list-all 主通道已连续 %d 轮未拉到任何新消息"
                        "（可能正常=确实无人发消息；若持续为空请检查账号登录/组织 CLI 权限）",
                        self._list_all_empty_streak
                    )
            else:
                if self._list_all_empty_streak > 0:
                    logger.info(
                        "[收信探针] list-all 主通道恢复收信（连续空轮计数已重置，峰值 %d 轮）",
                        self._list_all_empty_streak
                    )
                self._list_all_empty_streak = 0

        # 合并未读会话 + 置顶/最近会话 + 数据库缓存的最近会话 + 外部好友，去重
        # （这些层兜底处理组织内联系人的消息，list-all 已经处理了外部好友）
        seen = set()
        all_conversations = []
        # 未读会话：实时优先，永不参与长尾限频（每轮都抓）
        forced_ids = set()

        try:
            # 1. 未读会话（实时发现新消息）—— 复用 poll_once 顶部已计算的 unread_convs
            for c in unread_convs:
                oid = c.get("openConversationId", "")
                if oid and oid not in seen and not self._is_blocked(oid):
                    seen.add(oid)
                    forced_ids.add(oid)
                    all_conversations.append(c)
            logger.debug("[轮询器] 发现 %d 个未读会话", len(unread_convs))
        except Exception as e:
            logger.warning("合并未读会话失败: %s", e)

        try:
            # 2. 置顶/最近会话列表（不依赖未读标记，捕获用户已读但系统未回复的会话）
            #    缓存化：会话列表极少变化，无需每轮(默认5s)都打 DWS 接口
            top_convs = self._get_cached_top_conversations()
            for c in top_convs:
                oid = c.get("openConversationId", "")
                if oid and oid not in seen and not self._is_blocked(oid):
                    seen.add(oid)
                    all_conversations.append(c)
            logger.debug("[轮询器] + %d 个置顶/最近会话，总计 %d", len(top_convs), len(all_conversations))
        except DwsPermissionError as e:
            self._warn_permission_once(
                "list_top",
                f"无权限访问 list-top-conversations 接口，跳过置顶会话轮询: {e}"
            )
        except Exception as e:
            logger.warning("列出置顶会话失败: %s", e)

        try:
            # 3. 数据库缓存的最近会话（兜底，防止以上两种列表都漏掉）
            db_convs = self._get_recent_conversations_from_db()
            for c in db_convs:
                oid = c.get("openConversationId", "")
                if oid and oid not in seen and not self._is_blocked(oid):
                    seen.add(oid)
                    all_conversations.append(c)
            logger.debug("[轮询器] + %d 个数据库缓存会话，总计 %d", len(db_convs), len(all_conversations))
        except Exception as e:
            logger.warning("获取数据库缓存会话失败: %s", e)

        # 4. 外部好友强制轮询（不依赖未读标记，确保外部好友消息不漏）
        # 注意：如果 list-all 已经处理了外部好友消息，这一层可能不会拉到新消息
        # （因为 list-direct 对外部好友无权限）
        try:
            external_friends = self.store._external_friend_repo.list_external_friends()
            for ef in external_friends:
                oid = ef.get("open_dingtalk_id", "")
                if not oid or oid in seen or self._is_blocked(oid):
                    continue
                # 外部好友的 open_dingtalk_id（ou_xxx）不是会话级 chat_id。
                # 必须解析为真实的会话级 chat_id（oc_xxx），否则 Message.chat_id
                # 会被污染为 ou_xxx，导致 lark-cli 拒收（invalid chat ID format）。
                # 优先使用 external_friends 直接存储的 chat_id（sync_external_contacts
                # 通过 +search-user 获取的 p2p_chat_id 即 oc_xxx），无 oc_xxx 时跳过。
                ef_name = ef.get("name", "")
                real_conv_id = ""
                ef_chat_id = ef.get("chat_id", "")
                if ef_chat_id and str(ef_chat_id).startswith("oc_"):
                    real_conv_id = ef_chat_id
                else:
                    # 兜底：从 conversations 表映射
                    try:
                        conv = self.store._conversation_repo.get_conversation_by_peer(oid)
                        if conv:
                            conv_chat_id = str(conv.get("chat_id", ""))
                            if conv_chat_id.startswith("oc_"):
                                real_conv_id = conv_chat_id
                    except Exception:
                        logger.warning("[resilience] silent exception in poll_once", exc_info=True)
                if not real_conv_id:
                    logger.debug(
                        "[轮询器] 跳过外部好友 %s：无法解析为 oc_xxx 会话ID",
                        ef_name or oid[:24],
                    )
                    continue
                if real_conv_id not in seen and not self._is_blocked(real_conv_id):
                    seen.add(real_conv_id)
                    all_conversations.append({
                        "openConversationId": real_conv_id,
                        "singleChat": True,
                        "title": ef_name,
                    })
            if external_friends:
                logger.debug("[轮询器] + %d 个外部好友（强制），总计 %d", len(external_friends), len(all_conversations))
        except Exception as e:
            logger.warning("列出外部好友失败：%s", e)

        if not all_conversations:
            logger.debug("[轮询器] 没有会话需要检查")
            # 不提前返回：即使无会话也继续走到底，使周期性统计（每 12 轮）
            # 对空平台（如尚未有会话的 wecom）也可见，满足「各平台都要有」。

        # === 群消息批量预取（消除每群独立 list-all 全扫）===
        # 主通道(list_all)每轮已覆盖全部会话的新消息；但主循环对每个群又独立跑一次
        # 整窗 list-all 再按群过滤（自 F5 改用用户接口 list-all 后的副作用），
        # N 个群 = N 次全量扫描。改为：按所有活跃群的并集时间窗只扫一次，结果作为
        # cached_result 传给每个群的 chat_message_list，把 N 次全扫压成 1 次。
        group_cache = self._build_group_list_all_cache(all_conversations)

        throttled_skip = 0  # 本轮因长尾限频而跳过的会话数（用于统计日志）
        for conv in all_conversations:
            open_id = conv.get("openConversationId", "")
            if not open_id:
                continue

            # 会话级 chat_id 前缀因平台而异：飞书是 oc_（openConversationId），
            # 钉钉是 cid 前缀（后面直接跟 base64，无下划线，如 cidWBNsDj5f...）。
            # 历史数据里曾被写入 ou_xxx（飞书用户级 ID）作为 chat_id，会被 lark-cli
            # 拒绝为 invalid chat_id (232006)。这里只放行合法会话级 ID（oc_/cid*），
            # 跳过 ou_ 等非会话级 ID，避免每轮空转报错；同时不再误杀钉钉 cid 会话。
            if not (str(open_id).startswith("oc_") or str(open_id).startswith("cid")):
                logger.debug("[轮询器] 跳过非法 chat_id（需 oc_/cid* 前缀）: %s", open_id[:24])
                continue

            # 跳过本次运行内已确认无权限的会话（已删数据库记录，避免反复报错）
            if open_id in self._inaccessible_conversations:
                logger.debug("[轮询器] 跳过无权限会话: %s", open_id[:30])
                continue

            chat_type = self._detect_chat_type(conv)
            title = conv.get("title", "")
            # 飞书自动纠错：以 API chat_mode 为准修正 DB
            chat_type = self._feishu_correct_chat_type(open_id, title, chat_type)
            is_single = chat_type == "single"

            # 规则引擎黑名单：配置级跳过，不轮询该会话（避免白拉消息）
            if self._is_blacklisted_conversation(title, chat_type):
                logger.debug("[轮询器] 跳过黑名单会话: %s（类型=%s）",
                             title or open_id[:20], chat_type)
                continue

            # 单聊已读不回复闸门已移除：每条新消息都正常回复，不在此跳过。

            # 长尾会话（非未读）按会话限频抓取：避免每个置顶/db缓存会话每轮(默认5s)
            # 都打一次 chat_message_list。list-all 主通道每轮已保底抓取全部新消息，
            # 故限频不会漏消息；未读会话(forced_ids)实时优先，永不跳过。
            if self._should_skip_longtail_fetch(open_id, open_id in forced_ids):
                throttled_skip += 1
                logger.debug("[轮询器] 会话 %s 限频跳过本轮抓取", title or open_id[:20])
                continue

            # 保存/更新会话缓存
            self.store._conversation_repo.upsert_conversation(open_id, title, chat_type)

            # 系统/第三方应用会话（other类型）：跳过直接拉取，消息通过 list-all 通道获取
            if chat_type == "other":
                logger.debug("[轮询器] 跳过系统/应用会话 %s（类型=other，消息通过 list-all 获取）", title or open_id[:20])
                continue

            last_poll = self._last_poll_time.get(
                open_id, datetime.now() - timedelta(hours=24)
            )
            # 如果用的是初始值（即第一次轮询这个会话），尝试从数据库取 last_message_time
            if open_id not in self._last_poll_time:
                conv = self.store._conversation_repo.get_conversation(open_id)
                if conv and conv.get("last_message_time"):
                    try:
                        db_time = datetime.fromisoformat(conv["last_message_time"])
                        last_poll = db_time  # 用数据库里的时间，避免漏掉消息
                        logger.debug("[轮询器] 使用数据库的 last_message_time（%s）: %s", title, db_time)
                    except Exception as e:
                        logger.debug("[轮询器] 数据库 last_message_time 解析失败: %s", e)
            time_str = last_poll.strftime("%Y-%m-%d %H:%M:%S")

            # 工作通知是钉钉系统通知渠道，list-direct API 永久返回权限错误；
            # 跳过 per-conversation 轮询，消息通过 list-all 通道正常拉取。
            if title and title.startswith("工作通知"):
                logger.debug("[轮询器] 跳过系统通知会话（工作通知），消息通过 list-all 获取: %s", title)
                continue

            # 记录本次抓取时间（供长尾限频判断，仅真正发起请求时更新）
            self._last_fetch_time[open_id] = time.time()

            try:
                if is_single:
                    # === 单聊：必须用 list-direct（--group 仅支持群聊！） ===
                    peer = self._resolve_single_chat_peer(open_id, title)
                    peer_uid = peer.get("user_id", "")
                    peer_oid = peer.get("open_dingtalk_id", "")

                    if not peer_uid and not peer_oid:
                        logger.debug(
                            "[轮询器] 跳过单聊 %s: 无法解析对方信息（标题=%s）。"
                            "如果是外部好友，请通过 API POST /api/external-friends 添加",
                            open_id, title,
                        )
                        continue

                    raw_msgs = self.dws.chat_message_list_direct(
                        user_id=peer_uid,
                        open_dingtalk_id=peer_oid,
                        time_str=time_str,
                        limit=self.config.messages_per_conversation,
                    )
                else:
                    # === 群聊：用 --group ===
                    # cached_result=group_cache：复用本轮并集窗单次扫描结果（快路径）；
                    # group_cache 为 None（无群/预取失败）时 chat_message_list 自行全扫。
                    raw_msgs = self.dws.chat_message_list(
                        open_id, time_str, self.config.messages_per_conversation,
                        cached_result=group_cache,
                    )
                logger.debug("[轮询器] 从 %s（类型=%s）获取了 %d 条原始消息",
                            title, chat_type, len(raw_msgs))
                # 拉取成功：清除该会话的连续权限失败计数（瞬时错误已自愈，无需拉黑）
                self._perm_fail_streak.pop(open_id, None)
            except Exception as e:
                if self._is_permission_error(e):
                    if is_single:
                        # ⚠️ 单聊权限错误：区分正常外部好友（不拉黑）vs 永久 cross app/不同租户。
                        # 1) 外部好友根本无法用 chat_message_list_direct 拉取（代码注释已
                        #    明确记录"外部好友的消息无法通过 list-direct 拉取"），必失败；
                        #    这是正常的，跳过即可，list-all 主通道照常覆盖其消息。
                        # 2) 已离职/被删的单聊对象不会发消息，list-all 也拉不到，不需黑名单。
                        # 3) 但跨租户(232010)/跨app(99992361)/已退群(230002)是永久性错误，
                        #    不是"外部好友无法 direct 拉"的正常情况，应拉黑避免每轮重试。
                        err_str = str(e).lower()
                        if any(kw in err_str for kw in ("cross app", "different tenants",
                                                        "out of the chat", "can not be out")):
                            self._block_conversation(open_id, title, chat_type, e,
                                                     source="runtime_error")
                        else:
                            logger.debug(
                                "[轮询器] 单聊补拉无权限（外部好友/已无会话，跳过补拉，"
                                "不影响 list-all 收发）: %s | %s", title or open_id[:20], e
                            )
                    else:
                        # 群聊权限错误：区分永久性跨租户/跨app/已退群 vs 瞬时权限抖动。
                        # 跨租户(232010)/跨app(99992361)/已退群(230002)永远不会恢复，
                        # 直接拉黑，不走累计阈值（防止每轮刷 WARNING）。
                        err_str = str(e).lower()
                        if any(kw in err_str for kw in ("cross app", "different tenants",
                                                        "out of the chat", "can not be out")):
                            self._block_conversation(open_id, title, chat_type, e,
                                                     source="runtime_error")
                        else:
                            # 普通群聊权限错误（被踢/退群/保密群等）：先累计连续失败次数，
                            # 达到阈值才拉黑——避免一次瞬时抖动（token 刷新间隙 / CLI 偶发 /
                            # 限流被钉钉报成 AUTH_PERMISSION_DENIED）就把活跃群永久误杀。
                            should_block, streak = self._register_perm_failure(open_id)
                            if should_block:
                                self._block_conversation(open_id, title, chat_type, e,
                                                         source="runtime_error")
                            else:
                                logger.warning(
                                    "[轮询器] 群 %s 第 %d 次权限错误（疑似瞬时，暂不拉黑，下轮重试）: %s",
                                    title or open_id[:20], streak, e
                                )
                elif self._is_global_permission_error(e):
                    # ⚠️ 关键修正：全局/组织级权限错误（TOKEN_VERIFIED_FAILED、
                    # 该组织尚未开启 CLI 数据访问权限、AGENT_CODE_NOT_EXISTS）是
                    # 【环境级】问题，不是某个会话自身的属性：
                    #   1) 可能是瞬时抖动（token 刷新间隙、跨组织接口偶发），下轮自动恢复；
                    #   2) 一旦某个会话命中，往往同时命中一大批（同一次环境波动），
                    #      把每一个都永久拉黑会误杀活跃会话（如 cidWcq 正在正常收发却被
                    #      删行拉黑），并造成"黑名单里 16 条全是 org_cli_disabled"的假象
                    #      （若真全局关闭，list-all 也收不到杨超萍消息）。
                    # 因此【不拉黑、不删会话行】，仅跳过本轮该会话，下轮重试；
                    # 用去重告警保留可观测性，既不刷屏也不致误杀活跃会话。
                    self._warn_permission_once(
                        f"global_perm_{open_id[:24]}",
                        f"全局/组织级权限错误，跳过本轮该会话"
                        f"（不拉黑、不删行，下轮重试）: {title or open_id[:20]} | {e}"
                    )
                    continue  # 仅跳过当前会话，继续处理下一个
                else:
                    err_str = str(e)
                    if "openCid or cid is required" in err_str:
                        logger.debug("列出 %s 的消息失败(已降级): %s", title or open_id[:20], e)
                    else:
                        logger.warning("列出 %s 的消息失败: %s", title or open_id[:20], e)
                continue

            # 如果是单聊，从消息里提取对方 openDingTalkId 并更新会话缓存
            if is_single and raw_msgs:
                # 遍历所有消息找第一个非自己的 sender（不能只取 [0]，因为最近一条可能是自己发的）
                peer_oid_from_msgs = ""
                for raw_msg in raw_msgs:
                    candidate_oid = raw_msg.get("senderOpenDingTalkId") or raw_msg.get("senderId") or ""
                    if candidate_oid and not self._is_self_sender(candidate_oid):
                        peer_oid_from_msgs = candidate_oid
                        break
                if peer_oid_from_msgs:
                    logger.debug("[轮询器] 正在更新 %s 的对方信息：openDingTalkId=%s", open_id, peer_oid_from_msgs)
                    self.store._conversation_repo.upsert_conversation(
                        open_id, title, "single",
                        peer_open_dingtalk_id=peer_oid_from_msgs
                    )

            # 收集所有消息的时间戳（含已处理/自己发的），用于正确更新 _last_poll_time
            all_timestamps: list[datetime] = []
            conv_messages = []
            is_first_poll = open_id not in self._last_poll_time
            for raw in raw_msgs:
                # 主路径先去重，避免已处理消息反复触发图片下载/OCR
                raw_id = raw.get("openMessageId") or raw.get("msgId") or ""
                if raw_id and self.store._message_repo.is_message_processed(raw_id):
                    # 仍追踪时间戳
                    ts_str = raw.get("createTime") or raw.get("timestamp") or ""
                    if ts_str:
                        try:
                            all_timestamps.append(datetime.fromisoformat(ts_str))
                        except Exception as e:
                            logger.debug("[轮询器] 主路径时间戳解析失败: %s", e)
                    logger.debug("[轮询器] 主路径跳过已处理消息: %s", raw_id[:20])
                    continue

                msg = self._raw_to_message(raw, open_id, chat_type, title)
                if msg.timestamp:
                    all_timestamps.append(msg.timestamp)
                # 【强制过滤】最早入口拦截自己发的消息（sender_id 匹配当前用户即丢弃）。
                # 这是 poller 层的第一道防线，在 _raw_to_message 构造完 Message 后立即执行，
                # 早于编辑/撤回/合并等所有后续逻辑，确保任何路径下自己发的消息都不会进入 AI 管道。
                if msg.sender_id and self._is_self_sender(msg.sender_id):
                    # 【修复 2026-07-31】自己发的消息在强制过滤前先保存到历史，否则消息记录
                    # 中会缺失主人手动发送的消息。保存逻辑与下方 _is_self_message 分支一致。
                    is_bot_msg = self._check_if_bot_message(msg)
                    msg.is_bot = is_bot_msg
                    msg.role = "assistant" if is_bot_msg else "user"
                    if not self._is_duplicate_self_message(msg):
                        try:
                            self.store._message_repo.save_message(msg, msg.role)
                        except Exception as e:
                            logger.debug("[轮询器] 保存自己发的消息失败: %s", e)
                    else:
                        logger.debug("[轮询器] 跳过双写（%s，内容前60字符已存在）：%s",
                                    msg.sender_name, msg.content[:30])
                    logger.debug("[轮询器] 强制过滤：丢弃自己发的消息（%s，类型=%s，已入库）：%s",
                                msg.sender_name, msg.msg_type, (msg.content or "")[:30])
                    continue
                # msg_id 为空时用备选 ID（chat_id + sender_id + content + timestamp）避免反复拉取
                if not msg.msg_id:
                    sender_part = msg.sender_id or "unknown"
                    alt_id = f"{msg.chat_id}:{sender_part}:{msg.content[:30]}:{msg.timestamp}"
                    logger.debug("[轮询器] 消息无 msg_id，使用备选 ID: %s", alt_id[:50])
                    msg.msg_id = alt_id

                # 【消息编辑处理】更新本地消息记录
                if msg.msg_type == "edit":
                    self._handle_edit_message(msg)
                    continue

                # 【消息撤回处理】删除本地消息记录
                if msg.msg_type == "recall":
                    self._handle_recall_message(msg)
                    continue

                # 【首次运行忽略老消息】重启后第一次轮询某会话时，忽略超过N分钟的老消息
                if is_first_poll and self.config.first_run_ignore_older_than_minutes > 0 and msg.timestamp:
                    age_minutes = (datetime.now() - msg.timestamp).total_seconds() / 60
                    if age_minutes > self.config.first_run_ignore_older_than_minutes:
                        logger.debug("[轮询器] 首次运行忽略 %d 分钟前的老消息（来自 %s，时间=%s）",
                                    int(age_minutes), msg.sender_name, msg.timestamp)
                        continue
                if self._is_self_message(msg):
                    # 保存主人自己发的消息，然后跳过
                    # 通过数据库检查该消息是否已存在且为 AI 发送（role=assistant）
                    is_bot_msg = self._check_if_bot_message(msg)
                    msg.is_bot = is_bot_msg
                    msg.role = "assistant" if is_bot_msg else "user"
                    # 【双写去重】轮询器拉到 DWS 真实 msg_id 时不重复保存
                    if not self._is_duplicate_self_message(msg):
                        try:
                            self.store._message_repo.save_message(msg, msg.role)
                        except Exception as e:
                            logger.debug("[轮询器] 保存自己发的消息失败: %s", e)
                    else:
                        logger.debug("[轮询器] 跳过双写（%s，内容前60字符已存在）：%s",
                                    msg.sender_name, msg.content[:30])
                    logger.debug("[轮询器] 跳过自己发的消息（%s，%s）：%s",
                                msg.sender_name, "AI代发" if is_bot_msg else "真人", msg.content[:30])
                    continue
                # 图片消息：未启用 OCR 时按旧逻辑跳过（启用时已在 _raw_to_message 中识别为文字）
                if msg.msg_type == "image" and not self.config.image_ocr_enabled:
                    logger.debug("[轮询器] 跳过图片消息（OCR 未启用，来自 %s）", msg.sender_name)
                    continue
                # 跳过系统/自动消息（OA审批、待办任务、卡片、语音、视频等），无需 AI 回复
                if msg.msg_type in self._effective_skip_types():
                    logger.debug("[轮询器] 跳过 %s 类型消息（%s）：%s",
                                msg.msg_type, msg.sender_name, msg.content[:40])
                    continue
                # 窄签名层：仅拦截「以真人身份推送的纯文本机器通知」（结构层已处理其余通知）
                _sig = match_notification_signature(
                    msg.content, msg.sender_id,
                    self.config.skip_notification_patterns,
                    self.config.skip_notification_sender_ids,
                )
                if _sig:
                    logger.debug("[轮询器] 跳过通知(命中签名: %s)（来自 %s）：%s",
                                 _sig, msg.sender_name, (msg.content or "")[:40])
                    continue
                if self.store._message_repo.is_message_processed(msg.msg_id):
                    logger.debug("[轮询器] 来自 %s 的消息已处理过，忽略：%s", msg.msg_id[:20], title)
                    continue
                # 群消息过滤：只处理@我的消息
                if chat_type == "group" and not self._is_at_me(raw):
                    logger.debug("忽略来自 %s 的群消息（未 @ 我）", msg.sender_name)
                    continue
                logger.info("[轮询器] ✅ 收到 %s 的新消息（来自 %s）：%s", title, msg.sender_name, msg.content[:50])
                conv_messages.append(msg)

            # 合并同一人的连续消息
            logger.debug("[轮询器] 在 %s 中过滤得到 %d 条新消息", title, len(conv_messages))
            
            # 用消息里的对方信息反写会话缓存（确保下次轮询时 peer 信息已有）
            if conv_messages and is_single:
                peer_id = conv_messages[0].sender_id  # sender_id 即对方的 openDingTalkId
                peer_name = conv_messages[0].sender_name
                if peer_id and (not peer["open_dingtalk_id"] and not peer["user_id"]):
                    logger.debug("[轮询器] 从消息中更新对方信息：%s → %s", open_id, peer_id)
                    self.store._conversation_repo.upsert_conversation(
                        open_id, peer_name or title, "single",
                        peer_open_dingtalk_id=peer_id,
                    )
            
            merged = self._merge_consecutive_messages(
                conv_messages, window_seconds=self.config.merge_window_seconds
            )
            logger.debug("[轮询器] 已从 %s 合并得到 %d 条消息", title, len(merged))

            # 更新 _last_poll_time：用所有消息的最大时间戳（含已处理/自己发的）
            # 不能只用 conv_messages（未处理消息），否则全部已处理时 _last_poll_time 被重置为 now
            # 导致下一轮又拉到同一批消息，形成死循环
            if all_timestamps:
                max_ts = max(all_timestamps)
                # 飞书时间戳精度为分钟级，直接使用 max_ts 而非 +1s
                if type(self.dws).__name__ == 'FeishuCliAdapter':
                    self._last_poll_time[open_id] = max_ts
                else:
                    self._last_poll_time[open_id] = max_ts + timedelta(seconds=1)
                # 同步更新数据库（用 conv_messages 里最新消息的时间戳）
                if conv_messages:
                    try:
                        self.store._conversation_repo.upsert_conversation(
                            open_id, title, chat_type,
                            last_message_time=max_ts.isoformat()
                        )
                    except Exception as e:
                        logger.debug("[轮询器] 更新会话信息失败: %s", e)
                logger.debug("[轮询器] 更新 %s 的轮询时间点: %s",
                            title, self._last_poll_time[open_id])
            else:
                # 这条会话一条消息都没有，往前推配置的时间避免空转
                self._last_poll_time[open_id] = datetime.now() - timedelta(minutes=self.config.empty_poll_protection_minutes)

            new_messages.extend(merged)

        # === 全局去重：按 msg_id 去重 ===
        #   1. 同一轮次内 list-all / unread / top / DB 层的重复
        #   2. 跨轮次去重：之前已经 handle_message 处理过的消息不再返回
        seen_msg_ids: set[str] = set()
        deduped = []
        for msg in new_messages:
            if not msg.msg_id:
                continue
            # 跨轮次检查：之前已处理过？
            if self._is_msg_processed(msg.msg_id):
                logger.debug("[轮询器] 跨轮去重：消息 %s 已处理过", msg.msg_id)
                continue
            # 同轮次内检查
            if msg.msg_id in seen_msg_ids:
                logger.debug("[轮询器] 去重：丢弃重复消息 %s", msg.msg_id)
                continue
            seen_msg_ids.add(msg.msg_id)
            deduped.append(msg)
        if len(deduped) < len(new_messages):
            logger.debug("[轮询器] 去重完成：%d → %d，已去除 %d 条重复",
                        len(new_messages), len(deduped),
                        len(new_messages) - len(deduped))

        # 周期性统计（INFO 级）：直观确认接口请求优化生效
        if self._poll_count % 12 == 0:
            top_hit = getattr(self, "_top_cache_hit_flag", False)
            cli_label = _PLATFORM_CLI_LABEL.get(self.platform_id or "")
            cache_suffix = f"（减少 {cli_label} 调用）" if cli_label else ""
            logger.info(
                "[轮询器][%s] 轮询统计：本轮检查 %d 个会话，长尾限频跳过 %d 个抓取；"
                "置顶列表缓存=%s%s",
                self.platform_id or "?",
                len(all_conversations), throttled_skip,
                "命中" if top_hit else "刷新",
                cache_suffix,
            )

        return deduped
