"""轮询策略 Mixin — 主循环 / 间隔控制 / 会话发现 / 消息拉取。

从 poller.py 拆分出来，包含 poll_once 主循环及其相关联的辅助方法。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from src.dws_adapter import DwsPermissionError
from src.models import Message
from src.poller_mixins_base import PollerMixinBase
from src.poller_utils import match_notification_signature
from src.utils.security import mask_oid
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
            discovered = self.dws.sync_external_contacts()  # type: ignore[attr-defined]
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
            # P1-4: 单轮内按 conv_id 缓存 chat_conversation_info 结果，带 TTL 过期机制
            # 避免 _build_group_list_all_cache（遍历所有群）/ _fetch_conversation_messages
            # 对同一会话重复发起 subprocess CLI 调用。
            import time
            cache = getattr(self, "_feishu_conv_info_cache", None)
            cache_ttl = getattr(self, "_feishu_conv_info_cache_ttl", 300)  # 5 分钟 TTL
            if cache is None:
                cache = {}
                self._feishu_conv_info_cache = cache
            # 清理过期条目
            now = time.time()
            expired_keys = [k for k, (t, _) in cache.items() if now - t > cache_ttl]
            for k in expired_keys:
                del cache[k]
            _miss = object()
            cached = cache.get(conv_id, _miss)
            if cached is _miss:
                info = self.dws.chat_conversation_info(conv_id)
                cache[conv_id] = (now, info)
            else:
                info = cached[1]
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
        """已弃用：群消息改走用户级逐群接口 chat_message_list_group，不再依赖 list-all。

        旧的 list-all 批量预取（search_messages_by_time_range）对群聊返回业务错误
        （消息搜索权益不覆盖群聊），既拉不到群消息又每轮刷 warning。群消息现由
        ``_poll_one_conversation`` 逐群调用 ``chat_message_list_group`` 直接拉取，
        本方法保留为兼容桩、恒返回 None。
        """
        return None


    # ── poll_once 辅助方法（T5 拆分）──

    def _fetch_unread_conversations(self) -> list[dict]:
        """拉取未读会话列表（仅用于实时优先 forced_ids，不做已读不回复判定）。"""
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
        return unread_convs

    def _handle_list_all_fetch(self, handler) -> tuple[list, bool]:
        """通过 list-all 拉最近消息，执行快通道或直接扩展。

        Returns:
            (new_messages_from_list_all, success_flag)
        """
        result_msgs = []
        ok = False
        try:
            msgs = self._fetch_messages_via_list_all()
            ok = True
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
                _fd = 0
                for m in msgs:
                    try:
                        self._dispatch_one(m, handler)
                        _fd += 1
                    except Exception as _e:
                        logger.error("[轮询器] list-all 快通道派发失败（消息可能延迟）: %s", _e, exc_info=True)
                if _fd:
                    logger.info(
                        "[轮询器] list-all 快通道已即时派发 %d 条新消息（不等待 per-conversation 抓取）",
                        _fd,
                    )
            else:
                result_msgs.extend(msgs)
            logger.debug("[轮询器] list-all 直接返回了 %d 条新消息", len(msgs))
        except Exception as e:
            if self._is_global_permission_error(e):
                # 组织级权限问题：仅按 key 去重警告一次，不阻断后续其他取信通道
                self._warn_permission_once(
                    "global_token_list_all",
                    f"list-all 取信遇到全局权限错误（不影响其他通道）: {e}"
                )
                logger.debug("通过 list-all 获取消息失败（全局权限，已静默）: %s", e)
            else:
                logger.warning("通过 list-all 获取消息失败: %s", e)
        return result_msgs, ok

    def _record_empty_poll_probe(self, list_all_ok: bool, list_all_messages: list) -> None:
        """list-all 主通道空轮探针：连续 N 轮无消息则 DEBUG 级别提示。"""
        if self.config.list_all_empty_alert_rounds <= 0 or not list_all_ok:
            return
        if len(list_all_messages) == 0:
            self._list_all_empty_streak += 1
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

    def _gather_conversations(self, unread_convs: list[dict]) -> tuple[list[dict], set[str]]:
        """合并未读+置顶/最近+DB缓存+外部好友四个来源的会话，去重。

        Returns:
            (all_conversations, forced_ids) — forced_ids 永不参与长尾限频
        """
        seen = set()
        all_conversations = []
        forced_ids = set()

        # 1. 未读会话（实时发现新消息）
        try:
            for c in unread_convs:
                oid = c.get("openConversationId", "")
                if oid and oid not in seen and not self._is_blocked(oid):
                    seen.add(oid)
                    forced_ids.add(oid)
                    all_conversations.append(c)
            logger.debug("[轮询器] 发现 %d 个未读会话", len(unread_convs))
        except Exception as e:
            logger.warning("合并未读会话失败: %s", e)

        # 2. 置顶/最近会话列表（缓存化，极少变化）
        try:
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

        # 3. 数据库缓存的最近会话（兜底）
        try:
            db_convs = self._get_recent_conversations_from_db()
            for c in db_convs:
                oid = c.get("openConversationId", "")
                if oid and oid not in seen and not self._is_blocked(oid):
                    seen.add(oid)
                    all_conversations.append(c)
            logger.debug("[轮询器] + %d 个数据库缓存会话，总计 %d", len(db_convs), len(all_conversations))
        except Exception as e:
            logger.warning("获取数据库缓存会话失败: %s", e)

        # 4. 外部好友强制轮询
        try:
            external_friends = self.store._external_friend_repo.list_external_friends()
            for ef in external_friends:
                conv_id, conv_entry = self._resolve_external_friend_conv(ef, seen)
                if conv_id and conv_id not in seen and not self._is_blocked(conv_id):
                    seen.add(conv_id)
                    all_conversations.append(conv_entry)
            if external_friends:
                logger.debug("[轮询器] + %d 个外部好友（强制），总计 %d", len(external_friends), len(all_conversations))
        except Exception as e:
            logger.warning("列出外部好友失败：%s", e)

        # 5. 钉钉群枚举源（chat +chat-list-all / +chat-list-mine 补全群，list-all 搜索权益不覆盖群）
        try:
            groups = self._get_cached_joined_groups()
            for g in groups:
                oid = g.get("openConversationId", "")
                if oid and oid not in seen and not self._is_blocked(oid):
                    seen.add(oid)
                    all_conversations.append({
                        "openConversationId": oid,
                        "singleChat": False,   # 群聊：走 chat_message_list（list-all 按群过滤）
                        "title": g.get("name", ""),
                    })
            if groups:
                logger.debug("[轮询器] + %d 个群枚举（来自 dws 群列表），总计 %d", len(groups), len(all_conversations))
        except Exception as e:
            logger.warning("群枚举失败：%s", e)

        return all_conversations, forced_ids

    def _resolve_external_friend_conv(self, ef: dict, seen: set) -> tuple[str, dict | None]:
        """将外部好友记录解析为 oc_xxx 级别的会话入口。

        Returns:
            (real_conv_id, conv_dict_or_None)
        """
        oid = ef.get("open_dingtalk_id", "")
        if not oid or oid in seen:
            return "", None
        ef_name = ef.get("name", "")
        ef_chat_id = ef.get("chat_id", "")
        real_conv_id = ""
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
            return "", None
        return real_conv_id, {
            "openConversationId": real_conv_id,
            "singleChat": True,
            "title": ef_name,
        }

    def _get_cached_joined_groups(self) -> list[dict]:
        """获取钉钉群枚举（含「我加入 + 我创建」的群，TTL 缓存）。

        群列表极少变化，无需每轮(默认5s)都打 DWS 的 chat +chat-list-all。
        缓存有效期内直接返回内存副本；过期或首次才真正请求。请求失败则抛出，
        交由调用方的 try/except 处理。
        """
        ttl = getattr(self.config, "group_enum_cache_ttl_seconds", 600) or 600
        now = time.time()
        if self._group_enum_cache and (now - self._group_enum_cache_ts) < ttl:
            return self._group_enum_cache
        fresh = self._fetch_joined_groups()
        self._group_enum_cache = fresh or []
        self._group_enum_cache_ts = now
        return self._group_enum_cache

    def _fetch_joined_groups(self) -> list[dict]:
        """从 DWS 群列举命令拉取并合并「我加入 + 我创建」的群。

        仅钉钉适配器支持；非钉钉（飞书/企微）或不支持该方法的适配器返回空列表。
        """
        if getattr(self, "adapter_type", "") != "dingtalk":
            return []
        joined_fn = getattr(self.dws, "chat_list_groups_joined", None)
        mine_fn = getattr(self.dws, "chat_list_groups_mine", None)
        if joined_fn is None or mine_fn is None:
            return []
        try:
            joined = joined_fn()
        except Exception as e:
            logger.warning("[轮询器] 拉取已加入群列表失败: %s", e)
            joined = []
        try:
            mine = mine_fn()
        except Exception as e:
            logger.warning("[轮询器] 拉取自建群列表失败: %s", e)
            mine = []
        merged: dict[str, dict] = {}
        for g in (joined or []) + (mine or []):
            cid = g.get("openConversationId", "")
            if cid:
                merged[cid] = g
        return list(merged.values())

    def _compute_last_poll_time(self, open_id: str, now=None) -> tuple[datetime, bool]:
        """计算会话的上次轮询时间点。

        Returns:
            (last_poll_dt, is_first_poll)
        """
        if now is None:
            now = datetime.now()
        last_poll = self._last_poll_time.get(open_id, now - timedelta(hours=24))
        is_first_poll = open_id not in self._last_poll_time
        if is_first_poll:
            try:
                conv = self.store._conversation_repo.get_conversation(open_id)
                if conv and conv.get("last_message_time"):
                    last_poll = datetime.fromisoformat(conv["last_message_time"])
            except Exception as e:
                logger.debug("[轮询器] 数据库 last_message_time 解析失败: %s", e)
        return last_poll, is_first_poll

    def _store_self_message_if_new(self, msg) -> None:
        """保存自己发的消息到历史（双写去重保护）。DRY 后的统一入库逻辑。"""
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

    def _handle_fetch_errors(self, err: Exception, open_id: str, title: str,
                             is_single: bool, chat_type: str) -> bool:
        """处理消息拉取阶段的各种异常；return True 表示应 continue 跳过该会话。"""
        if self._is_permission_error(err):
            if is_single:
                err_str = str(err).lower()
                if any(kw in err_str for kw in ("cross app", "different tenants",
                                                "out of the chat", "can not be out")):
                    self._block_conversation(open_id, title, chat_type, err, source="runtime_error")
                else:
                    logger.debug(
                        "[轮询器] 单聊补拉无权限（外部好友/已无会话，跳过补拉，"
                        "不影响 list-all 收发）: %s | %s", title or open_id[:20], err
                    )
            else:
                err_str = str(err).lower()
                if any(kw in err_str for kw in ("cross app", "different tenants",
                                                "out of the chat", "can not be out")):
                    self._block_conversation(open_id, title, chat_type, err, source="runtime_error")
                else:
                    should_block, streak = self._register_perm_failure(open_id)
                    if should_block:
                        self._block_conversation(open_id, title, chat_type, err, source="runtime_error")
                    else:
                        logger.warning(
                            "[轮询器] 群 %s 第 %d 次权限错误（疑似瞬时，暂不拉黑，下轮重试）: %s",
                            title or open_id[:20], streak, err
                        )
        elif self._is_global_permission_error(err):
            self._warn_permission_once(
                f"global_perm_{open_id[:24]}",
                f"全局/组织级权限错误，跳过本轮该会话"
                f"（不拉黑、不删行，下轮重试）: {title or open_id[:20]} | {err}"
            )
        else:
            err_str = str(err)
            if "openCid or cid is required" in err_str:
                logger.debug("列出 %s 的消息失败(已降级): %s", title or open_id[:20], err)
            else:
                logger.warning("列出 %s 的消息失败: %s", title or open_id[:20], err)
        return True

    def _poll_one_conversation(self, conv: dict, group_cache, forced_ids: set[str]) -> tuple | None:
        """对单个会话执行消息拉取、过滤、合并、时间更新完整流程。

        Returns:
            (merged_msgs, all_timestamps, skipped_throttle_bool) or None on skip/error
        """
        open_id = conv.get("openConversationId", "")
        if not open_id:
            return None

        # 会话级 chat_id 前缀校验：飞书是 oc_，钉钉是 cid 前缀
        if not (str(open_id).startswith("oc_") or str(open_id).startswith("cid")):
            logger.debug("[轮询器] 跳过非法 chat_id（需 oc_/cid* 前缀）: %s", open_id[:24])
            return None

        # 跳过本次运行内已确认无权限的会话
        if open_id in self._inaccessible_conversations:
            logger.debug("[轮询器] 跳过无权限会话: %s", open_id[:30])
            return None

        chat_type = self._detect_chat_type(conv)
        title = conv.get("title", "")
        # 飞书自动纠错：以 API chat_mode 为准修正 DB
        chat_type = self._feishu_correct_chat_type(open_id, title, chat_type)
        is_single = chat_type == "single"

        # 规则引擎黑名单：配置级跳过
        if self._is_blacklisted_conversation(title, chat_type):
            logger.debug("[轮询器] 跳过黑名单会话: %s（类型=%s）",
                         title or open_id[:20], chat_type)
            return None

        # 长尾限频
        skipped_throttle = self._should_skip_longtail_fetch(open_id, open_id in forced_ids)
        if skipped_throttle:
            logger.debug("[轮询器] 会话 %s 限频跳过本轮抓取", title or open_id[:20])
            return None

        # 保存/更新会话缓存
        self.store._conversation_repo.upsert_conversation(open_id, title, chat_type)

        # 系统/第三方应用会话（other类型）：跳过直接拉取
        if chat_type == "other":
            logger.debug("[轮询器] 跳过系统/应用会话 %s（类型=other，消息通过 list-all 获取）", title or open_id[:20])
            return None

        last_poll, is_first_poll = self._compute_last_poll_time(open_id)
        time_str = last_poll.strftime("%Y-%m-%d %H:%M:%S")

        # 工作通知会话（如「工作通知:XX」）已在群枚举阶段作为 cid 会话纳入遍历，
        # 走 chat_message_list（list-all，user API）拉取；失败由 _handle_fetch_errors
        # 兜底（瞬时重试 / 拉黑自愈），不再在此硬跳过（原 list-direct 单聊权限错误不适用群路径）。

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
                    return None

                raw_msgs = self.dws.chat_message_list_direct(
                    user_id=peer_uid,
                    open_dingtalk_id=peer_oid,
                    time_str=time_str,
                    limit=self.config.messages_per_conversation,
                )
            else:
                # === 群聊：chat_message_list 现走用户级逐群接口（绕过 list-all 群消息搜索权益限制）===
                raw_msgs = self.dws.chat_message_list(
                    open_id, time_str, self.config.messages_per_conversation,
                )
            logger.debug("[轮询器] 从 %s（类型=%s）获取了 %d 条原始消息",
                        title, chat_type, len(raw_msgs))
            # 拉取成功：清除该会话的连续权限失败计数
            self._perm_fail_streak.pop(open_id, None)
        except Exception as e:
            self._handle_fetch_errors(e, open_id, title, is_single, chat_type)
            return None

        # 如果是单聊，从消息里提取对方 openDingTalkId 并更新会话缓存
        if is_single and raw_msgs:
            peer = self._resolve_single_chat_peer(open_id, title)
            peer_oid_from_msgs = ""
            for raw_msg in raw_msgs:
                candidate_oid = raw_msg.get("senderOpenDingTalkId") or raw_msg.get("senderId") or ""
                if candidate_oid and not self._is_self_sender(candidate_oid):
                    peer_oid_from_msgs = candidate_oid
                    break
            if peer_oid_from_msgs:
                logger.debug("[轮询器] 正在更新 %s 的对方信息：openDingTalkId=%s", mask_oid(open_id), mask_oid(peer_oid_from_msgs))
                self.store._conversation_repo.upsert_conversation(
                    open_id, title, "single",
                    peer_open_dingtalk_id=peer_oid_from_msgs
                )

        # 处理消息：过滤 + 转换 + 合并
        merged, all_timestamps = self._process_conv_messages(
            raw_msgs, open_id, chat_type, title, is_single, peer if is_single else None, is_first_poll
        )

        # 更新 _last_poll_time 及 DB
        self._update_poll_time_and_db(open_id, title, chat_type, all_timestamps, merged)

        return merged, all_timestamps, skipped_throttle

    def _process_conv_messages(self, raw_msgs, open_id: str, chat_type: str,
                               title: str, is_single: bool, peer,
                               is_first_poll: bool) -> tuple[list, list]:
        """处理单个会话的原始消息列表 → 过滤 + 转换 + 合并。

        Returns:
            (merged_messages, all_timestamps)
        """
        all_timestamps: list[datetime] = []
        conv_messages = []

        for raw in raw_msgs:
            # 主路径先去重，避免已处理消息反复触发图片下载/OCR
            raw_id = raw.get("openMessageId") or raw.get("msgId") or ""
            if raw_id and self.store._message_repo.is_message_processed(raw_id):
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

            # 【强制过滤】最早入口拦截自己发的消息
            if msg.sender_id and self._is_self_sender(msg.sender_id):
                self._store_self_message_if_new(msg)
                logger.debug("[轮询器] 强制过滤：丢弃自己发的消息（%s，类型=%s，已入库）：%s",
                             msg.sender_name, msg.msg_type, (msg.content or "")[:30])
                continue

            # msg_id 为空时用备选 ID 避免反复拉取
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

            # 【首次运行忽略老消息】
            if is_first_poll and self.config.first_run_ignore_older_than_minutes > 0 and msg.timestamp:
                age_minutes = (datetime.now() - msg.timestamp).total_seconds() / 60
                if age_minutes > self.config.first_run_ignore_older_than_minutes:
                    logger.debug("[轮询器] 首次运行忽略 %d 分钟前的老消息（来自 %s，时间=%s）",
                                 int(age_minutes), msg.sender_name, msg.timestamp)
                    continue

            # 【无条件年龄门槛】超过 history_days 的远古消息不触发 AI 回复。
            # 即使去重表被清空（重启/维护）导致 DWS 重新拉到老消息，
            # 也不应把一个月前的「好的」当作当前对话处理（2026-08 线上事故）。
            # 首次运行的 first_run_ignore 已在上游处理过；本检查覆盖所有后续轮次。
            history_days = self.config.history_days
            if history_days > 0 and msg.timestamp:
                age_days = (datetime.now() - msg.timestamp).total_seconds() / 86400
                if age_days > history_days:
                    logger.info(
                        "[轮询器] 跳过 %.1f 天前的远古消息（>%d 天阈值，"
                        "来自 %s，时间=%s，内容=%s）",
                        age_days, history_days,
                        msg.sender_name, msg.timestamp,
                        (msg.content or "")[:40],
                    )
                    continue

            if self._is_self_message(msg):
                self._store_self_message_if_new(msg)
                is_bot = msg.is_bot if hasattr(msg, 'is_bot') else False
                logger.debug("[轮询器] 跳过自己发的消息（%s，%s）：%s",
                             msg.sender_name, "AI代发" if is_bot else "真人", msg.content[:30])
                continue

            # 图片消息：未启用 OCR 时跳过
            if msg.msg_type == "image" and not self.config.image_ocr_enabled:
                logger.debug("[轮询器] 跳过图片消息（OCR 未启用，来自 %s）", msg.sender_name)
                continue

            # 跳过系统/自动消息
            if msg.msg_type in self._effective_skip_types():
                logger.debug("[轮询器] 跳过 %s 类型消息（%s）：%s",
                             msg.msg_type, msg.sender_name, msg.content[:40])
                continue

            # 窄签名层：仅拦截「以真人身份推送的纯文本机器通知」
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

        logger.debug("[轮询器] 在 %s 中过滤得到 %d 条新消息", title, len(conv_messages))

        # 从消息里的对方信息反写会话缓存（确保下次轮询时 peer 信息已有）
        if conv_messages and is_single and peer:
            peer_id = conv_messages[0].sender_id
            peer_name = conv_messages[0].sender_name
            if peer_id and (not peer.get("open_dingtalk_id") and not peer.get("user_id")):
                logger.debug("[轮询器] 从消息中更新对方信息：%s → %s", mask_oid(open_id), mask_oid(peer_id))
                self.store._conversation_repo.upsert_conversation(
                    open_id, peer_name or title, "single",
                    peer_open_dingtalk_id=peer_id,
                )

        merged = self._merge_consecutive_messages(
            conv_messages, window_seconds=self.config.merge_window_seconds
        )
        logger.debug("[轮询器] 已从 %s 合并得到 %d 条消息", title, len(merged))
        return merged, all_timestamps

    def _update_poll_time_and_db(self, open_id: str, title: str, chat_type: str,
                                  all_timestamps: list, conv_messages: list) -> None:
        """更新 _last_poll_time（基于所有消息的最大时间戳）及 DB。"""
        if all_timestamps:
            max_ts = max(all_timestamps)
            # 飞书时间戳精度为分钟级，直接使用 max_ts 而非 +1s
            if type(self.dws).__name__ == 'FeishuCliAdapter':
                self._last_poll_time[open_id] = max_ts
            else:
                self._last_poll_time[open_id] = max_ts + timedelta(seconds=1)
            # 同步更新数据库
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

    def _global_deduplicate(self, messages: list) -> list:
        """全局去重：同轮次内 + 跨轮次已处理消息。"""
        seen_ids: set[str] = set()
        deduped = []
        for msg in messages:
            if not msg.msg_id:
                continue
            # 跨轮次检查
            if self._is_msg_processed(msg.msg_id):
                logger.debug("[轮询器] 跨轮去重：消息 %s 已处理过", msg.msg_id)
                continue
            # 同轮次内检查
            if msg.msg_id in seen_ids:
                logger.debug("[轮询器] 去重：丢弃重复消息 %s", msg.msg_id)
                continue
            seen_ids.add(msg.msg_id)
            deduped.append(msg)
        removed = len(messages) - len(deduped)
        if removed:
            logger.debug("[轮询器] 去重完成：%d → %d，已去除 %d 条重复",
                         len(messages), len(deduped), removed)
        return deduped

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
        # H3-2026-08-08：清空上一轮的飞书会话信息缓存，使本轮按 conv_id 共享一次 CLI 结果
        self._feishu_conv_info_cache = {}
        logger.debug("[轮询器] poll_once() 已启动")

        # 每 N 轮用 list-top（安全、不弹窗）对账一次黑名单，自动解除已恢复访问的会话
        self._poll_count += 1
        if self._poll_count % self._reconcile_every == 0:
            try:
                self._reconcile_blocklist()
            except Exception as e:
                logger.warning("[轮询器] 周期对账黑名单失败: %s", e)

        # === 1. 拉取未读会话列表（仅用于实时优先强制轮询 forced_ids）===
        unread_convs = self._fetch_unread_conversations()

        # === 2. list-all 主通道拉消息（含外部好友）===
        la_msgs, la_ok = self._handle_list_all_fetch(handler)
        new_messages.extend(la_msgs)

        # === 3. list-all 空轮探针 ===
        self._record_empty_poll_probe(la_ok, la_msgs)

        # === 4. 合并多源会话列表 ===
        all_conversations, forced_ids = self._gather_conversations(unread_convs)

        if not all_conversations:
            logger.debug("[轮询器] 没有会话需要检查")
            # 不提前返回：即使无会话也继续走到底，使周期性统计（每 12 轮）
            # 对空平台（如尚未有会话的 wecom）也可见，满足「各平台都要有」。

        # === 5. 群消息批量预取 ===
        group_cache = self._build_group_list_all_cache(all_conversations)

        throttled_skip = 0
        for conv in all_conversations:
            result = self._poll_one_conversation(conv, group_cache, forced_ids)
            if result is None:
                continue
            merged_msgs, _, skipped = result
            new_messages.extend(merged_msgs)
            if skipped:
                throttled_skip += 1

        # === 6. 全局去重 ===
        deduped = self._global_deduplicate(new_messages)

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
