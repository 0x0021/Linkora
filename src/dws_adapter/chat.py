"""DwsAdapter 聊天能力 mixin（会话/消息读写/发送/引用回复）。拆分自 dws_adapter.py。"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from src.dws_adapter.core import DwsError
from src.im_adapter.markdown_fix import normalize_markdown_for_platform
from src.im_adapter.message_format import classify_message_format

logger = logging.getLogger(__name__)


class DwsAdapterChatMixin:
    def contact_user_get_self(self, timeout: int | None = None) -> dict:
        try:
            data = self.run(["contact", "user", "get-self"],
                          operation="contact_user_get_self", force_no_dry_run=True,
                          timeout=timeout or self.timeout)
        except DwsError as e:
            if self._is_personal_dingtalk_error(str(e)):
                logger.warning("个人钉钉模式：contact_user_get_self 不可用，跳过")
                return {}
            raise
        result = self._get_result(data)
        if isinstance(result, list) and result:
            return result[0]
        return result if isinstance(result, dict) else {}

    def chat_message_list_unread_conversations(self, count: int = 20) -> list[dict]:
        data = self.run([
            "chat", "message", "list-unread-conversations",
            "--count", str(count)
        ], operation="chat_message_list_unread_conversations", force_no_dry_run=True)
        result = self._get_result(data)
        if isinstance(result, dict):
            return result.get("conversations", [])
        return []

    def chat_list_top_conversations(self, limit: int = 50) -> list[dict]:
        """获取最近会话列表（含单聊/群聊，不依赖未读标记）。"""
        data = self.run([
            "chat", "list-top-conversations",
            "--limit", str(limit)
        ], operation="chat_list_top_conversations", force_no_dry_run=True)
        result = self._get_result(data)
        if isinstance(result, dict):
            return result.get("conversations", [])
        return []

    def chat_message_list_direct(self, user_id: str = "",
                                 open_dingtalk_id: str = "",
                                 time_str: str = "",
                                 limit: int = 50) -> list[dict]:
        """拉取单聊消息。forward=false 表示按时间正序返回（老→新）。"""
        args = ["chat", "message", "list-direct",
                "--time", time_str,
                "--limit", str(limit),
                "--forward=false"]
        if open_dingtalk_id:
            args.extend(["--open-dingtalk-id", open_dingtalk_id])
        elif user_id:
            args.extend(["--user", user_id])
        else:
            raise ValueError("Either user_id or open_dingtalk_id is required")
        data = self.run(args, operation="chat_message_list_direct", force_no_dry_run=True)
        result = self._get_result(data)
        if isinstance(result, dict):
            return result.get("messages", [])
        return []

    def chat_message_list(self, group: str, time_str: str,
                          limit: int = 50,
                          cached_result: dict | None = None,
                          timeout: int | None = None) -> list[dict]:
        """拉取指定群聊的消息（按时间正序）。

        ⚠️ 关键修正：dws 的 ``chat message list`` 底层是 ``list_conversation_message_v2``，
        这是**面向机器人(群机器人)**的接口，要求 bot 在群内才能调用；但 dws 以
        **用户本人身份**运行，群成员本人调 v2 会收到 ``AUTH_PERMISSION_DENIED``
        （与"用户是否在群里"无关）。因此这里改用面向用户本人的 ``chat message list-all``
        （``search_messages_by_time_range``），按 openConversationId 过滤出该群消息，
        无需建机器人、且尊重用户自身的群成员权限。底层 API 映射已用 ``dws --dry-run`` 验证：
        ``chat message list`` → ``list_conversation_message_v2``（bot API，拒）；
        ``chat message list-all`` → ``search_messages_by_time_range``（user API，通过）。

        ⚡ 性能优化（cached_result 快路径）：poller 主循环对 N 个活跃群原本会各自
        独立跑一次整窗 ``list-all`` 全扫再按群过滤（N 次全量扫描）。改为由 poller 先按
        所有活跃群的**并集时间窗**只扫一次，把合并字典通过 ``cached_result`` 传入，这里
        直接内存过滤、零额外 API 调用。``cached_result`` 为 None（自愈探针 / 工具单次调用
        / 单测）时走原 fallback：自己跑一次 ``chat_message_list_all``。
        """
        if cached_result is not None and isinstance(cached_result, dict):
            # 快路径：从预取的 list-all 合并字典按 openConversationId 过滤。
            # cached_result 覆盖 [min_time_str, now] ⊇ 本群的 [time_str, now]，
            # 故过滤结果包含本群在 time_str 之后的全部消息（更早的消息由
            # is_message_processed 去重兜底，不会重复处理）。
            for conv in cached_result.get("conversationMessagesList", []) or []:
                if conv.get("openConversationId") == group:
                    return conv.get("messages", []) or []
            return []

        # fallback：自行整窗扫描（保留给非批量调用点）。
        end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        merged = self.chat_message_list_all(time_str, end, limit=limit, timeout=timeout)
        if not isinstance(merged, dict):
            return []
        for conv in merged.get("conversationMessagesList", []) or []:
            if conv.get("openConversationId") == group:
                return conv.get("messages", []) or []
        return []

    def chat_message_list_all(self, start: str, end: str,
                              limit: int = 50, timeout: int | None = None,
                              extra_chat_ids: list[str] | None = None,
                              chat_ids: list[str] | None = None,
                              chat_meta: dict[str, dict] | None = None,
                              max_pages: int | None = None,
                              window_days: int = 7) -> dict:
        """按时间范围拉取所有消息（单聊+群聊，不需要 openConversationId）。

        内部自动处理 hasMore/nextCursor 分页（最多 effective_max_pages 页），把各页的
        conversationMessagesList 按 openConversationId 聚合、消息按 openMessageId 去重后合并返回。

        为规避「大时间窗 + 活跃群」下单窗翻页触顶上限（默认 50 页）导致漏消息 +
        刷告警，当 (end-start) 跨度超过 window_days 时，自动把时间窗切成多个不重叠的
        子窗口分别拉取并合并（各子窗独立翻页、互不污染；跨窗按 openMessageId 去重）。
        单窗跨度 <= window_days 时直接整窗拉取，行为与原实现一致。

        返回合并后的 result dict，由调用方决定如何处理。
        """
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # 时间格式异常：退回整窗逻辑（时间解析由 dws CLI 决定成败）。
            return self._chat_message_list_all_single(start, end, limit, timeout, max_pages)
        span_days = (end_dt - start_dt).days
        if window_days and window_days > 0 and span_days > window_days:
            return self._chat_message_list_all_windowed(
                start_dt, end_dt, limit, timeout, max_pages, window_days)
        return self._chat_message_list_all_single(start, end, limit, timeout, max_pages)

    def _chat_message_list_all_windowed(self, start_dt: datetime, end_dt: datetime,
                                        limit: int, timeout: int | None,
                                        max_pages: int | None, window_days: int) -> dict:
        """大时间窗分窗拉取：按 window_days 切片，各子窗独立翻页后合并。"""
        merged: dict = {"conversationMessagesList": [], "hasMore": False, "nextCursor": ""}
        cursor_dt = start_dt
        while cursor_dt < end_dt:
            next_dt = min(cursor_dt + timedelta(days=window_days), end_dt)
            sub = self._chat_message_list_all_single(
                cursor_dt.strftime("%Y-%m-%d %H:%M:%S"),
                next_dt.strftime("%Y-%m-%d %H:%M:%S"),
                limit, timeout, max_pages,
            )
            self._merge_list_all_conversations(
                merged, sub.get("conversationMessagesList", []) or [])
            # 以最后一个子窗的翻页状态作为整体翻页状态
            merged["hasMore"] = sub.get("hasMore", False)
            merged["nextCursor"] = sub.get("nextCursor", "")
            cursor_dt = next_dt
        return merged

    def _chat_message_list_all_single(self, start: str, end: str, limit: int,
                                      timeout: int | None, max_pages: int | None) -> dict:
        """单窗口 list-all 翻页拉取（不分子窗）。"""
        try:
            cursor = "0"
            # 安全上限，避免极端情况下死循环。原硬编码 20，现可配（默认 50）：
            # 在宽时间窗 + 活跃群下 20 极易触顶导致漏消息。0 = 不限制（谨慎使用）。
            effective_max_pages = max_pages if (max_pages and max_pages > 0) else 50
            merged: dict = {
                "conversationMessagesList": [],
                "hasMore": False,
                "nextCursor": "",
            }
            pages = 0
            while True:
                pages += 1
                data = self.run([
                    "chat", "message", "list-all",
                    "--start", start,
                    "--end", end,
                    "--limit", str(limit),
                    "--cursor", cursor,
                ], operation="chat_message_list_all", force_no_dry_run=True,
                   timeout=timeout or self.timeout)
                result = self._get_result(data)
                if not isinstance(result, dict):
                    break
                convs = result.get("conversationMessagesList", []) or []
                self._merge_list_all_conversations(merged, convs)
                next_cursor = result.get("nextCursor", "") or ""
                has_more = bool(result.get("hasMore", False))
                merged["hasMore"] = has_more
                merged["nextCursor"] = next_cursor
                if not has_more or not next_cursor or pages >= effective_max_pages:
                    if pages >= effective_max_pages and has_more:
                        # 同窗口 5 分钟内只告警一次，避免每轮轮询刷屏（实际仍会停止翻页）。
                        now_ts = time.time()
                        last = self.__dict__.get("_list_all_cap_warn_at", 0.0)
                        if now_ts - last >= 300:
                            logger.warning(
                                "list-all 分页达到上限 %d 页（时间窗 %s~%s），"
                                "停止翻页，窗口内可能仍有未拉取消息",
                                effective_max_pages, start, end
                            )
                            self._list_all_cap_warn_at = now_ts
                    break
                cursor = next_cursor
            return merged
        except DwsError as e:
            if "TOKEN_VERIFIED_FAILED" in str(e) or "该组织尚未开启 CLI 数据访问权限" in str(e):
                if "list-all" not in self._perm_warned:
                    self._perm_warned.add("list-all")
                    logger.warning("list-all 无权限访问 group-chat 接口，已静默，后续不再提示")
                return {"conversationMessagesList": [], "hasMore": False, "nextCursor": ""}
            raise

    @staticmethod
    def _merge_list_all_conversations(merged: dict, convs: list[dict]) -> None:
        """把一页的会话消息合并进 merged（按 openConversationId 聚合，消息按 openMessageId 去重）。"""
        index: dict[str, dict] = {
            c.get("openConversationId", ""): c
            for c in merged.get("conversationMessagesList", [])
            if c.get("openConversationId")
        }
        for conv in convs:
            cid = conv.get("openConversationId", "")
            if not cid:
                continue
            msgs = conv.get("messages", []) or []
            if cid not in index:
                merged["conversationMessagesList"].append(conv)
                index[cid] = conv
            else:
                existing = index[cid]
                seen_ids = {
                    m.get("openMessageId")
                    for m in existing.get("messages", [])
                    if m.get("openMessageId")
                }
                for m in msgs:
                    mid = m.get("openMessageId")
                    if mid and mid in seen_ids:
                        continue
                    existing.setdefault("messages", []).append(m)
                    if mid:
                        seen_ids.add(mid)

    def _infer_single_chat(self, chat: dict) -> bool:
        """判断会话是否为单聊。

        钉钉 ``dws chat conversation-info`` 返回的 chat 中包含：
        - ``conversationType``: 1 为单聊，2 为群聊
        - ``singleChat`` 字段（直接标记）
        """
        if not isinstance(chat, dict):
            return False
        if chat.get("singleChat") is True:
            return True
        ct = chat.get("conversationType")
        if isinstance(ct, int):
            return ct == 1
        if isinstance(ct, str) and ct.isdigit():
            return int(ct) == 1
        return False

    def chat_message_send(self, *, group: str | None = None,
                          user: str | None = None,
                          open_dingtalk_id: str | None = None,
                          title: str = "", text: str = "",
                          uuid: str | None = None,
                          ai_tag: bool | None = None,
                          msg_type: str | None = None,
                          media_id: str | None = None,
                          file_path: str | None = None,
                          at_all: bool = False,
                          at_open_dingtalk_ids: str | None = None) -> dict:
        """发送消息，支持多种格式。

        msg_type:
          - None / "auto": 自动判定（默认）。按内容结构分类为 text / markdown，
            结构化内容按平台能力做 markdown 归一化（如表格转等宽网格）。
          - "text": 纯文本，--text 传内容，不做 markdown 归一化。
          - "markdown": 结构化内容，--text 传内容，发送前按平台能力做归一化。
          - "image": 图片，需 media_id（或传 file_path 由本方法自动上传后取得）
          - "file" / "audio" / "video": 本地文件，传 file_path，CLI 自动上传发送
        at_all / at_open_dingtalk_ids 仅群聊生效，且内容需含对应 <@all> / <@id> 占位符
          （本方法会在缺失时自动补入 text，避免 dws 校验失败）。
        """
        if ai_tag is None:
            ai_tag = self.ai_tag_default

        # 目标
        if group:
            target = ["--group", group]
        elif user:
            target = ["--user", user]
        elif open_dingtalk_id:
            target = ["--open-dingtalk-id", open_dingtalk_id]
        else:
            raise ValueError("group, user, or open_dingtalk_id must be specified")

        args = ["chat", "message", "send"] + target

        # @ 占位符：at_all 由 dws 自动补入内容（atAll=true 时），无需手动注入；
        # at_open_dingtalk_ids 则需内容含 <@id> 占位符（dws 不自动注入），这里补入。
        if at_open_dingtalk_ids:
            for oid in [x.strip() for x in at_open_dingtalk_ids.split(",") if x.strip()]:
                ph = f"<@{oid}>"
                if ph not in text:
                    text = f"{text} {ph}".strip()

        # 文本类格式：auto 时按内容结构自动判定 text / markdown
        # （dws 用户态发送底层恒为 markdown msgType，无独立 text 类型；分类驱动
        #  的是「是否按平台能力做 markdown 归一化」与可观测日志，详见 message_format.py）
        mt = (msg_type or "auto").lower()
        if mt == "image":
            if not media_id and file_path:
                media_id = self.media_upload(file_path, media_type="image")
            if not media_id:
                raise ValueError("msg_type=image 需要 media_id（或先上传取得）")
            args.extend(["--msg-type", "image", "--media-id", media_id])
            if text:
                if classify_message_format(text) == "markdown" and not self.supports_markdown_tables:
                    text = normalize_markdown_for_platform(text, supports_tables=False)
                args.extend(["--text", text])
        elif mt in ("file", "audio", "video"):
            if not file_path:
                raise ValueError(f"msg_type={mt} 需要 file_path（本地文件路径）")
            args.extend(["--msg-type", mt, "--file-path", file_path])
            if text:
                if classify_message_format(text) == "markdown" and not self.supports_markdown_tables:
                    text = normalize_markdown_for_platform(text, supports_tables=False)
                args.extend(["--text", text])
        else:  # text / markdown / auto
            fmt = mt if mt in ("text", "markdown") else classify_message_format(text)
            if not text and not media_id:
                raise ValueError("text 消息需要 text 内容")
            # 仅 markdown 格式按平台能力做表格归一化；text 保持原样
            if fmt == "markdown" and not self.supports_markdown_tables:
                text = normalize_markdown_for_platform(text, supports_tables=False)
            args.extend(["--text", text])
            logger.debug("[DWS] 发送格式判定: %s (requested=%r)", fmt, msg_type)

        if title:
            args.extend(["--title", title])
        if at_all:
            args.append("--at-all")
        if at_open_dingtalk_ids:
            args.extend(["--at-open-dingtalk-ids", at_open_dingtalk_ids])
        if uuid:
            args.extend(["--uuid", uuid])
        if ai_tag:
            args.append("--ai-tag")

        # 脱敏 --text 参数值，避免日志泄露消息内容
        _masked_args = list(args)
        for _i, _a in enumerate(_masked_args):
            if _a == "--text" and _i + 1 < len(_masked_args):
                _v = _masked_args[_i + 1]
                _masked_args[_i + 1] = (_v[:20] + "...") if len(_v) > 20 else _v
        logger.debug("[DWS] 执行命令: dws %s", " ".join(_masked_args))
        return self.run(args)

    def chat_message_update(self, *, message_id: str, text: str = "",
                           title: str = "", group: str | None = None,
                           user: str | None = None) -> dict:
        """更新已发送的消息内容（用于流式输出：先占位再逐步 patch）。

        封装 `dws chat message update --msg-id xxx --text xxx`。
        """
        args = ["chat", "message", "update", "--msg-id", message_id]
        if text:
            # 钉钉不渲染 markdown 表格：仅 markdown 格式发送前转换（流式更新同理）
            if classify_message_format(text) == "markdown" and not self.supports_markdown_tables:
                text = normalize_markdown_for_platform(text, supports_tables=False)
            args.extend(["--text", text])
        if title:
            args.extend(["--title", title])
        if group:
            args.extend(["--group", group])
        if user:
            args.extend(["--user", user])
        logger.debug("[DWS] 更新消息: dws %s", " ".join(args))
        return self.run(args)

    def chat_message_reply(self, *, message_id: str | None = None, text: str = "",
                           title: str = "", uuid: str | None = None,
                           reply_in_thread: bool = False,
                           group: str | None = None,
                           user: str | None = None,
                           open_dingtalk_id: str | None = None,
                           msg_type: str | None = None,
                           media_id: str | None = None,
                           file_path: str | None = None,
                           ref_msg_id: str | None = None,
                           ref_sender: str | None = None,
                           conversation_id: str | None = None,
                           use_native_reply: bool | None = None,
                           fallback_to_send: bool = True) -> dict:
        """回复消息。

        原生引用回复：当提供被引用消息的 ``ref_msg_id``（openMessageId）、其发送者
        ``ref_sender``（openDingTalkId）与会话 ``conversation_id``（openConversationId）
        三者齐全、且非富媒体时，调用 dws 原生的 ``chat message reply``，在钉钉内展示
        「引用气泡」（用户能直观看到 AI 在回复哪条消息）。该接口以**用户本人身份**发送，
        与本项目一致，无需建机器人。

        原生回复失败时的降级策略由 ``fallback_to_send`` 控制：
        - ``True``（默认，供独立调用 / 工具）：降级为普通 chat_message_send，保证回复
          永不丢失；
        - ``False``（供 runtime 单聊路径）：改抛异常，让调用方走自己的 peer 感知分片
          发送分支，避免「原生失败 + 内部发送」造成重复发送。
        （reply_in_thread 钉钉不支持，已忽略。）
        """
        # 目标：普通发送用 group/user/open_dingtalk_id；原生引用回复用 conversation_id。
        target = group or user or open_dingtalk_id or conversation_id
        if not target:
            raise ValueError(
                "dws chat_message_reply 需提供 group / user / open_dingtalk_id 之一"
                "（或原生引用回复的 conversation_id）"
            )
        if reply_in_thread:
            logger.warning("dws 不支持 reply_in_thread，已忽略")
        content = text or title
        if not content:
            raise ValueError("chat_message_reply 需提供 text 或 title")

        # 是否走原生引用回复：默认自动——三者齐全且非富媒体时启用
        if use_native_reply is None:
            use_native_reply = bool(
                ref_msg_id and ref_sender and conversation_id
                and not (msg_type or media_id or file_path)
            )
        if use_native_reply and ref_msg_id and ref_sender and conversation_id:
            try:
                # 原生引用回复同样按格式归一化 markdown（与 fallback 的 send 一致）
                _reply_text = text
                if classify_message_format(text) == "markdown" and not self.supports_markdown_tables:
                    _reply_text = normalize_markdown_for_platform(text, supports_tables=False)
                args = [
                    "chat", "message", "reply",
                    "--conversation-id", conversation_id,
                    "--ref-msg-id", ref_msg_id,
                    "--ref-sender", ref_sender,
                    "--text", _reply_text,
                ]
                # 注意：dws chat message reply 不支持 --title（help 的 available_flags
                # 无此 flag），标题仅在 fallback 的 chat_message_send 中使用。
                if uuid:
                    args.extend(["--uuid", uuid])
                if self.ai_tag_default:
                    args.append("--ai-tag")
                logger.debug("[DWS] reply via native quote-reply (ref=%s)", ref_msg_id[:20])
                return self.run(args)
            except Exception as e:
                if not fallback_to_send:
                    # 调用方自己有 peer 感知的分片发送分支，交还控制权避免重复发送
                    raise
                logger.warning(
                    "[DWS] 原生引用回复失败，降级为普通发送: %s", e
                )
                # 落到下方普通发送分支

        logger.debug("[DWS] reply (via send) to message_id=%s", message_id)
        return self.chat_message_send(
            group=group, user=user, open_dingtalk_id=open_dingtalk_id,
            title=title, text=text, uuid=uuid,
            msg_type=msg_type, media_id=media_id, file_path=file_path,
        )
