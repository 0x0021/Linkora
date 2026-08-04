"""MessageRepo.backfill_missing_image_path 回归测试。

锁定 A1 生产 bug 的修复：原 conversations 路由用未定义变量 ``conn`` + 错误主键 ``id``
直写主库 ``store.conn``，导致图片路径磁盘兜底回填永远不生效、错误被 ``except: pass`` 吞掉。
修复后回填走 MessageRepo，落在正确的 conv_conn 会话库，且幂等（仅当 image_path 为空时写入）。
"""
from __future__ import annotations

from src.memory.sqlite_store import SQLiteStore


def _insert_message(store: SQLiteStore, platform: str, msg_id: str,
                    msg_type: str = "image", image_path: str = "") -> None:
    conn = store.conv_conn(platform)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO messages
           (chat_id, chat_type, msg_id, sender_id, sender_name, content, msg_type,
            timestamp, role, created_at, is_archived, image_path)
           VALUES (?, 'single', ?, 'u1', '张三', 'x', ?, datetime('now'), 'user',
                   datetime('now'), 0, ?)""",
        ("c1", msg_id, msg_type, image_path),
    )
    conn.commit()


def _get_image_path(store: SQLiteStore, platform: str, msg_id: str):
    conn = store.conv_conn(platform)
    cur = conn.cursor()
    cur.execute("SELECT image_path FROM messages WHERE msg_id = ?", (msg_id,))
    row = cur.fetchone()
    return row[0] if row else None


def test_backfill_writes_to_conv_conn(tmp_path):
    """空 image_path 的消息回填后，应在 conv_conn 会话库生效（而非主库）。"""
    store = SQLiteStore(str(tmp_path / "linkora.db"))
    platform = "dingtalk"
    _insert_message(store, platform, "m1", image_path="")  # 空 -> 应被填入

    n = store._message_repo.backfill_missing_image_path("m1", "img/a.png", platform)

    assert n == 1
    assert _get_image_path(store, platform, "m1") == "img/a.png"


def test_backfill_idempotent_does_not_overwrite(tmp_path):
    """image_path 已存在时，回填不应覆盖既有真值。"""
    store = SQLiteStore(str(tmp_path / "linkora.db"))
    platform = "dingtalk"
    _insert_message(store, platform, "m2", image_path="already.png")

    n = store._message_repo.backfill_missing_image_path("m2", "img/b.png", platform)

    assert n == 0
    assert _get_image_path(store, platform, "m2") == "already.png"


def test_backfill_unknown_msg_id_noop(tmp_path):
    """不存在的 msg_id 不应报错，也不应影响任何行。"""
    store = SQLiteStore(str(tmp_path / "linkora.db"))
    n = store._message_repo.backfill_missing_image_path("nope", "x.png", "dingtalk")
    assert n == 0


def test_backfill_default_platform_falls_back(tmp_path):
    """缺省 platform 时回落当前平台上下文（默认 dingtalk），仍应命中。"""
    store = SQLiteStore(str(tmp_path / "linkora.db"))
    _insert_message(store, "dingtalk", "m3", image_path="")

    n = store._message_repo.backfill_missing_image_path("m3", "img/c.png")

    assert n == 1
    assert _get_image_path(store, "dingtalk", "m3") == "img/c.png"
