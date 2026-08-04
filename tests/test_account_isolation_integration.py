"""账号级会话隔离 —— 集成测试（task #31）。

验证：
  1. 同一平台下不同登录账号 → 物理隔离的会话 DB 文件（文件名 sha256 账号键）。
  2. 账号 A 写入的会话/消息，账号 B 的查询【完全不可见】（0 跨账号命中）；
     反之亦然。
  3. 会话数据不再落在主库（主库 conversations/messages 表为空）。

设计前提：store 的 per-account 连接按 (线程, 平台) 缓存，进程内账号稳定
（re-login 视为重启 → 新进程新 store）。因此两个账号用【两个线程】模拟两个
独立登录会话，每个线程解析到各自的账号，互不串扰。

不依赖 torch / faiss（仅 sqlite3 + store），可直接跑。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.memory import account_identity
from src.memory.platform_context import with_platform
from src.memory.sqlite_store import SQLiteStore


# 线程级「当前账号」：模拟每个登录会话（线程）解析到不同账号。
_TLS = threading.local()


def _fake_resolve(platform: str, fallback_corp_id=None) -> str:
    return getattr(_TLS, "account", "feishu:unknown")


class AccountIsolationIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="linkora_iso_")
        self.main_db = os.path.join(self.tmp, "linkora.db")
        self._patcher = patch.object(account_identity, "resolve_account_id", _fake_resolve)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ──────────────── helpers ────────────────

    def _session_work(self, account: str, chat_id: str, content: str,
                      results: dict) -> None:
        """在一个线程内模拟一次完整登录会话：解析账号 → 建 store → 写 → 读。"""
        _TLS.account = account
        store = SQLiteStore(self.main_db)
        store.init_db()
        with with_platform("feishu"):
            # 写会话 + 消息（走与线上一致的路由路径）
            store._conversation_repo.upsert_conversation(chat_id, chat_id, "single", platform="feishu")
            conn = store.conv_conn("feishu")
            conn.execute(
                """INSERT INTO messages
                   (chat_id, chat_type, msg_id, sender_id, sender_name, content,
                    msg_type, timestamp, role, is_bot, created_at)
                   VALUES (?, 'single', ?, 'u1', 'owner', ?, 'text',
                           '2026-07-28T00:00:00', 'assistant', 0,
                           '2026-07-28T00:00:00')""",
                (chat_id, f"m_{chat_id}", content),
            )
            conn.commit()

            # 本账号视角：能读到自己的数据
            self_conv = store._conversation_repo.get_conversation(chat_id, platform="feishu")
            self_msg = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE chat_id = ?", (chat_id,)
            ).fetchone()["c"]

        results["path"] = store._conv_db_path("feishu", account)
        results["self_conv_found"] = self_conv is not None
        results["self_msg_count"] = int(self_msg)
        # 暴露 store 供后续跨账号查询验证
        results["store"] = store
        results["account"] = account

    def _cross_check(self, store: SQLiteStore, thread_account: str,
                     other_chat_id: str, key: str, out: dict) -> None:
        """在「特定账号上下文」线程内，用该账号的会话 store 查询对方 chat_id。

        关键：conv_conn 按 (线程, 平台) 缓存，且解析当前线程的账号。只有在本线程
        _TLS.account 设为该账号时，conv_conn 才会路由到该账号的库，从而真实验证
        「对方数据不可见」。
        """
        _TLS.account = thread_account
        with with_platform("feishu"):
            conv = store._conversation_repo.get_conversation(other_chat_id, platform="feishu")
            msg = store.conv_conn("feishu").execute(
                "SELECT COUNT(*) AS c FROM messages WHERE chat_id = ?", (other_chat_id,)
            ).fetchone()["c"]
        out[key] = (conv is not None, int(msg))

    # ──────────────── tests ────────────────

    def test_cross_account_zero_hit_isolation(self) -> None:
        res_a: dict = {}
        res_b: dict = {}

        # 账号 A 登录会话（独立线程）
        _TLS.account = "feishu:acctA"
        t_a = threading.Thread(target=self._session_work,
                               args=("feishu:acctA", "oc_chatA", "账号A的私密消息", res_a))
        # 账号 B 登录会话（独立线程）
        _TLS.account = "feishu:acctB"
        t_b = threading.Thread(target=self._session_work,
                               args=("feishu:acctB", "oc_chatB", "账号B的私密消息", res_b))
        t_a.start(); t_a.join()
        t_b.start(); t_b.join()

        path_a = res_a["path"]
        path_b = res_b["path"]

        # (1) 物理文件必须不同
        self.assertNotEqual(path_a, path_b, "不同账号应使用不同的会话 DB 文件")
        self.assertTrue(os.path.exists(path_a), "账号A会话库应已创建")
        self.assertTrue(os.path.exists(path_b), "账号B会话库应已创建")

        # (2) 各账号仅见自己数据
        self.assertTrue(res_a["self_conv_found"])
        self.assertEqual(res_a["self_msg_count"], 1)
        self.assertTrue(res_b["self_conv_found"])
        self.assertEqual(res_b["self_msg_count"], 1)

        # (3) 跨账号 0 命中：在各自账号上下文里，用本会话 store 查询「对方」chat_id
        cross: dict = {}
        # 账号 A 上下文（_TLS.account=acctA）→ 查账号 B 的 chatB → 必须 None/0
        ta = threading.Thread(target=self._cross_check,
                              args=(res_a["store"], "feishu:acctA", "oc_chatB", "a_to_b", cross))
        # 账号 B 上下文（_TLS.account=acctB）→ 查账号 A 的 chatA → 必须 None/0
        tb = threading.Thread(target=self._cross_check,
                              args=(res_b["store"], "feishu:acctB", "oc_chatA", "b_to_a", cross))
        ta.start(); tb.start(); ta.join(); tb.join()

        a_found, a_msg = cross["a_to_b"]
        b_found, b_msg = cross["b_to_a"]
        self.assertFalse(a_found, "账号A绝不应看到账号B的会话（0 跨账号命中）")
        self.assertEqual(a_msg, 0, "账号A的消息计数必须排除账号B")
        self.assertFalse(b_found, "账号B绝不应看到账号A的会话（0 跨账号命中）")
        self.assertEqual(b_msg, 0, "账号B的消息计数必须排除账号A")

        # (4) 主库不应承载会话数据（隔离到 per-account 库）
        main = sqlite3.connect(self.main_db)
        main.row_factory = sqlite3.Row
        main_conv = main.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()["c"]
        main_msg = main.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        main.close()
        self.assertEqual(main_conv, 0, "主库 conversations 表应为空（会话已隔离）")
        self.assertEqual(main_msg, 0, "主库 messages 表应为空（会话已隔离）")

    def test_conv_db_path_is_account_namespaced(self) -> None:
        """会话库文件名对账号键取 sha256，同账号稳定、换账号必变。"""
        _TLS.account = "feishu:acctA"
        s1 = SQLiteStore(self.main_db)
        p1 = s1._conv_db_path("feishu", "feishu:acctA")

        _TLS.account = "feishu:acctB"
        s2 = SQLiteStore(self.main_db)
        p2 = s2._conv_db_path("feishu", "feishu:acctB")

        self.assertNotEqual(p1, p2)
        # 文件名不含账号明文（仅 sha256 前 16 位），且以平台名前缀
        self.assertNotIn("acctA", os.path.basename(p1))
        self.assertNotIn("acctB", os.path.basename(p2))
        self.assertTrue(os.path.basename(p1).startswith("feishu__"))
        self.assertTrue(os.path.basename(p2).startswith("feishu__"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
