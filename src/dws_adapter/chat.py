"""DwsAdapter 聊天能力 mixin（会话/消息读写/发送/引用回复）。拆分自 dws_adapter.py。"""
from __future__ import annotations
from .dws_mixins_base import DwsAdapterBase

import logging
import time
from datetime import datetime, timedelta

from src.dws_adapter.core import DwsError
from src.im_adapter.markdown_fix import normalize_markdown_for_platform
from src.im_adapter.message_format import classify_message_format

logger = logging.getLogger(__name__)


class DwsAdapterChatMixin(DwsAdapterBase):
    # 类级冷却表：key = "start|end" 时间窗，value = 上次打印上限提示的 unix timestamp。
    # 用类级而非实例级，避免多实例/多线程场景下 5 分钟冷却失效导致刷屏。
    _list_all_cap_warn_at: dict[str, float] = {}

    # ``chat message list`` 自动翻页上限（页数；每页 ``--limit`` 条，故总量上限为
    # limit × 本值，默认 20 × 10 = 200 条）。见 chat_message_list_group 的分页说明。
    _MESSAGE_LIST_MAX_PAGES = 10

    # ``chat list-top-conversations`` 翻页上限（页数，每页默认 1000 条）。
    # 置顶会话正常远少于一页，本值只作死循环兜底。
    _TOP_CONVERSATIONS_MAX_PAGES = 5

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

    def chat_list_top_conversations(self, limit: int = 1000) -> list[dict]:
        """获取**置顶会话**列表（含单聊/群聊，不依赖未读标记）。

        ⚠️ 语义是「置顶会话」（dws 原话：拉取当前用户的置顶会话列表），**不是**最近
        会话列表——旧 docstring 描述为「最近会话」有歧义，已更正。

        ⚠️ 必须翻页：dws ``--limit`` 默认 1000 且支持 ``--cursor`` / ``nextCursor``。
        旧实现硬编码单页 ``--limit 100`` 且忽略 ``hasMore``，置顶会话超过 100 个时
        会**漏掉会话**（进而漏轮询），并让黑名单对账（_reconcile_blocklist）据此
        误判可访问性。这里按 ``hasMore`` / ``nextCursor`` 翻页并去重合并。
        """
        merged: list[dict] = []
        seen: set[str] = set()
        cursor = ""
        for _ in range(self._TOP_CONVERSATIONS_MAX_PAGES):
            args = ["chat", "list-top-conversations", "--limit", str(limit)]
            if cursor:
                args += ["--cursor", str(cursor)]
            data = self.run(args, operation="chat_list_top_conversations",
                            force_no_dry_run=True)
            result = self._get_result(data)
            if not isinstance(result, dict):
                break
            for c in result.get("conversations", []) or []:
                cid = c.get("openConversationId", "")
                # 仅在能取到 id 时去重；取不到 id 的条目按旧行为保留（不过度丢弃）
                if cid:
                    if cid in seen:
                        continue
                    seen.add(cid)
                merged.append(c)
            if not result.get("hasMore"):
                break
            next_cursor = str(result.get("nextCursor", "") or "")
            # 游标停滞保护：缺失或与上一页相同即停止，避免死循环重复打接口
            if not next_cursor or next_cursor == str(cursor):
                break
            cursor = next_cursor
        return merged

    def chat_list_groups_joined(self, limit: int = 200) -> list[dict]:
        """分页拉取「我加入的所有群」（dws chat +chat-list-all）。

        钉钉群聊不在「消息搜索权益」覆盖范围内，``chat message list-all`` 只回单聊，
        导致群消息长期拉不到。本方法通过专用群列举命令补全群枚举，返回的
        openConversationId 进入轮询会话集后，由 poller 走 ``chat message list``
        （list-all 按 openConversationId 过滤）拉取群消息。
        """
        return self._chat_list_groups(["chat", "+chat-list-all"], limit)

    def chat_list_groups_mine(self, limit: int = 0) -> list[dict]:
        """拉取「我创建/管理的群」（dws chat +chat-list-mine）。

        ⚠️ 该 shortcut **不支持 --cursor**：dws v1.0.62-beta.8 实测传 --cursor 会直接
        返回 unknown flag 错误（合法 flag 仅 limit / exclude-muted / role）。但其
        ``--limit`` **不传即返回全部**（实测一次返回全部自建群）。故这里 limit 默认 0
        （不传 --limit）、单次拉取不翻页。

        旧实现复用通用分页函数会拼 --cursor，第 2 页在真实 dws 上必然失败——虽被
        except 吞掉不致崩，但每次都产生一次无效调用 + 告警日志，且只能拿到第一页。
        """
        return self._chat_list_groups(["chat", "+chat-list-mine"], limit,
                                      supports_cursor=False)

    def _chat_list_groups(self, base_args: list[str], limit: int,
                          *, supports_cursor: bool = True) -> list[dict]:
        """通用群列表分页拉取，合并去重返回 [{openConversationId, name}]。

        ``supports_cursor=False``：不拼 --cursor 且只请求一次（用于 +chat-list-mine）；
        此时 ``limit=0`` 表示不传 --limit —— dws 语义为「返回全部」。
        """
        merged: list[dict] = []
        seen: set[str] = set()
        cursor = ""
        pages = 0
        while True:
            pages += 1
            args = list(base_args)
            if limit:
                args += ["--limit", str(limit)]
            if supports_cursor and cursor:
                args += ["--cursor", str(cursor)]
            try:
                data = self.run(args, operation="chat_list_groups", force_no_dry_run=True)
            except DwsError as e:
                logger.warning("[DWS] 群列表拉取失败（%s）: %s", base_args[-1], e)
                break
            result = self._get_result(data)
            if not isinstance(result, dict):
                break
            for g in result.get("groups", []) or []:
                cid = g.get("openConversationId", "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                merged.append({"openConversationId": cid, "name": g.get("name", "")})
            if not supports_cursor:
                break  # 不支持游标分页：单次请求即为全量
            if result.get("complete") or not result.get("nextCursor") or pages >= 10:
                break
            cursor = result.get("nextCursor", "") or ""
            if not cursor:
                break
        return merged

    def chat_message_list_direct(self, user_id: str = "",
                                 open_dingtalk_id: str = "",
                                 time_str: str = "",
                                 limit: int = 50) -> list[dict]:
        """拉取单聊消息：**从 ``time_str`` 向现在方向**（``--direction newer``）取一页。

        ⚠️ 方向语义（曾长期用错，务必看清）：``--direction`` 才是文档化参数，
        ``newer``=从给定时间往现在拉、``older``=从给定时间往以前拉（``--forward`` 是它的
        兼容别名，``--forward=true``≡newer、``false``≡older）。**注意 ``--forward`` 不是
        「排序方向」**——旧实现写 ``--forward=false``（=older）并注释为「按时间正序返回」，
        实际是在**向历史方向**拉取：每轮都取到早于游标的老消息、``hasMore`` 恒为 true，
        并按这批老消息的最大时间戳回写游标，使游标在历史里**逐轮后退**（实测同一会话
        16:48→09-14→09-11），新消息只能靠 event 流兜住。现改为 ``--direction newer``。

        ⚠️ ``list-direct``（实测 v1.0.61 / v1.0.62-beta.8 均如此）**没有** ``--page-all``，
        也**不支持** ``--cursor``，故无法在适配器内自动翻页补齐——它只回一页 ``--limit`` 条
        （升序：本页最旧→最新）。这意味着「单聊一次积压 > limit 条」时本方法拿不全，
        必须由调用方保证不漏：游标按页内最大时间戳推进（**不 +1s 越秒前进**），
        未取到的消息都比游标新，下一轮从同一时间点重新拉取即可覆盖，再靠 msg_id 去重
        跳过已处理消息（见 poller_strategy._update_poll_time_and_db）。
        返回体带 ``hasMore``（真积压，还有比本页更新的消息）时打 WARNING 便于发现异常。
        """
        args = ["chat", "message", "list-direct",
                "--time", time_str,
                "--limit", str(limit),
                "--direction", "newer"]
        if open_dingtalk_id:
            args.extend(["--open-dingtalk-id", open_dingtalk_id])
        elif user_id:
            args.extend(["--user", user_id])
        else:
            raise ValueError("Either user_id or open_dingtalk_id is required")
        data = self.run(args, operation="chat_message_list_direct", force_no_dry_run=True)
        result = self._get_result(data)
        if not isinstance(result, dict):
            return []
        if result.get("hasMore"):
            logger.warning(
                "[DWS] 单聊消息单页已满（time=%s, limit=%d）：该命令不支持自动翻页，"
                "仍有比本页更新的消息，依赖下一轮从本页最大时间戳重拉（去重跳过已处理）",
                time_str, limit,
            )
        return result.get("messages", [])

    def chat_message_list_group(self, group_id: str, time_str: str,
                                 limit: int = 50,
                                 timeout: int | None = None) -> list[dict]:
        """按 openConversationId 拉取单个群的会话消息（用户本人身份，按时间正序）。

        底层命令 ``dws chat message list --group <openConversationId> --time <t>
        --direction newer``，是**用户级逐群接口**（非群机器人接口），实测对个人钉钉群
        可用，且能拉到「工作通知」等系统推送会话的消息。

        ⚠️ 为什么不用 list-all（search_messages_by_time_range）：该接口依赖「消息搜索权益」，
        而该权益默认**不覆盖群聊**，对群调用会返回业务错误（PREPARE_CALL_TOOL_ERROR），
        导致群消息长期拉不到。逐群接口不受此限制，是群消息的正确拉取通道。

        ⚠️ 必须带 ``--page-all``：单页只返回 ``--limit`` 条并带 ``hasMore`` / ``nextCursor``，
        旧实现忽略分页标志只取第一页，超出一页的消息被**静默丢弃**（实测 limit=5 只回 5 条
        且 hasMore=true，同一窗口实际有 6 条）。交由 dws 按服务端**毫秒级** nextCursor
        自动翻页并按 messageId 去重后聚合，才能覆盖「同一秒内多条消息 / 一次积压超过
        一页」这两种真实场景——它们只靠调用方按**秒级** ``--time`` 前进是无法补齐的。
        仍被 ``--page-limit`` / ``--max-items`` 截断时打 WARNING，便于发现异常积压。
        """
        page_limit = self._MESSAGE_LIST_MAX_PAGES
        max_items = max(limit, 1) * page_limit
        data = self.run([
            "chat", "message", "list",
            "--group", group_id,
            "--time", time_str,
            "--direction", "newer",
            "--limit", str(limit),
            "--page-all",
            "--page-limit", str(page_limit),
            "--max-items", str(max_items),
        ], operation="chat_message_list_group", force_no_dry_run=True,
           timeout=timeout or self.timeout)
        result = self._get_result(data)
        if not isinstance(result, dict):
            return []
        # partial / truncated 表示翻页被页数或总量上限截断，本窗口仍有消息未取到。
        # 不静默吞掉：调用方据此可判断「本轮游标推进后需继续追赶」。
        if result.get("partial") or result.get("truncated"):
            logger.warning(
                "[DWS] 群消息翻页被截断（group=%s, time=%s, limit=%d, 上限=%d 条）："
                "stopReason=%s, pagesFetched=%s, 已取 %d 条，剩余消息将在下轮继续拉取",
                group_id[:24], time_str, limit, max_items,
                result.get("stopReason"), result.get("pagesFetched"),
                len(result.get("messages", []) or []),
            )
        return result.get("messages", []) or []

    def chat_message_list(self, group: str, time_str: str,
                          limit: int = 50,
                          timeout: int | None = None) -> list[dict]:
        """拉取指定群聊的消息（按时间正序）。

        群消息走用户级逐群接口 ``chat message list --group``（见 ``chat_message_list_group``），
        绕过 list-all 的「消息搜索权益」群聊限制；旧实现经 list-all 按 openConversationId 过滤，
        但 list-all 对群返回业务错误，导致群消息长期拉不到。
        """
        return self.chat_message_list_group(group, time_str, limit, timeout)

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
                next_cursor = str(result.get("nextCursor", "") or "")
                has_more = bool(result.get("hasMore", False))
                merged["hasMore"] = has_more
                merged["nextCursor"] = next_cursor
                if not has_more or not next_cursor or pages >= effective_max_pages:
                    if pages >= effective_max_pages and has_more:
                        # 同窗口 5 分钟内只提示一次，避免每轮轮询刷屏（实际仍会停止翻页）。
                        # 该上限是设计内的保护机制：实时轮询触顶说明窗口内消息过多，
                        # 应缩小时间窗或走 sync_history 做深度回填，不是需要立即处理的异常。
                        now_ts = time.time()
                        warn_key = f"{start}|{end}"
                        last = self._list_all_cap_warn_at.get(warn_key, 0.0)
                        if now_ts - last >= 300:
                            logger.info(
                                "list-all 分页达到上限 %d 页（时间窗 %s~%s），"
                                "停止翻页，窗口内可能仍有未拉取消息",
                                effective_max_pages, start, end
                            )
                            self._list_all_cap_warn_at[warn_key] = now_ts
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
            if media_id:
                # 已有 mediaId（复用上游上传结果）→ 原生 image 消息
                args.extend(["--msg-type", "image", "--media-id", media_id])
            elif file_path:
                # ⚠️ dws 已下线「本地文件 → mediaId」的通用上传能力：``chat media upload``
                # 现在直接返回 validation error（"已下线，当前 CLI 不提供通用的本地文件
                # 到 mediaId 的上传能力"）。官方指引：本地图片/文件改用
                # ``chat message send --msg-type file --file <本地路径>`` 直接发送。
                # 故此处降级为 file 消息，避免整条发图链路失败。
                args.extend(["--msg-type", "file", "--file", file_path])
            else:
                raise ValueError("msg_type=image 需要 media_id 或 file_path")
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
        """编辑已发送的消息内容（用于流式输出：先占位再逐步 patch）。

        封装 ``dws chat message edit --conversation-id <cid> --message-id <mid> --text <t>``。

        ⚠️ dws 已移除 ``chat message update``（核实 v1.0.61 与 v1.0.62-beta.8 均无此
        子命令，仅剩 ``edit``），且 ``--msg-id`` 更名为 ``--message-id``、
        ``--conversation-id`` 为必填。旧实现三处皆错，导致钉钉流式输出每一轮更新都
        失败——占位消息永远停在 "..."（2026-09-15 核对 dws 新版本时发现）。

        ``edit`` 只能按 openConversationId 定位会话，**不支持按 user 定位**；单聊同样
        以 openConversationId 表达（Linkora 的 ``message.chat_id`` 即该值）。故 ``user``
        参数仅为签名兼容而保留，不参与命令拼装。
        """
        conversation_id = group or ""
        if not conversation_id:
            raise ValueError(
                "chat_message_update 需要 group（openConversationId）："
                "dws chat message edit 只支持按会话定位，无 --user 参数"
            )
        args = ["chat", "message", "edit",
                "--conversation-id", conversation_id,
                "--message-id", message_id]
        if text:
            # 钉钉不渲染 markdown 表格：仅 markdown 格式发送前转换（流式更新同理）
            if classify_message_format(text) == "markdown" and not self.supports_markdown_tables:
                text = normalize_markdown_for_platform(text, supports_tables=False)
            args.extend(["--text", text])
        if title:
            args.extend(["--title", title])
        logger.debug("[DWS] 编辑消息: dws %s", " ".join(args))
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

    # === 消息搜索（v1.0.59+）：替代全量拉取后本地过滤 ===

    def chat_message_search(self, *, query: str,
                            conversation_id: str | None = None,
                            start: str | None = None, end: str | None = None,
                            limit: int = 100, cursor: str = "0",
                            page_all: bool = False,
                            max_items: int = 0, page_limit: int = 50,
                            page_delay: int = 200) -> dict:
        """在用户会话中按关键词搜索消息（dws ``chat message search``）。

        服务端关键词检索，替代 ``chat_message_list_all`` 全量拉取后在本地 grep 的低效路径。
        返回结构含顶层 ``messages``（稳定字段 ``messageId`` / ``text``，兼容保留
        ``openMessageId`` / ``content`` / 原始 ``result``）。

        默认时间窗最近 7 天到当前；不传 ``conversation_id`` 则搜索所有会话。
        未指定会话默认只读单页；``page_all=True`` 自动翻页并合并跨页结果。

        RAG / 历史回填场景：用本方法按 query 检索候选消息，再交给 LLM 做语义精排，
        比拉全量再过滤省 90%+ 的 API 调用量。
        """
        if not query:
            raise ValueError("chat_message_search 需提供 query")
        args = [
            "chat", "message", "search",
            "--query", query,
            "--limit", str(limit),
            "--cursor", cursor,
        ]
        if conversation_id:
            args.extend(["--conversation-id", conversation_id])
        if start:
            args.extend(["--start", start])
        if end:
            args.extend(["--end", end])
        if page_all:
            args.append("--page-all")
            args.extend([
                "--max-items", str(max_items),
                "--page-limit", str(page_limit),
                "--page-delay", str(page_delay),
            ])
        data = self.run(args, operation="chat_message_search", force_no_dry_run=True)
        return self._get_result(data)

    def chat_message_search_advanced(self, *, query: str | None = None,
                                     user: str | None = None,
                                     users: str | None = None,
                                     sender_ids: str | None = None,
                                     at_ids: str | None = None,
                                     at_me: bool = False,
                                     conversation_ids: str | None = None,
                                     start: str | None = None, end: str | None = None,
                                     limit: int = 100, cursor: str = "0",
                                     page_all: bool = False,
                                     max_items: int = 0, page_limit: int = 50,
                                     page_delay: int = 200) -> dict:
        """多维搜索消息（dws ``chat message search-advanced``）。

        支持关键词 / 发送者 / @我 / @指定人 / 指定会话 / 时间范围组合检索。
        发送者 userId 用 ``user`` / ``users``；发送者或 @人 的 openDingTalkId 用
        ``sender_ids`` / ``at_ids``。

        至少指定一个搜索条件（否则 dws 拒绝执行）。
        """
        if not any([query, user, users, sender_ids, at_ids, at_me, conversation_ids]):
            raise ValueError("search_advanced 至少指定一个搜索条件（query/user/users/"
                             "sender_ids/at_ids/at_me/conversation_ids）")
        args = [
            "chat", "message", "search-advanced",
            "--limit", str(limit),
            "--cursor", cursor,
        ]
        if query:
            args.extend(["--query", query])
        if user:
            args.extend(["--user", user])
        if users:
            args.extend(["--users", users])
        if sender_ids:
            args.extend(["--sender-ids", sender_ids])
        if at_ids:
            args.extend(["--at-ids", at_ids])
        if at_me:
            args.append("--at-me")
        if conversation_ids:
            args.extend(["--conversation-ids", conversation_ids])
        if start:
            args.extend(["--start", start])
        if end:
            args.extend(["--end", end])
        if page_all:
            args.append("--page-all")
            args.extend([
                "--max-items", str(max_items),
                "--page-limit", str(page_limit),
                "--page-delay", str(page_delay),
            ])
        data = self.run(args, operation="chat_message_search_advanced", force_no_dry_run=True)
        return self._get_result(data)

    # === 流式卡片（v1.0.59+）：交互式卡片消息，可多次 update ===

    def chat_message_send_card(self, *, group: str | None = None,
                               open_dingtalk_id: str | None = None,
                               at_all: bool = False,
                               at_open_dingtalk_ids: str | None = None) -> dict:
        """创建流式卡片（``dws chat message send-card``），返回 bizId 供 ``update_card`` 续填内容。

        群聊传 ``group``（openConversationId），单聊传 ``open_dingtalk_id``，二者互斥。
        创建时无需传入卡片内容，后续通过 ``chat_message_update_card`` 更新内容；
        最后一次更新必须将 ``flow_status`` 设为 3（finish），否则卡片一直显示"生成中"。

        适合长内容生成场景（如会议纪要/审批结论）：先占卡片位，再流式 patch 内容，
        避免先发占位文本再追发造成的消息碎片。
        """
        if group:
            target = ["--conversation-id", group]
        elif open_dingtalk_id:
            target = ["--open-dingtalk-id", open_dingtalk_id]
        else:
            raise ValueError("send_card 需提供 group（群）或 open_dingtalk_id（单聊）之一")
        args = ["chat", "message", "send-card"] + target
        if at_all:
            args.append("--at-all")
        if at_open_dingtalk_ids:
            args.extend(["--at-open-dingtalk-ids", at_open_dingtalk_ids])
        data = self.run(args, operation="chat_message_send_card", force_no_dry_run=True)
        return self._get_result(data)

    def chat_message_update_card(self, *, biz_id: str, content: str,
                                 flow_status: int = 3) -> dict:
        """更新已发送的流式卡片内容（``dws chat message update-card``）。

        Args:
            biz_id: send_card 返回的业务 ID（必填）
            content: 卡片消息内容（必填）
            flow_status: 流式状态，1=处理中 2=输入中 3=完成 4=执行中 5=错误；
                最后一次更新必须设 3（finish），否则卡片一直处于"生成中"加载态

        典型用法：生成过程中多次 ``flow_status=2`` 增量更新，结束时 ``flow_status=3`` 收尾。
        """
        if not biz_id:
            raise ValueError("update_card 需提供 biz_id")
        if content is None:
            raise ValueError("update_card 需提供 content")
        args = [
            "chat", "message", "update-card",
            "--biz-id", biz_id,
            "--content", content,
            "--flow-status", str(flow_status),
        ]
        data = self.run(args, operation="chat_message_update_card", force_no_dry_run=True)
        return self._get_result(data)
