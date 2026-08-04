from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Message:
    msg_id: str
    chat_id: str
    chat_type: str
    chat_name: Optional[str]
    sender_id: str
    sender_name: str
    content: str
    msg_type: str
    timestamp: datetime
    raw: dict = field(default_factory=dict)
    role: str = ""  # user / assistant / system
    image_path: str = ""  # 持久化图片路径（相对 data/tmp_images）
    is_bot: bool = False  # True=机器人/AI发送, False=真人
