"""per-account 会话连接模型测试：文件隔离 + 表存在 + 同账号复用。"""

import os

import pytest

import src.memory.account_identity as ai
import src.memory.sqlite_store as ss
from src.memory.sqlite_store import SQLiteStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    # 固定账号键，避免真实 shell-out
    monkeypatch.setattr(ai, "resolve_account_id", lambda p, fb=None: f"{p}:acct-A")
    db = tmp_path / "linkora.db"
    s = SQLiteStore(str(db))
    yield s
    s.close()


def _tables(conn):
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def test_conv_db_file_created(store):
    conn = store.conv_conn("feishu")
    expected = os.path.join(store._conv_root, "feishu__" + __import__("hashlib").sha256(b"feishu:acct-A").hexdigest()[:16] + ".db")
    assert os.path.exists(expected)
    for t in ("conversations", "messages", "conversation_summaries",
             "external_friends", "blocked_conversations", "dedup_messages"):
        assert t in _tables(conn)


def test_different_platform_different_file(store):
    feishu_conn = store.conv_conn("feishu")
    ding_conn = store.conv_conn("dingtalk")
    assert feishu_conn is not ding_conn
    # 飞书库里的会话在钉钉库里查不到
    feishu_conn.execute(
        "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) VALUES (?,?,?,?)",
        ("oc_test", "single", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    feishu_conn.commit()
    row = ding_conn.execute("SELECT * FROM conversations WHERE chat_id=?", ("oc_test",)).fetchone()
    assert row is None


def test_same_account_reuses_connection_same_thread(store):
    c1 = store.conv_conn("feishu")
    c2 = store.conv_conn("feishu")
    assert c1 is c2  # 同线程同账号 → 同一连接（缓存）


def test_account_switch_opens_new_db(store, monkeypatch):
    # 第一次飞书=账号A
    c_a = store.conv_conn("feishu")
    c_a.execute(
        "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) VALUES (?,?,?,?)",
        ("oc_a", "single", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    c_a.commit()
    # 切换飞书账号=账号B
    monkeypatch.setattr(ai, "resolve_account_id", lambda p, fb=None: f"{p}:acct-B")
    c_b = store.conv_conn("feishu")
    assert c_b is not c_a
    # 账号B 看不到账号A 的数据
    assert c_b.execute("SELECT * FROM conversations WHERE chat_id=?", ("oc_a",)).fetchone() is None


def test_empty_platform_does_not_migrate(store):
    """空/未知 platform 调用 conv_conn 不应把主库全量数据盲拷进无前缀孤儿库。

    回归守护：过渡期曾因 platform_prefix=None 时 `ELSE 拷全表` 产生 __<hash>.db 垃圾库。
    """
    # 主库预置飞书会话数据
    store.conn.execute(
        "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) VALUES (?,?,?,?)",
        ("oc_seed", "single", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    store.conn.commit()
    # 空 platform 调用 conv_conn（fixture 下 account_id = ":acct-A"）
    empty_conn = store.conv_conn("")
    # 无前缀库文件被创建，但保持为空（未继承主库数据）
    assert empty_conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    # 主库数据不受影响
    assert (
        store.conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE chat_id=?", ("oc_seed",)
        ).fetchone()[0]
        == 1
    )
