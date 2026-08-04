"""P2-3 回归：get_conversation_history 回放历史时必须剔除残留的 OCR 占位符。

背景：OCR 异步未完成时，消息在 DB 里可能长期停留在 "[图片识别中...]" 占位符。
历史上下文回放若把该占位符喂给 LLM，会造成答非所问 / 上下文断链。sqlite_store
在构建历史消息时应防御性剔除。
"""

from __future__ import annotations

from datetime import datetime

from src.memory.sqlite_store import SQLiteStore
from src.models import Message


def _make_store(tmp_db_path):
    store = SQLiteStore(db_path=str(tmp_db_path))
    store.init_db()
    return store


class TestGetConversationHistoryPlaceholderStrip:
    def test_ocr_placeholder_stripped_from_history(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        msg = Message(
            msg_id="x1",
            chat_id="c1",
            chat_type="single",
            chat_name="g",
            sender_id="s1",
            sender_name="张三",
            content="看图\n[图片识别中...]",
            msg_type="image",
            timestamp=datetime.now(),
            role="user",
        )
        store._message_repo.save_message(msg)

        hist = store._message_repo.get_conversation_history("c1")
        assert hist, "应返回历史"
        assert "[图片识别中...]" not in hist[0].content
        assert hist[0].content.strip() == "看图"
