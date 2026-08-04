"""批量删除消息端点（/api/messages/batch-delete）测试。

背景：前端 messages.js 的多选批量删除一直调用本端点，但后端从未实现
（前端功能死链，点击必 404）。本文件覆盖：
- repo 层 delete_conversations 的真实删除行为（tmp 库验证四表联动清空）
- Web 端点参数校验（空数组 / 超上限 / 正常调用透传）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ============ repo 层：delete_conversations 真实行为 ============

class TestDeleteConversations:
    def _make_store(self, tmp_path):
        from src.memory.conversation_repo import ConversationRepo
        from src.memory.sqlite_store import SQLiteStore

        store = SQLiteStore(db_path=str(tmp_path / "main.db"))
        repo = ConversationRepo(store)
        conn = store.conv_conn("dingtalk")
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) "
            "VALUES (?,?,?,?)", ("c1", "group", "t", "t"),
        )
        cur.execute(
            "INSERT INTO conversations (chat_id, chat_type, created_at, updated_at) "
            "VALUES (?,?,?,?)", ("c2", "single", "t", "t"),
        )
        cur.execute(
            "INSERT INTO messages (chat_id, msg_id, content, created_at) VALUES (?,?,?,?)",
            ("c1", "m1", "hi", "t"),
        )
        cur.execute(
            "INSERT INTO messages (chat_id, msg_id, content, created_at) VALUES (?,?,?,?)",
            ("c2", "m2", "keep", "t"),
        )
        cur.execute(
            "INSERT INTO conversation_summaries (chat_id, summary_text, older_boundary_msg_id, "
            "covered_count, generation, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("c1", "s", "m0", 1, 1, "t", "t"),
        )
        cur.execute(
            "INSERT INTO dedup_messages (msg_id, chat_id, processed_at) VALUES (?,?,?)",
            ("d1", "c1", "t"),
        )
        conn.commit()
        return store, repo

    def test_deletes_conversation_and_related_rows(self, tmp_path):
        store, repo = self._make_store(tmp_path)
        n = repo.delete_conversations(["c1"], "dingtalk")
        assert n == 1

        cur = store.conv_conn("dingtalk").cursor()
        assert cur.execute("SELECT COUNT(*) FROM conversations WHERE chat_id='c1'").fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM messages WHERE chat_id='c1'").fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM conversation_summaries WHERE chat_id='c1'").fetchone()[0] == 0
        assert cur.execute("SELECT COUNT(*) FROM dedup_messages WHERE chat_id='c1'").fetchone()[0] == 0
        # 未选的会话不受影响
        assert cur.execute("SELECT COUNT(*) FROM conversations WHERE chat_id='c2'").fetchone()[0] == 1
        assert cur.execute("SELECT COUNT(*) FROM messages WHERE chat_id='c2'").fetchone()[0] == 1

    def test_empty_ids_noop(self, tmp_path):
        store, repo = self._make_store(tmp_path)
        assert repo.delete_conversations([], "dingtalk") == 0
        cur = store.conv_conn("dingtalk").cursor()
        assert cur.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2


# ============ Web 端点：/api/messages/batch-delete ============

@pytest.fixture
def client():
    """先完整初始化 web.api（get_store 在 api.py:44 已定义，早于 856 行
    conversations 注册），再独立挂载 conversations router。

    测试通过 patch("web.api.get_store") 生效——conversations 用 _api.get_store()
    动态访问模块属性，patch 路径稳定，天然抗全量顺序污染（不注入 fake 模块）。
    """
    import web.api  # noqa: F401  # 完整初始化，避免 conversations 顶部 import 触发循环
    from fastapi import FastAPI
    from web.routers.conversations import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestBatchDeleteEndpoint:
    def test_success(self, client):
        repo = MagicMock()
        repo.delete_conversations.return_value = 3
        store = MagicMock()
        store._conversation_repo = repo
        with patch("web.api.get_store", return_value=store):
            resp = client.post(
                "/api/messages/batch-delete", json={"chat_ids": ["c1", "c2", "c3"]}
            )
        assert resp.status_code == 200
        assert resp.json() == {"deleted": 3}
        assert repo.delete_conversations.call_args.args[0] == ["c1", "c2", "c3"]

    def test_empty_chat_ids_rejected(self, client):
        resp = client.post("/api/messages/batch-delete", json={"chat_ids": []})
        assert resp.status_code == 400

    def test_missing_chat_ids_rejected(self, client):
        resp = client.post("/api/messages/batch-delete", json={})
        assert resp.status_code == 400

    def test_too_many_chat_ids_rejected(self, client):
        ids = [f"c{i}" for i in range(201)]
        resp = client.post("/api/messages/batch-delete", json={"chat_ids": ids})
        assert resp.status_code == 400
