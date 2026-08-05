"""per-account 会话连接模型测试：文件隔离 + 表存在 + 同账号复用。"""

import os

import pytest

import src.memory.account_identity as ai
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


def test_migrate_prefixes_is_class_level():
    """_MIGRATE_PLATFORM_PREFIXES 必须是类属性，不能退化成 __init__ 局部变量。

    回归守护：该常量曾被误缩进在 SQLiteStore.__init__ 内成为局部变量，导致
    sqlite_store_conn._migrate_main_to_conv 访问 self._MIGRATE_PLATFORM_PREFIXES
    必然 AttributeError；而调用方 except Exception 会把它吞成一条 warning，
    使「主库→会话库首次引导迁移」长期静默失效。
    """
    assert isinstance(SQLiteStore._MIGRATE_PLATFORM_PREFIXES, dict)
    assert set(SQLiteStore._MIGRATE_PLATFORM_PREFIXES) == {"feishu", "dingtalk", "wecom"}


def test_known_platform_migrates_main_db_data(store):
    """已知平台首次建会话库时，应把主库中该平台的既有会话迁移过去。

    与 test_empty_platform_does_not_migrate 互为正反面：后者只能证明「不该迁的没迁」，
    无法发现「该迁的也没迁」——_MIGRATE_PLATFORM_PREFIXES 缺失时它依然全绿。
    """
    store.conn.execute(
        "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) VALUES (?,?,?,?)",
        ("oc_seed", "single", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    # dingtalk 前缀会话，用于验证按平台前缀过滤而非全量盲拷
    store.conn.execute(
        "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) VALUES (?,?,?,?)",
        ("cid_other", "group", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    store.conn.commit()

    conn = store.conv_conn("feishu")
    rows = {
        r[0]
        for r in conn.execute("SELECT chat_id FROM conversations").fetchall()
    }
    assert "oc_seed" in rows, "飞书前缀会话应被迁入账号会话库"
    assert "cid_other" not in rows, "非本平台前缀会话不应被迁入"
