"""孤儿图片清理测试（修复 data/tmp_images 磁盘泄漏）。

覆盖：
- ``purge_orphan_images`` 直接删除相对路径图片 + 路径越界护栏
- ``cleanup_old_messages`` 删除旧消息时连带清理磁盘图片
- ``delete_message`` 撤回消息时连带清理磁盘图片
- ``delete_conversations`` 批量删会话时连带清理磁盘图片
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.memory.image_cleanup import purge_orphan_images
from src.memory.sqlite_store import SQLiteStore
from src.memory.platform_context import platform_scope
from src.models import Message


def _make_store(tmp_db_path):
    store = SQLiteStore(db_path=str(tmp_db_path))
    store.init_db()
    return store


def _tmp_images_dir(tmp_db_path) -> Path:
    return Path(tmp_db_path).resolve().parent / "tmp_images"


def _fake_msg(msg_id: str, image_rel: str, chat_id: str = "chat-001") -> Message:
    return Message(
        msg_id=msg_id,
        chat_id=chat_id,
        chat_type="single",
        chat_name="张三",
        sender_id="sender-001",
        sender_name="张三",
        content="图片消息",
        msg_type="image",
        timestamp=datetime(2026, 7, 7, 12, 0, 0),
        image_path=image_rel,
        raw={},
    )


class TestPurgeOrphanImages:
    def test_deletes_existing_image(self, tmp_db_path):
        img_dir = _tmp_images_dir(tmp_db_path)
        rel = "dingtalk/acct/chat1/ocr_x.png"
        f = img_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("img")
        assert purge_orphan_images(str(tmp_db_path), [rel]) == 1
        assert not f.exists()

    def test_skips_missing_image(self, tmp_db_path):
        assert purge_orphan_images(str(tmp_db_path), ["dingtalk/acct/chat1/missing.png"]) == 0

    def test_path_traversal_guard(self, tmp_db_path):
        # 越界路径（含 ../）不应删除任何文件，且不应崩溃；base 内的安全文件不受影响
        safe = _tmp_images_dir(tmp_db_path) / "dingtalk/keep.png"
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text("keep")
        purge_orphan_images(str(tmp_db_path), ["../../../etc/hosts"])
        assert safe.exists()


class TestCleanupOldMessagesOrphan:
    def test_cleanup_old_messages_purges_images(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        img_dir = _tmp_images_dir(tmp_db_path)
        rel = "dingtalk/acct/chat1/ocr_old.png"
        f = img_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("old-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-old", rel), role="user")
            # 回拨 created_at 到 100 天前，确保被 retention 命中
            conn = store.conv_conn("dingtalk")
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE msg_id = ?",
                ((datetime.now() - timedelta(days=100)).isoformat(), "msg-old"),
            )
            conn.commit()
            res = store._message_repo.cleanup_old_messages(retention_days=0)
        assert res["deleted_count"] == 1
        assert not f.exists()


class TestDeleteMessageOrphan:
    def test_delete_message_purges_image(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        img_dir = _tmp_images_dir(tmp_db_path)
        rel = "dingtalk/acct/chat1/ocr_del.png"
        f = img_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("del-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-del", rel), role="user")
            assert store._message_repo.delete_message("msg-del") is True
        assert not f.exists()


class TestDeleteConversationsOrphan:
    def test_delete_conversations_purges_images(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        img_dir = _tmp_images_dir(tmp_db_path)
        rel = "dingtalk/acct/chat1/ocr_bulk.png"
        f = img_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("bulk-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-bulk", rel), role="user")
            store._conversation_repo.delete_conversations(["chat-001"], platform="dingtalk")
        assert not f.exists()
