"""当前用户消息包装层：从 _build_user_message 抽出的「truncate + 群前缀」独立模块。

设计：纯函数，零 agent 依赖。
- 不读 self，不写 self
- 只接收 Message 对象 + truncate 长上限，返回最终「待发送」字符串
- 不修改原 Message 字段
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models import Message

logger = logging.getLogger(__name__)


def wrap_incoming_message(message: "Message", *, truncate_fn, max_chars: int = 1000) -> str:
    """包装本次收到的 user 消息：

    1. 若 content 是字符串，调用 truncate_fn 截断到 max_chars（默认 1000）。
    2. 若 chat_type == "group"，加 `[群]{chat_name}:` 前缀。
    3. 返回最终字符串（不再含原 Message 引用）。

    truncate_fn 注入是为了：
    - 单测时可注入 FakeTruncate 验证调用参数
    - 不强制依赖 agent 实例上的 _truncate_long_message（已抽到 history.py）

    非字符串 content（如图片/文件 marker）保持原样，不做截断。
    """
    content = message.content
    if isinstance(content, str):
        content = truncate_fn(content, max_chars=max_chars)
    if message.chat_type == "group":
        content = f"[群]{message.chat_name}:{content}"
    return content
