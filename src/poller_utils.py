"""Poller 纯函数工具集。

从 poller.py 中提取无状态/纯函数，方便单测与复用。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import logging

if TYPE_CHECKING:
    from src.models import Message

logger = logging.getLogger(__name__)


def detect_chat_type(conv: dict) -> str:
    """判定会话类型：single（单聊）、group（群聊）、other（与系统账号的单聊）。

    判定优先级：
    1. 群聊永远归类为 group（即使里面有系统机器人，如"呆滞物料预警"群）
    2. 单聊 + 系统/应用发送者 → other（用户在和系统机器人/应用对话）
    3. 单聊 + 真人 → single

    注意：singleChat 字段缺失时（常见于外部好友或某些组织内成员会话），
    尝试从消息发送者数量推断，而不是默认归为群聊，避免漏掉消息。
    """
    single_chat = conv.get("singleChat")
    title = conv.get("title", "")
    sender = conv.get("sender") or conv.get("senderName") or ""

    if single_chat is False:
        return "group"
    if single_chat is True:
        # 群聊判断优先：标题有群聊特征 → group
        if _is_group_like_title(title):
            return "group"
        # 单聊检查发送者（系统账号 → other）
        if _is_system_sender_name(sender):
            return "other"
        return "single"
    if single_chat is None:
        # singleChat 缺失：用标题推断
        if _is_group_like_title(title):
            return "group"
        # 外部好友等场景：默认单聊
        if _is_system_sender_name(sender):
            return "other"
        return "single"
    return "single"


def _is_group_like_title(title: str) -> bool:
    """标题含群聊特征词视为群聊。"""
    group_keywords = ["群", "组", "团队", "部门", "项目", "讨论组", "room"]
    return any(kw in title for kw in group_keywords)


def _is_system_sender_name(sender_name: str) -> bool:
    """判断发送者名称是否为系统/应用账号。"""
    if not sender_name:
        return False
    keywords = [
        "系统", "System", "钉钉小秘书", "钉钉官方", "应用",
        "bot", "Bot", "机器人", "审批", "考勤", "公告",
    ]
    return any(kw in sender_name for kw in keywords)


def match_notification_signature(
    content: str | None,
    sender_id: str | None,
    patterns: list[str] | None = None,
    sender_ids: list[str] | None = None,
) -> str | None:
    """窄签名层：判断一条文本消息是否命中「机器生成通知」签名，应被静默跳过。

    仅作为结构层（msgType + 系统发送者）之后的最后安全网，用于拦截
    「以真人身份推送的纯文本通知」。返回命中原因字符串（签名或 sender_id），
    未命中返回 None。

    设计要点：
    - 只匹配【机器生成物】（固定模板 / 报错栈），不匹配人类散文，避免误伤真人消息。
    - 发送者 ID 精确匹配优先（结构性静音，最强）。
    - 非法正则按普通子串兜底，避免配置错误导致整轮静默逻辑失效。
    """
    if sender_ids and sender_id and sender_id in sender_ids:
        return f"sender_id:{sender_id}"
    if patterns and content:
        for pat in patterns:
            if not pat:
                continue
            try:
                if re.search(pat, content):
                    return pat
            except re.error as _exc:
                logger.warning(f"match_notification_signature: swallowed exception: {_exc}")
                # 容错：非法正则当作普通子串匹配
                if pat in content:
                    return pat
    return None


# 「已读」回执类系统通知（机器生成）锚定关键词。
# 只锚定【机器生成物】（方括号前缀 / 回执 / 英文），绝不匹配人类散文。
# 反例（真人消息，必须放行）："他们已读不回了"、"标记已读后过连天就忘了"。
# 形态参考钉钉通知风格（同 "[语音通话] 通话时长 48秒" 的方括号前缀约定）。
READ_RECEIPT_KEYWORDS = (
    "[已读]", "[已读回执]", "已读回执", "消息已读回执", "已读通知",
    "read receipt", "readreceipt", "read_receipt", "msgread", "msg_read",
)


def is_read_receipt_content(content: str | None) -> bool:
    """判断一条文本是否为「已读」回执类系统通知（机器生成，不应触发回复）。

    仅锚定机器生成形态（方括号前缀 / 回执 / 英文），避免误伤真人消息。
    与 call/edit/recall 同族（见 poller_core_parse._classify_by_content_keywords），
    用于把这类通知在结构层归为 read_receipt 类型并交由 skip_msg_types 过滤，
    或作为 _send_reply 发送前硬闸门的兜底判定。
    """
    if not content:
        return False
    c = content.lower()
    return any(kw in c for kw in READ_RECEIPT_KEYWORDS)


def is_polite_message(content: str) -> bool:
    """判断消息是否是「纯」礼貌/感谢/确认/结束语消息。

    仅当整条消息除礼貌词外不含其它实质内容时才视为纯礼貌消息。
    """
    if not content:
        return False
    text = content.strip()
    if not text:
        return False

    polite_words = [
        "谢谢", "感谢", "辛苦了", "辛苦", "谢了", "多谢", "感恩",
        "收到", "好的", "明白", "知道了", "了解", "清楚了", "没问题",
        "OK", "ok", "再见", "拜拜", "晚安", "先忙", "收工", "下班",
    ]
    fillers = [
        "了", "呢", "吧", "啊", "嘛", "吗", "哦", "嗯", "额", "诶", "哈",
        "呀", "啦", "老板", "老大", "亲", "哥", "姐", "总", "师傅",
        "老师", "同学", "朋友",
    ]

    remaining = text
    for kw in polite_words + fillers:
        remaining = re.sub(re.escape(kw), "", remaining, flags=re.IGNORECASE)
    stripped = re.sub(r"[\s，。！？、,.!?~～\-—()（）""''」』:：;；]", "", remaining)
    return stripped == ""


def wrap_image_block(content: str) -> str:
    """给合并组中的图片 OCR 内容加显式分隔块。"""
    if not content:
        return content
    return (
        "———— 图片识别内容 ————\n"
        f"{content}\n"
        "———— 图片识别内容结束 ————"
    )


def combine_message_group(
    group: list["Message"],
    *,
    is_polite: Callable[[str], bool] = is_polite_message,
    wrap_img: Callable[[str], str] = wrap_image_block,
) -> "Message":
    """将一组消息合并为一条消息。

    参数：
        group: 按时间排序的消息列表
        is_polite: 判断单条消息是否为纯礼貌消息的函数
        wrap_img: 图片内容包装函数
    """
    if len(group) == 1:
        return group[0]

    first = group[0]
    types = {getattr(m, "msg_type", "") for m in group}
    has_image = "image" in types
    has_other = any(t != "image" for t in types)

    filtered_contents = []
    polite_count = 0
    for m in group:
        c = getattr(m, "content", "") or ""
        if not c:
            continue
        if is_polite(c):
            polite_count += 1
            continue
        if getattr(m, "msg_type", "") == "image":
            filtered_contents.append(wrap_img(c))
        else:
            filtered_contents.append(c)

    if not filtered_contents:
        return first

    from src.models import Message

    merged_content = "\n".join(filtered_contents)
    merged_type = "mixed" if (has_image and has_other) else first.msg_type
    # 保留所有原始 msg_id，防止合并后未标记的消息被重复处理
    all_msg_ids = [m.msg_id for m in group if m.msg_id]
    merged = Message(
        msg_id=first.msg_id,
        chat_id=first.chat_id,
        chat_type=first.chat_type,
        chat_name=first.chat_name,
        sender_id=first.sender_id,
        sender_name=first.sender_name,
        content=merged_content,
        msg_type=merged_type,
        timestamp=first.timestamp,
        raw={**getattr(first, "raw", {}), "merged": True, "merged_original_ids": all_msg_ids},
    )
    return merged


def merge_consecutive_messages(
    messages: list["Message"],
    window_seconds: int = 60,
    *,
    is_polite: Callable[[str], bool] = is_polite_message,
    wrap_img: Callable[[str], str] = wrap_image_block,
    semantic_threshold: float = 0.75,
) -> list["Message"]:
    """合并同一人在短时间窗口内的连续消息。

    先按 (chat_id, sender) 分组，避免跨会话排序导致同一会话消息被拆散。

    Args:
        semantic_threshold: 语义相似度阈值（0~1）。超出时间窗口但语义相似的消息也会合并。
                            设为 0 或 None 可禁用语义合并。
    """
    if not messages:
        return messages

    groups = defaultdict(list)
    for msg in messages:
        key = (getattr(msg, "chat_id", ""), getattr(msg, "sender_id", "") or getattr(msg, "sender_name", ""))
        groups[key].append(msg)

    # 语义合并所需的两个函数：任一不可用即整体禁用。
    # 此前用独立的 embedding_available 布尔位当开关，两者可能不同步——
    # 且类型检查器无法据此收窄 Optional，后续任何漏判都会直接在 None 上调用。
    # 现改为「函数句柄本身即开关」，判定与使用一处对齐。
    embed_func: Callable[[str], Any] | None = None
    cosine_func: Callable[[Any, Any], float] | None = None
    if semantic_threshold and semantic_threshold > 0:
        try:
            from src import semantic as semantic_index
            if semantic_index.get_embedding_client() is not None:
                embed_func = semantic_index._embed
                cosine_func = semantic_index.cosine
        except Exception:
            logger.debug("语义嵌入模块不可用，跳过去重合并")

    merged: list["Message"] = []
    for key, group in groups.items():
        group.sort(key=lambda m: m.timestamp)
        current_group: list["Message"] = []
        last_embedding = None

        for msg in group:
            if not current_group:
                current_group.append(msg)
                if embed_func is not None:
                    content = getattr(msg, "content", "") or ""
                    last_embedding = embed_func(content) if content else None
                continue

            last = current_group[-1]
            time_diff = (msg.timestamp - last.timestamp).total_seconds()
            same_sender = (getattr(msg, "sender_id", "") == getattr(last, "sender_id", "")
                           or getattr(msg, "sender_name", "") == getattr(last, "sender_name", ""))
            same_chat = getattr(msg, "chat_id", "") == getattr(last, "chat_id", "")

            should_merge = False
            if same_chat and same_sender:
                if time_diff <= window_seconds:
                    should_merge = True
                elif (embed_func is not None and cosine_func is not None
                      and semantic_threshold and last_embedding):
                    content = getattr(msg, "content", "") or ""
                    if content:
                        msg_embedding = embed_func(content)
                        if msg_embedding:
                            similarity = cosine_func(last_embedding, msg_embedding)
                            if similarity >= semantic_threshold:
                                should_merge = True

            if should_merge:
                current_group.append(msg)
                if embed_func is not None:
                    content = getattr(msg, "content", "") or ""
                    if content:
                        last_embedding = embed_func(content)
            else:
                merged.append(combine_message_group(current_group, is_polite=is_polite, wrap_img=wrap_img))
                current_group = [msg]
                if embed_func is not None:
                    content = getattr(msg, "content", "") or ""
                    last_embedding = embed_func(content) if content else None

        if current_group:
            merged.append(combine_message_group(current_group, is_polite=is_polite, wrap_img=wrap_img))

    return merged
