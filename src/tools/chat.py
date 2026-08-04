from __future__ import annotations

import logging
import threading
import uuid as uuid_mod
from datetime import datetime, timedelta

from src.dws_adapter import DwsAdapter
from src.memory.platform_context import get_current_platform
from src.tools.base import BaseTool
from src.tools.media import is_allowed_local_path

logger = logging.getLogger(__name__)


class SendMessageTool(BaseTool):
    name = "send_message"
    display_name = "发送消息"
    platforms = ["dingtalk"]
    short_description = "向指定会话发送消息，支持文本/Markdown、图片、文件、音频、视频，可@成员"
    description = (
        "向指定会话（群聊或单聊）发送消息。支持多种消息格式，由你根据场景自行选择：\n"
        "• text（默认）：普通对话回复、问答、提醒。text 支持 Markdown（标题、**加粗**、列表、链接、表格），\n"
        "  且配合 --title 会渲染成「markdown 卡片」（标题栏 + 渲染后的正文）。天气、日程、列表等结构化数据\n"
        "  建议用 text + 一个有意义的 title + Markdown 正文，呈现为卡片比纯文本更易读。\n"
        "• image：发送图片，必须提供已上传的 media_id（或传本地 file_path 由工具自动上传后取得）。\n"
        "  适用于图表截图、二维码、图片素材等。\n"
        "• file：发送本地文档/表格/附件（pdf、xlsx、docx 等）。\n"
        "• audio：发送本地语音/录音文件（mp3、wav 等）。\n"
        "• video：发送本地视频文件（mp4 等）。\n"
        "转发对方发来的文件：对话里若出现「[本地文件] /路径」或「[本地图片] /路径」，"
        "那是用户此前发来的真实文件（已自动存到本地），对方要求再发/转发该文件时，"
        "直接把该绝对路径填进 file_path 即可（仅 data/ 与 /tmp 下允许）。\n"
        "群聊可用 at_all=true（内容会自动补入 <@all>）或 at_open_dingtalk_ids='id1,id2' 指定@某人。\n"
        "选型原则：不确定时默认 text；有明确附件/媒体需求时选对应富媒体类型；图片优先用 media_id。\n"
        "当用户让你发消息、发文件、发图片、发语音、发视频、@某人 时使用。"
    )
    intent_keywords: list[str] = []  # 基础工具，始终包含
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": (
                    "目标会话的唯一标识，直接使用当前消息对象的 chat_id 字段值，"
                    "不要自行推断或替换为其他 ID 格式（如 userId / openDingTalkId）。"
                    "飞书：无论单聊群聊均为 oc_xxx 格式的 openConversationId。"
                    "钉钉：群聊为 openConversationId，单聊为 userId 或 openDingTalkId。"
                )
            },
            "chat_type": {
                "type": "string",
                "enum": ["single", "group"],
                "description": (
                    "会话类型：single=单聊, group=群聊。"
                    "注意：飞书单聊的 chat_id 依然是 oc_xxx 格式（openConversationId），不是 userId；"
                    "不要因为 chat_type=single 就把 chat_id 替换为 userId 格式。"
                )
            },
            "text": {
                "type": "string",
                "description": "消息正文。text 类型必填；image/file/audio/video 类型可作为可选说明/标题"
            },
            "msg_type": {
                "type": "string",
                "enum": ["auto", "text", "markdown", "image", "file", "audio", "video"],
                "description": "消息格式，默认 auto（程序按内容结构自动判定 text / markdown：短纯文本按 text、含标题/列表/表格/加粗等结构化内容按 markdown）。也可显式指定 text / markdown。image 需 media_id；file/audio/video 需 file_path"
            },
            "title": {
                "type": "string",
                "description": "消息标题（可选，显示在消息列表/通知上；不填默认用正文）"
            },
            "media_id": {
                "type": "string",
                "description": "图片 media_id（msg_type=image 时使用）。可通过 dws dt_media_upload 上传本地图片取得；也可不填而直接传 file_path 由工具自动上传"
            },
            "file_path": {
                "type": "string",
                "description": "本地文件路径（msg_type=image/file/audio/video 时使用）。file/audio/video 由 CLI 自动上传发送；image 不填 media_id 时也会先自动上传"
            },
            "at_all": {
                "type": "boolean",
                "description": "是否@所有人（仅群聊生效）。true 时内容会自动补入 <@all> 占位符"
            },
            "at_open_dingtalk_ids": {
                "type": "string",
                "description": "@指定成员的 openDingTalkId 列表，逗号分隔（仅群聊生效），如 'id1,id2'。内容会自动补入 <@id> 占位符"
            },
            "ai_tag": {
                "type": "boolean",
                "description": "是否带 AI 发送角标，默认 true（不填即带）"
            },
        },
        "required": ["chat_id", "chat_type"],
    }

    def __init__(self, dws: DwsAdapter, store=None, self_user_id: str | list[str] = ""):
        """
        Args:
            dws: DWS 适配器
            store: SQLite 存储（可选）
            self_user_id: 机器人自己的 userId / openDingTalkId，用于防「自发自」护栏。
                支持传入字符串或列表（H10修复：同时传入两种 ID 格式）。
                为空则不启用该硬护栏。
        """
        self.dws = dws
        self.store = store
        # 【H10修复】self_user_id 支持列表，同时匹配 userId 和 openDingTalkId
        if isinstance(self_user_id, list):
            self.self_user_ids = [x.strip() for x in self_user_id if x and x.strip()]
        elif self_user_id and str(self_user_id).strip():
            self.self_user_ids = [str(self_user_id).strip()]
        else:
            self.self_user_ids = []
        # 向后兼容：保留 self_user_id 属性（取列表第一个元素）
        self.self_user_id = self.self_user_ids[0] if self.self_user_ids else ""
        self._send_lock = threading.Lock()
        self._recent_sends: list[tuple[str, float]] = []

    def execute(self, args: dict) -> str | dict:
        import os
        from time import time

        chat_id = args.get("chat_id", "")
        chat_type = args.get("chat_type", "group")
        text = args.get("text", "") or ""
        msg_type = (args.get("msg_type") or "auto").lower()
        title = args.get("title", "") or ""
        media_id = args.get("media_id") or None
        file_path = args.get("file_path") or None
        at_all = bool(args.get("at_all", False))
        at_ids = args.get("at_open_dingtalk_ids") or None
        ai_tag = args.get("ai_tag", None)  # None=用实例默认(True)

        if not chat_id:
            return {"error": "chat_id is required"}

        # 【护栏 P0-3】防「自发自」：单聊时 chat_id 若是机器人自己的 userId / openDingTalkId，
        # 意味着 LLM 拼错 ID 把消息发回自己——钉钉那边仍会作为新消息被 poller 拉到，
        # 轮转产生「AI 发的 → poller 拉回 → agent 再回复」的死循环。
        # 【H10修复】同时匹配 userId 和 openDingTalkId 两种格式
        if self.self_user_ids and chat_id and chat_id in self.self_user_ids:
            logger.warning(
                "[护栏 send_message] 拒绝发往机器人自己 ID=%s（防自发自回声）；text=%r",
                chat_id, (text or "")[:50],
            )
            return {"error": f"禁止向机器人自己的会话发送消息（chat_id={chat_id} 命中自身 ID），如需自调请换其他会话 ID"}

        # 【护栏 P0-3】防「短时重复」：同一 chat_id 在 10 秒内连续发 ≥3 次 → 认定为 LLM
        # 幻觉/回声（agent 多次跳到同个会话发同条消息）。跟踪发送时间戳 list。
        # 线程安全：SendMessageTool 实例被多线程（agent 并发回复）共享，list 的
        # 非原子 pop/append/切片在并发下可能状态错乱甚至 IndexError，导致回声死循环
        # 护栏失效。整段临界区用实例锁保护；with 块的 return 会先释放锁，安全。
        with self._send_lock:
            now_ts = time()
            # 清理超过 10 秒的记录（list 顺序追加，从头部 pop 过期）
            while self._recent_sends and (now_ts - self._recent_sends[0][1] > 10):
                self._recent_sends.pop(0)
            # 统计该 chat_id 近期发送次数。阈值2表示：已有2次历史 + 本次 = 3次/10s，到达限频拒。
            same_chat_hits = sum(1 for cid, _ in self._recent_sends if cid == chat_id)
            if same_chat_hits >= 2:
                logger.warning(
                    "[护栏 send_message] 拒绝短时重复发送（chat_id=%s，10s 内已发 %d 次，加本次将达 %d 次）；text=%r",
                    chat_id, same_chat_hits, same_chat_hits + 1, (text or "")[:50],
                )
                return {"error": f"发送频次异常：chat_id={chat_id} 在 10 秒内已发送 {same_chat_hits} 次，疑似 LLM 回声，已拒绝"}
            self._recent_sends.append((chat_id, now_ts))
            # 限容，防止内存泄
            if len(self._recent_sends) > 200:
                # 丢弃最老一半（不重新走 pop 循环）
                self._recent_sends = self._recent_sends[-100:]

        # 参数校验（按类型）
        if msg_type == "image":
            if not media_id and not file_path:
                return {"error": "msg_type=image 需要 media_id 或 file_path（二选一）"}
            if file_path and not os.path.isfile(file_path):
                return {"error": f"file_path 不是有效文件: {file_path}"}
            # 安全护栏：图片自动上传路径同样受限，防越权读取敏感文件
            if file_path and not is_allowed_local_path(file_path):
                return {"error": f"安全限制：仅允许发送 data/ 与 /tmp 下的文件，拒绝: {file_path}"}
        elif msg_type in ("file", "audio", "video"):
            if not file_path:
                return {"error": f"msg_type={msg_type} 需要 file_path（本地文件路径）"}
            if not os.path.isfile(file_path):
                return {"error": f"file_path 不是有效文件: {file_path}"}
            # 安全护栏：仅允许发送 data/ 与 /tmp 下的本地文件，防越权外发系统机密
            if not is_allowed_local_path(file_path):
                return {"error": f"安全限制：仅允许发送 data/ 与 /tmp 下的文件，拒绝: {file_path}"}
        else:  # text / markdown
            if not text:
                return {"error": "text 消息需要 text 内容"}

        # 【提示词泄漏防线】send_message 是 LLM 直发路径（agent 标记 already_sent
        # 后跳过 _done() 的 enforce_brevity 清洗）——此前该路径完全绕过
        # sanitize_reply，模型把 system prompt 痕迹塞进工具参数 text 会原样发出。
        # 在发送前统一清洗；清洗后为空说明整段都是泄漏内容，拒发。
        if text:
            try:
                from src.llm.style import sanitize_reply
                cleaned_text = sanitize_reply(text)
            except Exception:
                logger.warning("[resilience] send_message 清洗失败，按原文发送", exc_info=True)
                cleaned_text = text
            if cleaned_text != text:
                logger.warning(
                    "[sanitize send_message] 工具直发文本含提示词泄漏痕迹，已清洗: %d -> %d 字符",
                    len(text), len(cleaned_text),
                )
                if not cleaned_text and msg_type in ("text", "markdown", "auto"):
                    return {"error": "消息内容全部为内部推理痕迹，已拦截不发送"}
                text = cleaned_text

        # 解析目标（群/单聊）
        group = chat_id if chat_type == "group" else None
        peer_user_id = ""
        peer_oid = ""
        if chat_type != "group":
            if self.store:
                conv = self.store._conversation_repo.get_conversation(chat_id)
                if conv:
                    peer_user_id = conv.get("peer_user_id") or ""
                    peer_oid = conv.get("peer_open_dingtalk_id") or ""
            # chat_id 本身可能就是 userId 或 openDingTalkId
            # 纠错：peer_oid 若命中机器人自己的 user 级 ID，说明 conversations
            # 表的 peer_open_dingtalk_id 被污染（如外部好友的 peer 被误写为
            # 机器人自己的 openDingTalkId），拒绝发送并记录日志。
            if peer_oid and self.self_user_ids and peer_oid in self.self_user_ids:
                logger.warning(
                    "[护栏 send_message] 拒绝发往自身：peer_oid=%s 命中机器人自己 ID"
                    "（conversations.peer_open_dingtalk_id 可能被污染，对应的 chat_id=%s）",
                    peer_oid, chat_id,
                )
                return {"error": f"peer_open_dingtalk_id={peer_oid} 命中自身 ID，"
                                 f"拒绝发送。请排查 conversations 表中 "
                                 f"chat_id={chat_id} 的 peer_open_dingtalk_id 是否正确"}

        reply_uuid = str(uuid_mod.uuid4())
        # 外部好友（跨租户）必须用 --chat-id oc_xxx 发送，不能用 --user-id ou_xxx
        # 否则触发飞书 230038 "cross tenant p2p chat operate forbid"。
        is_external = False
        if peer_oid and self.store:
            try:
                ef = self.store._external_friend_repo.get_external_friend_by_id(peer_oid)
                is_external = bool(ef)
            except Exception:
                logger.warning("get_external_friend_by_id failed peer_oid=%s", peer_oid, exc_info=True)

        # 降级：会话已在黑名单（飞书跨租户 p2p 不可发送、离职、跨组织无权限等），
        # 直接跳过 lark-cli 调用，避免每轮重试都刷 230027/230002 错误。
        # 注意：仍返回结构化 result，调用方可据此让 LLM 知道本会话不可代发。
        if self.store and chat_id:
            try:
                if self.store._blacklist_repo.is_conversation_blocked(chat_id):
                    blk = next(
                        (b for b in self.store._blacklist_repo.list_blocked_conversations()
                         if b.get("chat_id") == chat_id),
                        None,
                    )
                    reason = (blk or {}).get("reason") or "会话已加入黑名单"
                    cu = (blk or {}).get("cooldown_until") if blk else None
                    fc = (blk or {}).get("failure_count", 0) if blk else 0
                    is_perm = not cu
                    if is_perm:
                        # 永久黑名单
                        logger.info(
                            "[降级 send_message] chat_id=%s 永久黑名单（fc=%d, reason=%s），跳过",
                            chat_id, fc, reason,
                        )
                        user_hint = (
                            f"本会话已永久黑名单（{reason}），分身不会代发消息。"
                            "请考虑：1) 让对方在飞书里加你“OWNER CLI”机器人为好友后继续 p2p；"
                            "2) 拉对方进一个包含机器人的群聊。"
                        )
                    else:
                        # 临时冷却：仅记入日志，不报错；会让发送跳过 lark-cli 调用但返回给 LLM 一个
                        # 「还差 1/2 次失败才升级」的提示，以帮助后续决策。
                        remain = self.store._blacklist_repo.cooldown_remaining(chat_id)
                        logger.info(
                            "[降级 send_message] chat_id=%s 临时冷却中（fc=%d, 剩 %ds, reason=%s）",
                            chat_id, fc, remain, reason,
                        )
                        user_hint = (
                            f"本会话发送失败 {fc} 次（最大 3 次），分身暂不代发。"
                            f"下次重试需等 {remain}s 后。\n"
                            f"原因：{reason}\n"
                            "修复建议：1) 走「包含机器人的群聊」路径；"
                            "2) 在飞书开放平台确认「应用权限-机器人」中启用了「发消息给外部用户」。"
                        )
                    return {
                        "success": False,
                        "degraded": True,
                        "reason": user_hint,
                        "chat_id": chat_id,
                    }
            except Exception as e:
                logger.debug("检查 blocked_conversations 失败: %s", e)

        # 发送
        # 飞书身份降级控制：跨租户外部好友场景（is_external=True）不要走 bot 身份
        # fallback——本场景下 bot 也无权发到跨租户会话，但会错误地消耗一次调用
        # 并且如果成员表有错可能泄露内部信息。其余场景下发生 230027/230002 时
        # feishu.py 会自动用 --as bot 重试。
        prev_disable = getattr(self.dws, "_disable_bot_fallback", False)
        if is_external:
            self.dws._disable_bot_fallback = True
        try:
            if chat_type == "group":
                self.dws.chat_message_send(
                    group=group, title=title, text=text, uuid=reply_uuid,
                    ai_tag=ai_tag, msg_type=msg_type, media_id=media_id,
                    file_path=file_path, at_all=at_all,
                    at_open_dingtalk_ids=at_ids,
                )
            else:
                if is_external and chat_id.startswith("oc_"):
                    # 外部好友（跨租户）用 chat_id（oc_xxx）作为 --chat-id 发送
                    self.dws.chat_message_send(
                        group=chat_id, title=title, text=text,
                        uuid=reply_uuid, ai_tag=ai_tag, msg_type=msg_type,
                        media_id=media_id, file_path=file_path,
                        at_all=at_all, at_open_dingtalk_ids=at_ids,
                    )
                elif peer_oid:
                    self.dws.chat_message_send(
                        open_dingtalk_id=peer_oid, title=title, text=text,
                        uuid=reply_uuid, ai_tag=ai_tag, msg_type=msg_type,
                        media_id=media_id, file_path=file_path,
                        at_all=at_all, at_open_dingtalk_ids=at_ids,
                    )
                elif peer_user_id:
                    self.dws.chat_message_send(
                        user=peer_user_id, title=title, text=text,
                        uuid=reply_uuid, ai_tag=ai_tag, msg_type=msg_type,
                        media_id=media_id, file_path=file_path,
                        at_all=at_all, at_open_dingtalk_ids=at_ids,
                    )
                else:
                    self.dws.chat_message_send(
                        open_dingtalk_id=chat_id, title=title, text=text,
                        uuid=reply_uuid, ai_tag=ai_tag, msg_type=msg_type,
                        media_id=media_id, file_path=file_path,
                        at_all=at_all, at_open_dingtalk_ids=at_ids,
                    )
        except Exception as e:
            err_text = str(e)
            # 飞书跨租户 / 跨企业 / 应用未在会话 场景：永久失败，写入黑名单避免反复触发。
            # 错误码：230027 user_unauthorized（user→跨企业外部联系人被策略拒）
            #        230002 Bot/User can NOT be out of the chat（bot 不在 p2p 会话）
            # 触发后本 chat_id 后续发送全部跳过（持久化在 blocked_conversations 表）。
            # 严格门限：仅在「本会话已被识别为 is_external」（跨租户外部好友）时
            # 才走此降级，避免误吞其他平台的同类错误码。is_external 在发送前已赋值。
            is_unsendable_external = is_external and (
                "230027" in err_text
                or "230002" in err_text
                or "user_unauthorized" in err_text.lower()
                or ("Bot/User can NOT be out of the chat" in err_text)
                or ("external-chat" in err_text.lower() and "policy" in err_text.lower())
            )
            if is_unsendable_external and self.store and chat_id:
                try:
                    conv_info = self.store._conversation_repo.get_conversation(chat_id) or {}
                    # 跨租户外部好友不一律黑名单：仅连续失败 ≥ 3 次才升级永久黑名单；
                    # 1/2 次失败仅写「临时冷却」（cooldown_until=now+1h）。
                    # 原因：跨租户可能是当前 app 缺权限（可后期授权恢复），
                    # 也可能是管理员策略（不可逆）；不能用首次失败就下结论。
                    existing = self.store._blacklist_repo.is_conversation_blocked(chat_id)
                    is_perm = False
                    if existing:
                        # 检查是否已为永久黑名单
                        cur = self.store.conv_conn(get_current_platform()).cursor()
                        cur.execute(
                            "SELECT cooldown_until, failure_count FROM blocked_conversations WHERE chat_id = ?",
                            (chat_id.rstrip("="),),
                        )
                        row = cur.fetchone()
                        if row and (not row["cooldown_until"]):
                            is_perm = True
                    if not existing or is_perm:
                        # 未冻结 或 已是永久黑名单 → 顺加 failure_count
                        self.store._blacklist_repo.add_blocked_conversation(
                            chat_id=chat_id,
                            chat_name=conv_info.get("chat_name") or "",
                            chat_type=chat_type or "p2p",
                            reason=("飞书永久黑名单：跨租户p2p不可达" if is_perm
                                    else "飞书跨租户p2p首次失败（进入冷却）"),
                            source="feishu_external_chat_unsendable",
                            last_error=err_text,
                        )
                        cur = self.store.conv_conn(get_current_platform()).cursor()
                        cur.execute(
                            "SELECT failure_count FROM blocked_conversations WHERE chat_id = ?",
                            (chat_id.rstrip("="),),
                        )
                        fc_row = cur.fetchone()
                        fc = int(fc_row["failure_count"]) if fc_row else 0
                    else:
                        # 处于冷却中：读出现有 failure_count，+1 后再写
                        cur = self.store.conv_conn(get_current_platform()).cursor()
                        cur.execute(
                            "SELECT failure_count FROM blocked_conversations WHERE chat_id = ?",
                            (chat_id.rstrip("="),),
                        )
                        row = cur.fetchone()
                        fc = (int(row["failure_count"]) if row and row["failure_count"] else 0) + 1
                        self.store._blacklist_repo.add_blocked_conversation(
                            chat_id=chat_id,
                            chat_name=conv_info.get("chat_name") or "",
                            chat_type=chat_type or "p2p",
                            reason=f"飞书跨租户p2p第 {fc} 次失败（重试冷却）",
                            source="feishu_external_chat_unsendable",
                            last_error=err_text,
                            failure_count=fc,
                        )

                    # 连续失败 ≥ 3 → 升级为永久黑名单
                    if fc >= 3:
                        self.store._blacklist_repo.upgrade_to_permanent_block(chat_id)
                        logger.warning(
                            "[永久黑名单] chat_id=%s 连续失败 %d 次，升级为永久黑名单（err=%s）",
                            chat_id, fc, err_text[:200],
                        )
                    else:
                        # 设临时冷却 1 小时
                        cooldown_until = (datetime.now() + timedelta(hours=1)).isoformat()
                        cur = self.store.conv_conn(get_current_platform()).cursor()
                        cur.execute(
                            "UPDATE blocked_conversations SET cooldown_until = ? WHERE chat_id = ?",
                            (cooldown_until, chat_id.rstrip("=")),
                        )
                        self.store.conv_conn(get_current_platform()).commit()
                        logger.info(
                            "[跨租户冷却] chat_id=%s 失败计数=%d，冷却至 %s（1h 后可重试）",
                            chat_id, fc, cooldown_until,
                        )
                except Exception as be:
                    logger.error("写入 blocked_conversations 失败: %s", be)

            logger.error("发送消息失败: %s", e)
            if is_unsendable_external:
                return {
                    "error": f"发送失败（已加入黑名单，后续跳过重试）: {err_text[:300]}",
                    "degraded": True,
                    "chat_id": chat_id,
                }
            return {"error": f"发送失败: {e}"}
        finally:
            # 恢复跨租户外部好友场景的 fallback 状态
            self.dws._disable_bot_fallback = prev_disable

        # 持久化机器人回复（is_bot=1），后续在消息记录页可区分真人/机器人
        if self.store:
            try:
                from src.models import Message
                conv_info = self.store._conversation_repo.get_conversation(chat_id)
                chat_name = (conv_info.get("chat_name") or "") if conv_info else ""
                content = text or (file_path or media_id or "")
                bot_msg = Message(
                    msg_id=f"bot_{reply_uuid}",
                    chat_id=chat_id,
                    chat_type=chat_type,
                    chat_name=chat_name,
                    sender_id="ai",
                    sender_name="AI助手",
                    content=content,
                    msg_type=msg_type,
                    timestamp=datetime.now(),
                    role="assistant",
                    is_bot=True,
                )
                self.store._message_repo.save_message(bot_msg, "assistant")
            except Exception as e:
                logger.warning("保存机器人回复记录失败: %s", e)

        return {
            "success": True,
            "uuid": reply_uuid,
            "msg_type": msg_type,
            "text": text,
            "media_id": media_id,
            "file_path": file_path,
        }
