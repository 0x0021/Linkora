"""孤儿图片清理测试（修复 data/tmp_images 磁盘泄漏）。

覆盖：
- ``purge_orphan_images`` 直接删除相对路径图片 + 路径越界护栏
- ``cleanup_old_messages`` 删除旧消息时连带清理磁盘图片
- ``delete_message`` 撤回消息时连带清理磁盘图片
- ``delete_conversations`` 批量删会话时连带清理磁盘图片
- D10 回归：会话库嵌套在 data/conversations/ 下时，图片根仍在 data/tmp_images，
  须显式传 base_dir 才能正确定位（旧默认 base=库父目录/tmp_images 会漏删）
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from pathlib import Path

from src.memory.image_cleanup import purge_orphan_images
from src.memory.sqlite_store import SQLiteStore
from src.memory.platform_context import platform_scope
from src.models import Message
from src.paths import set_data_dir, clear_path_overrides, data_path


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


@pytest.fixture
def data_dir_at_tmp(tmp_path):
    """把 data_path 根重定向到 tmp_path，使仓库代码里的 data_path('tmp_images')
    指向测试沙箱而非真实 data/，保证测试 hermetic 且修复真实图根定位。"""
    set_data_dir(str(tmp_path))
    try:
        yield tmp_path
    finally:
        clear_path_overrides()


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
        safe = _tmp_images_dir(tmp_db_path) / "dingtalk/keep.png"
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text("keep")
        purge_orphan_images(str(tmp_db_path), ["../../../etc/hosts"])
        assert safe.exists()


class TestCleanupOldMessagesOrphan:
    def test_cleanup_old_messages_purges_images(self, data_dir_at_tmp):
        tmp_path = data_dir_at_tmp
        store = _make_store(tmp_path / "linkora.db")
        img_root = data_path("tmp_images")
        rel = "dingtalk/acct/chat1/ocr_old.png"
        f = img_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("old-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-old", rel), role="user")
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
    def test_delete_message_purges_image(self, data_dir_at_tmp):
        tmp_path = data_dir_at_tmp
        store = _make_store(tmp_path / "linkora.db")
        img_root = data_path("tmp_images")
        rel = "dingtalk/acct/chat1/ocr_del.png"
        f = img_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("del-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-del", rel), role="user")
            assert store._message_repo.delete_message("msg-del") is True
        assert not f.exists()


class TestDeleteConversationsOrphan:
    def test_delete_conversations_purges_images(self, data_dir_at_tmp):
        tmp_path = data_dir_at_tmp
        store = _make_store(tmp_path / "link_path" / "linkora.db")
        img_root = data_path("tmp_images")
        rel = "dingtalk/acct/chat1/ocr_bulk.png"
        f = img_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("bulk-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-bulk", rel), role="user")
            store._conversation_repo.delete_conversations(["chat-001"], platform="dingtalk")
        assert not f.exists()


class TestD10NestedConversationDb:
    """D10 回归：会话库位于 data/conversations/<platform>__<acct>.db 时，
    旧默认 base=<库父目录>/tmp_images 会指向 data/conversations/tmp_images（错误），
    导致活跃删除不回收图片。修复后显式传 base_dir=data_path('tmp_images')，
    图片根 data/tmp_images 下的文件应被正确删除。"""

    def test_delete_message_nested_db_uses_data_tmp_images_root(self, data_dir_at_tmp):
        tmp_path = data_dir_at_tmp
        # 模拟真实布局：会话库嵌套在 conversations/ 下
        nested_db = tmp_path / "conversations" / "dingtalk__4c11dc67bc0226ad.db"
        nested_db.parent.mkdir(parents=True, exist_ok=True)
        store = _make_store(nested_db)
        # 真实图片根：data/tmp_images（与库父目录无关）
        img_root = data_path("tmp_images")
        rel = "dingtalk/acct/chat1/ocr_nested.png"
        f = img_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("nested-img")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-nested", rel), role="user")
            assert store._message_repo.delete_message("msg-nested") is True
        assert not f.exists()

    def test_cleanup_old_messages_nested_db_uses_data_tmp_images_root(self, data_dir_at_tmp):
        tmp_path = data_dir_at_tmp
        nested_db = tmp_path / "conversations" / "dingtalk__4c11dc67bc0226ad.db"
        nested_db.parent.mkdir(parents=True, exist_ok=True)
        store = _make_store(nested_db)
        img_root = data_path("tmp_images")
        rel = "dingtalk/acct/chat1/ocr_nested_old.png"
        f = img_root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("nested-old")
        with platform_scope("dingtalk"):
            store._message_repo.save_message(_fake_msg("msg-nested-old", rel), role="user")
            conn = store.conv_conn("dingtalk")
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE msg_id = ?",
                ((datetime.now() - timedelta(days=100)).isoformat(), "msg-nested-old"),
            )
            conn.commit()
            res = store._message_repo.cleanup_old_messages(retention_days=0)
        assert res["deleted_count"] == 1
        assert not f.exists()
