"""群消息「是否显式 @ 本人」判定（工作时间外待办专用）。

🔴 为什么需要（2026-11-26 实测事故）：
主轮询链路（``poller_strategy`` / ``poller_core_discovery``）对群消息有
「未 @ 我不处理」门控，但 off-hours 待办扫描器从本地库和平台侧拉全量消息时
**没有这道门**：群里 @ 别人的消息（如「传递涂油脂么？这个咋用图纸传递？
@同事甲」）只要工作时间外没人亲自回，就会被误建待办 + 误发回执。

钉钉 dws 返回的消息实测**没有 ``atUsers`` / ``atUserIds`` 结构化字段**
（2026-11-26 拉取「工业线-ECN信息拉通和问题沟通群」24 条消息逐字段核对），
@ 只存在于正文文本里（形如 ``@同事甲``、``@875981221``）。故本模块以文本
@ 提及为唯一可靠判据，与轮询器 ``_is_at_me`` 的口径一致。

判据严格：只认**显式 @ 提及**。正文里提到某人名字但不带 @，不算叫他——
误建待办会对外误发「稍后回复」回执，代价高于漏建。
"""

from __future__ import annotations

import re
from typing import Any

# @all / @所有人 / @全体成员 都算 @ 了我。
_AT_ALL_WORDS = frozenset({"all", "所有人", "全体成员", "全体", "everyone"})

# 钉钉正文里的 @ 提及形如 @同事甲 / @875981221 / @DDn1xxx。
# 中文姓名 2~4 字为主，上限 32 兼容 openDingTalkId 与长账号。
_AT_MENTION_RE = re.compile(r"@([\w\u4e00-\u9fff\-]{1,32})")


def is_at_me(
    content: str,
    *,
    self_ids: frozenset[str] | set[str] = frozenset(),
    self_names: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """正文中是否显式 @ 了本人（含 @所有人）；纯函数，无网络/DB 依赖。

    Args:
        content: 消息正文（原始文本，含 ``@姓名`` 标记）。
        self_ids: 本人 ID 集合（userId / openDingTalkId），用于识别 ``@<ID>``。
        self_names: 本人姓名 / 别名集合（正式姓名 + 昵称），用于识别 ``@姓名``。

    Returns:
        True = 至少一处 @ 命中本人或 @所有人；False = 没有 @ 或只 @ 了别人。
    """
    text = str(content or "")
    if not text:
        return False
    lowered_ids = {str(x).strip().lower() for x in self_ids if str(x).strip()}
    lowered_names = {str(x).strip().lower() for x in self_names if str(x).strip()}
    for match in _AT_MENTION_RE.finditer(text):
        token = match.group(1).strip()
        low = token.lower()
        if low in _AT_ALL_WORDS or low in lowered_ids or low in lowered_names:
            return True
    return False


def is_group_at_me(
    row: Any,
    *,
    self_ids: frozenset[str] | set[str] = frozenset(),
    self_names: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """从扫描行里取正文并判定 @；非群聊直接返回 True（单聊本就是对我说的）。"""
    if not hasattr(row, "get"):
        return True
    chat_type = str(row.get("chat_type") or "").strip().lower()
    if chat_type != "group":
        return True
    return is_at_me(
        str(row.get("content") or ""), self_ids=self_ids, self_names=self_names,
    )
