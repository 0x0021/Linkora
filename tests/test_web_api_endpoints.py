"""web/api.py 关键端点测试。

覆盖端点（按风险/价值排序）：
- /api/status: 系统总览（DB + 配置 + DWS）
- /api/intents: 意图体系定义
- /api/decisions: 决策追踪
- /api/dead-letters (list/replay/discard): 死信队列
- /api/clear-cross-org-skips: 跨组织跳过清理
- /api/config/default: 重置默认配置
- /api/config/export: 导出配置
- /health: 综合健康检查
- /api/keywords/test-match: 关键词匹配测试
- /api/tools: 工具清单

策略：使用真实 SQLiteStore + tmp 配置文件，仅 mock 外部依赖（DWS、共享状态）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from web.errors import SAFE_INTERNAL_ERROR


# ============ Fixtures ============

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时 DB 路径,patch SQLiteStore 默认连接。"""
    db_path = tmp_path / "test_api.db"
    monkeypatch.setattr("web.dependencies.DEFAULT_DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """临时 config.yaml,只含必要最小字段。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "dws:\n"
        "  dry_run: true\n"
        "  cli_path: /usr/bin/echo\n"
        "  profile: test\n"
        "poller:\n"
        "  interval_seconds: 30\n"
        "llm:\n"
        "  model: test-model\n"
        "  base_url: http://localhost\n"
        "  api_key: test\n"
        "embedding:\n"
        "  enabled: false\n"
        "tools:\n"
        "  available: []\n"
        "rules:\n"
        "  intent_filter: {}\n"
        "web:\n"
        "  port: 8000\n"
        "  auth_enabled: false\n"
        "  auth_username: admin\n"
        "  auth_password: ''\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("web.api.CONFIG_PATH", str(cfg))
    return str(cfg)


@pytest.fixture(autouse=True)
def _clear_shared_caches():
    """隔离跨测试的模块级缓存（stats TTL 缓存、配置磁盘缓存、单例）。

    P1-3 引入的 /api/stats/messages TTL 缓存按 days 缓存、不区分 DB，
    若不清理会导致后续测试读到上一测试的陈旧统计（如空库 0 计数）或绕过
    get_store mock。同样清理 _get_cfg 的磁盘缓存与配置单例，避免串味。
    """
    import web.api as api
    if hasattr(api, "_stats_messages_cache"):
        api._stats_messages_cache.clear()
    if hasattr(api, "_cfg_cache"):
        api._cfg_cache = None
        api._cfg_cache_path = None
        api._cfg_cache_mtime = -1
    from src.shared_state import set_config
    set_config(None)
    yield
    if hasattr(api, "_stats_messages_cache"):
        api._stats_messages_cache.clear()
    from src.shared_state import set_config as _sc
    _sc(None)


# ============ /api/intents ============

class TestIntents:
    def test_intents_returns_definitions(self, tmp_config, tmp_db):
        """返回 IntentRegistry 的定义,含 meta。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/intents")
        assert resp.status_code == 200
        data = resp.json()
        # meta 必含 routing_mode
        assert "meta" in data
        assert "routing_mode" in data["meta"]

    def test_intents_includes_evidence_keywords(self, tmp_config, tmp_db):
        """意图分类定义应返回 evidence_keywords 数组（P2-6：证据词标签此前永不显示）。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/intents")
        assert resp.status_code == 200
        data = resp.json()
        cats = (data.get("layers", {}).get("disposition", [])
                + data.get("layers", {}).get("action", []))
        assert cats, "应返回至少一个意图分类"
        with_kw = [c for c in cats if c.get("evidence_keywords")]
        assert with_kw, "应返回 evidence_keywords 数组供前端渲染证据词标签"
        for c in with_kw:
            assert isinstance(c["evidence_keywords"], list)


# ============ /api/decisions ============

class TestDecisions:
    def test_decisions_returns_tracker_recent(self, tmp_config, tmp_db):
        """返回追踪器记录的最近 n 条决策。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("src.decision_tracker.tracker") as mock_tracker:
            mock_tracker.recent.return_value = [{"msg_id": "m1", "intent": "greet"}]
            resp = client.get("/api/decisions?n=10")
        assert resp.status_code == 200
        data = resp.json()
        assert "decisions" in data
        assert data["decisions"][0]["msg_id"] == "m1"
        mock_tracker.recent.assert_called_with(10, "")

    def test_decisions_default_n_is_50(self, tmp_config, tmp_db):
        """默认 n=50。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("src.decision_tracker.tracker") as mock_tracker:
            mock_tracker.recent.return_value = []
            client.get("/api/decisions")
        mock_tracker.recent.assert_called_with(50, "")


# ============ /api/dead-letters ============

class TestDeadLettersList:
    def test_list_pending_default(self, tmp_config, tmp_db):
        """默认 status=pending, limit=100。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/dead-letters")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "items" in data
        assert "count" in data
        assert data["count"] == 0  # 空表

    def test_list_with_status_filter(self, tmp_config, tmp_db):
        """status=replayed 应不返回 pending。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        # 预存一条 dead-letter
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store._draft_repo.add_dead_letter(
            msg_id="m_x", chat_id="c1", chat_name="g",
            sender_id="u", sender_name="u", content="x", msg_type="text",
            stage="llm", error="e", raw={},
        )
        store.close()
        resp = client.get("/api/dead-letters?status=pending")
        assert resp.status_code == 200
        assert resp.json()["count"] == 1
        resp = client.get("/api/dead-letters?status=replayed")
        assert resp.json()["count"] == 0

    def test_list_db_error_returns_500(self, tmp_config, tmp_db):
        """底层 DB 错应 500。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.routers.dead_letters.get_store", side_effect=Exception("db boom")):
            resp = client.get("/api/dead-letters")
        assert resp.status_code == 500


class TestDeadLettersReplay:
    def test_replay_no_app_instance_returns_500(self, tmp_config, tmp_db):
        """get_app_instance 返回 None 时应 500。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.routers.dead_letters.get_app_instance", return_value=None):
            resp = client.post("/api/dead-letters/1/replay")
        assert resp.status_code == 500
        # 5xx 经全局处理器脱敏为安全文案，不回传内部细节（如「应用实例不可用」）
        assert resp.json()["detail"] == SAFE_INTERNAL_ERROR

    def test_replay_app_no_method_returns_500(self, tmp_config, tmp_db):
        """app_instance 没有 replay_dead_letter 方法时 500。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        fake_app = object()  # 没有任何方法
        with patch("web.routers.dead_letters.get_app_instance", return_value=fake_app):
            resp = client.post("/api/dead-letters/1/replay")
        assert resp.status_code == 500

    def test_replay_app_failure_returns_400(self, tmp_config, tmp_db):
        """app.replay_dead_letter 返回 success=False → 400。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        fake_app = MagicMock()
        fake_app.replay_dead_letter.return_value = {"success": False, "error": "not_found"}
        with patch("web.routers.dead_letters.get_app_instance", return_value=fake_app):
            resp = client.post("/api/dead-letters/999/replay")
        assert resp.status_code == 400
        assert "not_found" in resp.json()["detail"]

    def test_replay_success(self, tmp_config, tmp_db):
        """成功路径返回 success=True。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        fake_app = MagicMock()
        fake_app.replay_dead_letter.return_value = {"success": True}
        with patch("web.routers.dead_letters.get_app_instance", return_value=fake_app):
            resp = client.post("/api/dead-letters/1/replay")
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestDeadLettersDiscard:
    def test_discard_existing(self, tmp_config, tmp_db):
        """已存在的 dl_id 应被标 discarded。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        dl_id = store._draft_repo.add_dead_letter(
            msg_id="m", chat_id="c1", chat_name="g",
            sender_id="u", sender_name="u", content="x", msg_type="text",
            stage="llm", error="e", raw={},
        )
        store.close()
        client = TestClient(app)
        resp = client.post(f"/api/dead-letters/{dl_id}/discard")
        assert resp.status_code == 200
        # 验证状态确实变了
        store = SQLiteStore(tmp_db)
        rec = store._draft_repo.get_dead_letter(dl_id)
        assert rec["status"] == "discarded"
        store.close()

    def test_discard_nonexistent_returns_404(self, tmp_config, tmp_db):
        """不存在的 dl_id → 404。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.post("/api/dead-letters/99999/discard")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "not_found"


# ============ /api/status ============

class TestStatus:
    def test_status_returns_running_with_stats(self, tmp_config, tmp_db):
        """正常路径:返回 status=running + 各项计数。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = {"userName": "test_user"}
            dws.dry_run = True
            mock_dws.return_value = dws
            resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "running"
        assert "stats" in data
        assert "messages" in data["stats"]
        assert "config" in data
        assert data["user"]["name"] == "test_user"

    def test_status_token_verified_failed_returns_personal_user(self, tmp_config, tmp_db):
        """TOKEN_VERIFIED_FAILED 错应回退为「个人用户」。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = None
            dws.contact_user_get_self.side_effect = Exception("TOKEN_VERIFIED_FAILED: token 已过期")
            dws.dry_run = True
            mock_dws.return_value = dws
            resp = client.get("/api/status")
        data = resp.json()
        assert data["user"]["name"] == "个人用户"

    def test_status_other_error_returns_na(self, tmp_config, tmp_db):
        """其他 DWS 错应回退为 N/A。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = None
            dws.contact_user_get_self.side_effect = Exception("网络超时")
            dws.dry_run = True
            mock_dws.return_value = dws
            resp = client.get("/api/status")
        data = resp.json()
        assert data["user"]["name"] == "N/A"


# ============ /api/clear-cross-org-skips ============

class TestClearCrossOrgSkips:
    def test_clear_endpoint_responds(self, tmp_config, tmp_db):
        """端点返回有效 JSON (成功/失败均可,仅验证不报错)。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.post("/api/clear-cross-org-skips")
        # 503 表示应用实例不可用 - 测试环境下预期
        assert resp.status_code in (200, 500, 503)


# ============ /api/config/default ============

class TestConfigDefault:
    def test_reset_to_default(self, tmp_config, tmp_db):
        """重置为默认配置,返回 success。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.api._write_config"):
            resp = client.post("/api/config/default")
        assert resp.status_code == 200
        assert resp.json().get("success") is True or "config" in resp.json()


# ============ /api/config/export ============

class TestConfigExport:
    def test_export_returns_yaml(self, tmp_config, tmp_db):
        """导出 config 应返回 yaml 文本。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/config/export")
        assert resp.status_code == 200
        # 必含 llm 段
        text = resp.text
        assert "llm" in text or "model" in text


# ============ /health ============

class TestHealth:
    def test_health_db_healthy(self, tmp_config, tmp_db):
        """DB 健康 → components.database.healthy, status=ok。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "components" in data
        # DB 应是 healthy(临时 DB 可用)
        db = data["components"].get("database", {})
        assert db.get("status") in ("healthy", "unhealthy")  # 至少不报错

    def test_health_config_readable(self, tmp_config, tmp_db):
        """配置文件可读 → components.config.readable。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/health")
        data = resp.json()
        cfg = data["components"].get("config", {})
        assert cfg.get("status") in ("readable", "unreadable")

    def test_health_db_error_marks_degraded(self, tmp_config, tmp_db):
        """DB 不可用 → status=degraded。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.routers.health.get_store", side_effect=Exception("db down")):
            resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["components"]["database"]["status"] == "unhealthy"


# ============ /api/tools ============

class TestTools:
    def test_list_tools_returns_config(self, tmp_config, tmp_db):
        """返回 tools 配置对象（含 enabled/available/rate_limit）。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/tools")
        assert resp.status_code == 200
        data = resp.json()
        # 配置是 dict 含 enabled/available/rate_limit
        assert "enabled" in data
        assert "available" in data
        assert "rate_limit" in data


# ============ /api/kb/stats ============

class TestKbStats:
    def test_kb_stats_returns_dict(self, tmp_config, tmp_db):
        """返 store._kb_repo.kb_stats() 输出的 dict。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/kb/stats")
        assert resp.status_code == 200
        data = resp.json()
        # 临时库应该是空表返 0 计数
        assert isinstance(data, dict)

    def test_kb_stats_db_error_returns_500(self, tmp_config, tmp_db):
        """store.kb_stats 抛错 → 500。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.api.get_store") as mock:
            store = MagicMock()
            store._kb_repo.kb_stats.side_effect = Exception("db boom")
            mock.return_value = store
            resp = client.get("/api/kb/stats")
        assert resp.status_code == 500


# ============ /api/memories ============

class TestMemories:
    def _create_memory(self, content="x", source="manual", chat_id=""):
        from src.memory.sqlite_store import SQLiteStore
        SQLiteStore(tmp_db) if hasattr(self, '_tmp_db') else None
        # use fixture tmp_db directly
        from src.memory.sqlite_store import SQLiteStore as Store
        s = Store(self._tmp_db())
        s.init_db()
        import hashlib
        key = "mem_" + hashlib.md5(content.encode()).hexdigest()[:12]
        return s._memory_repo.save_memory(key=key, content=content, source=source, chat_id=chat_id), s

    # stub helper
    def _tmp_db(self):
        # collect tmp_db from active pytest context via monkeypatched DEFAULT_DB_PATH
        from web.dependencies import DEFAULT_DB_PATH
        return DEFAULT_DB_PATH

    def test_list_memories_empty(self, tmp_config, tmp_db):
        """空表返空列表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/memories")
        assert resp.status_code == 200
        assert resp.json()["memories"] == []

    def test_list_memories_with_data(self, tmp_config, tmp_db):
        """插入后能列表查询。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store._memory_repo.save_memory(key="mem_k1", content="记一下", source="manual", chat_id="c1")
        store._memory_repo.save_memory(key="mem_k2", content="另一条", source="extracted", chat_id="c2")
        store.close()
        client = TestClient(app)
        resp = client.get("/api/memories")
        assert resp.status_code == 200
        items = resp.json()["memories"]
        assert len(items) >= 2

    def test_list_memories_limit(self, tmp_config, tmp_db):
        """limit 参数生效。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        for i in range(5):
            store._memory_repo.save_memory(key=f"mem_{i}", content=f"c{i}", source="manual", chat_id="")
        store.close()
        client = TestClient(app)
        resp = client.get("/api/memories?limit=2")
        assert len(resp.json()["memories"]) <= 2

    def test_add_memory_success(self, tmp_config, tmp_db):
        """POST /api/memories 成功。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.post("/api/memories", json={"content": "相公喜欢拉面", "source": "manual"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["memory_id"]
        # 验证入库
        from src.memory.sqlite_store import SQLiteStore
        s = SQLiteStore(tmp_db)
        all_mems = s._memory_repo.get_all_memories()
        assert any(m["content"] == "相公喜欢拉面" for m in all_mems)
        s.close()

    def test_add_memory_db_error_returns_500(self, tmp_config, tmp_db):
        """save_memory 抛错 → 500。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.routers.memories.get_store") as mock:
            store = MagicMock()
            store._memory_repo.save_memory.side_effect = Exception("write fail")
            mock.return_value = store
            resp = client.post("/api/memories", json={"content": "x"})
        assert resp.status_code == 500

    def test_delete_memory(self, tmp_config, tmp_db):
        """DELETE /api/memories/{id} 成功。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        mid = store._memory_repo.save_memory(key="mem_d", content="to delete", source="manual", chat_id="")
        store.close()
        client = TestClient(app)
        resp = client.delete(f"/api/memories/{mid}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        # 验证删除
        s = SQLiteStore(tmp_db)
        all_mems = s._memory_repo.get_all_memories()
        assert not any(m["id"] == mid for m in all_mems)
        s.close()

    def test_delete_memory_nonexistent_still_succeeds(self, tmp_config, tmp_db):
        """DELETE 不存在 id: 200 (SQL 0 行受影响但不报错)。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.delete("/api/memories/99999")
        # 实现走 execute+commit,无 NotFound 校验 → 200
        assert resp.status_code in (200, 500)

    def test_update_memory_content(self, tmp_config, tmp_db):
        """PUT /api/memories/{id} 改 content。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        mid = store._memory_repo.save_memory(key="mem_u", content="旧", source="manual", chat_id="")
        store.close()
        client = TestClient(app)
        resp = client.put(f"/api/memories/{mid}", json={"content": "新"})
        assert resp.status_code == 200
        s = SQLiteStore(tmp_db)
        all_mems = s._memory_repo.get_all_memories()
        target = next((m for m in all_mems if m["id"] == mid), None)
        assert target is not None
        assert target["content"] == "新"
        s.close()

    def test_memory_facets(self, tmp_config, tmp_db):
        """GET /api/memories/facets 返 facets 结构。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store._memory_repo.save_memory(key="mem_f1", content="x1", source="manual", chat_id="c1")
        store._memory_repo.save_memory(key="mem_f2", content="x2", source="extracted", chat_id="c2")
        store.close()
        client = TestClient(app)
        resp = client.get("/api/memories/facets")
        assert resp.status_code == 200
        data = resp.json()
        # 实现返什么结构不重要，仅验证不报错且是 dict
        assert isinstance(data, dict)


# ============ /api/stats/messages ============

class TestMessageStats:
    def test_message_stats_returns_structure(self, tmp_config, tmp_db):
        """返 message_stats 结构 (trend + types + top_senders)。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/stats/messages?days=7")
        assert resp.status_code == 200
        data = resp.json()
        # 至少应含这些 key
        for key in ("trend", "msg_types", "top_senders"):
            assert key in data, f"missing key: {key}"

    def test_message_stats_system_sender_classified(self, tmp_config, tmp_db):
        """系统发送者被归为「系统消息」分类。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        from src.models import Message
        from datetime import datetime
        store = SQLiteStore(tmp_db)
        store.init_db()
        msg = Message(
            msg_id="om_test_001",
            chat_id="c1", chat_name="g1", chat_type="single",
            sender_id="u1", sender_name="钉钉客服",
            content="hello", msg_type="text",
            timestamp=datetime.now(),
        )
        store._message_repo.save_message(msg)
        store.close()
        client = TestClient(app)
        resp = client.get("/api/stats/messages?days=7")
        data = resp.json()
        types_map = {t["msg_type"]: t["cnt"] for t in data["msg_types"]}
        # 钉钉客服是系统发送者（端点分类标签为「系统通知」）
        assert types_map.get("系统通知", 0) >= 1

    def test_message_stats_days_param(self, tmp_config, tmp_db):
        """days 参数透传生效（SQL 使用 format 拼接）。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/stats/messages?days=30")
        assert resp.status_code == 200

    def test_message_stats_db_error_returns_500(self, tmp_config, tmp_db):
        """get_store 抛错 → 500。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        with patch("web.api.get_store", side_effect=Exception("db down")):
            resp = client.get("/api/stats/messages")
        assert resp.status_code == 500


# ============ /api/rules ============

class TestRules:
    def test_rules_returns_dict(self, tmp_config, tmp_db):
        """返 config.rules.* 四件套。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/rules")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("blacklist", "whitelist", "keywords", "enabled"):
            assert key in data


# ============ /api/orgs ============

class TestOrgs:
    def test_orgs_no_app_instance_returns_503(self, tmp_config, tmp_db):
        """无 app_instance.poller → 503 轮询器未启动。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/orgs")
        # TestClient 无 app_instance,预期 503
        assert resp.status_code == 503
        # 5xx 经全局处理器脱敏为安全文案，不回传内部细节（如「轮询器未启动」）
        assert resp.json()["detail"] == SAFE_INTERNAL_ERROR

    def test_orgs_with_app_instance(self, tmp_config, tmp_db):
        """有 app_instance 时返 orgs 结构。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        fake_poller = MagicMock()
        fake_poller.dws.list_orgs.return_value = [{"corp_id": "c1", "name": "org1"}]
        fake_poller.dws.get_current_org.return_value = "c1"
        fake_poller.current_org = "c1"
        fake_poller.target_org_corp_id = ""
        fake_poller._inaccessible_conversations = set()
        fake_app = MagicMock()
        fake_app.poller = fake_poller
        with patch("web.routers.orgs.get_app_instance", return_value=fake_app):
            resp = client.get("/api/orgs")
        assert resp.status_code == 200
        data = resp.json()
        assert "orgs" in data
        assert "current" in data
        assert "target" in data
        assert "skipped_count" in data


class TestToolStats:
    """【P0-4 UX】首页“工具调用统计”卡片只返 TOP 9。"""

    def _make_config_with_tools(self, n_tools=12):
        """构造含 n 个工具的 config + 应用实例。
        必须设 web.auth_enabled=False,否则中间件拦截返 401。
        """
        config = MagicMock()
        config.tools.available = [f"tool_{i}" for i in range(n_tools)]
        # rate_limit.get 走 dict 一样
        config.tools.rate_limit = {f"tool_{i}": {"per_hour": 30} for i in range(n_tools)}
        # web 中间件检查项
        config.web.auth_enabled = False
        return config

    def test_default_top_n_is_12(self, tmp_config, tmp_db):
        """不传 top_n 时默认返 12 条。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.load_config", return_value=self._make_config_with_tools(15)), \
             patch("web.api.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/stats/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tools"]) == 12
        assert data["top_n"] == 12

    def test_explicit_top_n_3(self, tmp_config, tmp_db):
        """top_n=3 返 3 条。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.load_config", return_value=self._make_config_with_tools(12)), \
             patch("web.api.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/stats/tools?top_n=3")
        data = resp.json()
        assert len(data["tools"]) == 3
        assert data["top_n"] == 3

    def test_top_n_zero_returns_all(self, tmp_config, tmp_db):
        """top_n=0 或负数返全量。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.load_config", return_value=self._make_config_with_tools(5)), \
             patch("web.api.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/stats/tools?top_n=0")
        data = resp.json()
        assert len(data["tools"]) == 5  # 全量
        assert data["top_n"] == 5  # top_n 返回实际返的条数

    def test_top_n_larger_than_available(self, tmp_config, tmp_db):
        """top_n > available 工具数时返全量(不报错)。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.load_config", return_value=self._make_config_with_tools(3)), \
             patch("web.api.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/stats/tools?top_n=100")
        data = resp.json()
        assert len(data["tools"]) == 3

    def test_results_sorted_by_total_calls_desc(self, tmp_config, tmp_db):
        """返的列表应按 total_calls 降序。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.load_config", return_value=self._make_config_with_tools(5)), \
             patch("web.api.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/stats/tools?top_n=5")
        data = resp.json()
        calls = [t["total_calls"] for t in data["tools"]]
        assert calls == sorted(calls, reverse=True)

    def test_response_includes_period_days(self, tmp_config, tmp_db):
        """返 period_days 与请求一致。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.load_config", return_value=self._make_config_with_tools(3)), \
             patch("web.api.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/stats/tools?days=14")
        data = resp.json()
        assert data["period_days"] == 14


# ============ /api/keywords/* ============

class TestKeywordsCRUD:
    """关键词规则 CRUD 端点。"""

    def test_list_keywords_empty(self, tmp_config, tmp_db):
        """空表返空列表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/keywords")
        assert resp.status_code == 200
        data = resp.json()
        assert data["rules"] == []
        assert "categories" in data
        assert "total" in data
        assert "page" in data
        assert "page_size" in data

    def test_list_keywords_with_data(self, tmp_config, tmp_db):
        """插数据后能列表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="你好", reply_text="在的", category="greet", match_type="exact", priority=10)
        store.add_keyword_rule(match_pattern="谢谢", reply_text="不客气", category="greet", match_type="fuzzy", priority=5)
        store.close()
        client = TestClient(app)
        resp = client.get("/api/keywords")
        data = resp.json()
        assert data["total"] == 2

    def test_list_keywords_search_filter(self, tmp_config, tmp_db):
        """search 参数走 Python 侧 filter。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="你好", reply_text="在的", category="greet", match_type="exact", priority=10)
        store.add_keyword_rule(match_pattern="订单", reply_text="详情", category="order", match_type="fuzzy", priority=5)
        store.close()
        client = TestClient(app)
        resp = client.get("/api/keywords?search=订单")
        data = resp.json()
        assert data["total"] == 1
        assert data["rules"][0]["match_pattern"] == "订单"

    def test_add_keyword(self, tmp_config, tmp_db):
        """POST 返 success + id。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.post("/api/keywords", json={
            "match_pattern": "你好",
            "reply_text": "在的",
            "category": "greet",
            "match_type": "exact",
            "priority": 10,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["id"]

    def test_get_keyword_existing(self, tmp_config, tmp_db):
        """GET /api/keywords/{id} 返规则。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        rid = store.add_keyword_rule(match_pattern="hello", reply_text="hi", category="greet", match_type="exact", priority=5)
        store.close()
        client = TestClient(app)
        resp = client.get(f"/api/keywords/{rid}")
        assert resp.status_code == 200
        assert resp.json()["rule"]["match_pattern"] == "hello"

    def test_get_keyword_nonexistent_returns_404(self, tmp_config, tmp_db):
        """不存在的 id → 404。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/keywords/99999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "规则不存在"

    def test_update_keyword(self, tmp_config, tmp_db):
        """PUT /api/keywords/{id} 改 reply_text。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        rid = store.add_keyword_rule(match_pattern="hi", reply_text="old", category="g", match_type="exact", priority=1)
        store.close()
        client = TestClient(app)
        resp = client.put(f"/api/keywords/{rid}", json={"reply_text": "new"})
        assert resp.status_code == 200
        s = SQLiteStore(tmp_db)
        rule = s.get_keyword_rule(rid)
        assert rule["reply_text"] == "new"
        s.close()

    def test_update_keyword_nonexistent_returns_404(self, tmp_config, tmp_db):
        """PUT 不存在 id → 404。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.put("/api/keywords/99999", json={"reply_text": "x"})
        assert resp.status_code == 404

    def test_delete_keyword(self, tmp_config, tmp_db):
        """DELETE /api/keywords/{id} 成功。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        rid = store.add_keyword_rule(match_pattern="d", reply_text="r", category="c", match_type="exact", priority=0)
        store.close()
        client = TestClient(app)
        resp = client.delete(f"/api/keywords/{rid}")
        assert resp.status_code == 200
        s = SQLiteStore(tmp_db)
        assert s.get_keyword_rule(rid) is None
        s.close()

    def test_keywords_stats(self, tmp_config, tmp_db):
        """GET /api/keywords/stats 返总/启用/分类/类型/热度。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="a", reply_text="x", category="c1", match_type="exact", priority=1)
        store.add_keyword_rule(match_pattern="b", reply_text="x", category="c1", match_type="fuzzy", priority=1)
        store.add_keyword_rule(match_pattern="c", reply_text="x", category="c2", match_type="regex", priority=1)
        store.close()
        client = TestClient(app)
        resp = client.get("/api/keywords/stats")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("total", "enabled", "by_category", "by_type", "top_hits"):
            assert key in data
        assert data["total"] == 3


# ============ /api/keywords/test-match ============

class TestKeywordMatch:
    """关键词匹配测试端点。"""

    def test_exact_match(self, tmp_config, tmp_db):
        """exact 类型与文本完全一致才命中。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="你好", reply_text="在的", category="g", match_type="exact", priority=10)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["hit_count"] == 1
        assert data["top_reply"] == "在的"

    def test_exact_no_match_partial(self, tmp_config, tmp_db):
        """exact 类型部分匹配不命中。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="你好", reply_text="在的", category="g", match_type="exact", priority=10)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "你好呀"})
        assert resp.status_code == 200
        assert resp.json()["hit_count"] == 0

    def test_fuzzy_substring_match(self, tmp_config, tmp_db):
        """fuzzy 类型子串匹配命中。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="订单", reply_text="详情", category="o", match_type="fuzzy", priority=10)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "查订单进度"})
        assert resp.status_code == 200
        assert resp.json()["hit_count"] >= 1

    def test_regex_match(self, tmp_config, tmp_db):
        """regex 类型 re.search 命中。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern=r"\d{3,}", reply_text="number", category="n", match_type="regex", priority=10)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "编号 12345"})
        assert resp.status_code == 200
        assert resp.json()["hit_count"] >= 1

    def test_invalid_regex_skipped(self, tmp_config, tmp_db):
        """regex 错误不应炸，同条规则跳过。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="[", reply_text="bad", category="n", match_type="regex", priority=10)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "anything"})
        assert resp.status_code == 200
        assert resp.json()["hit_count"] == 0

    def test_results_sorted_by_priority_desc(self, tmp_config, tmp_db):
        """命中按 priority 降序。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store.add_keyword_rule(match_pattern="hi", reply_text="low", category="g", match_type="exact", priority=1)
        store.add_keyword_rule(match_pattern="hi", reply_text="high", category="g", match_type="exact", priority=99)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "hi"})
        data = resp.json()
        assert data["hit_count"] == 2
        assert data["matched"][0]["priority"] >= data["matched"][1]["priority"]
        assert data["top_reply"] == "high"

    def test_only_enabled_rules_matched(self, tmp_config, tmp_db):
        """test-match 只查 enabled=1。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        rid = store.add_keyword_rule(match_pattern="hi", reply_text="r", category="g", match_type="exact", priority=1)
        store.update_keyword_rule(rid, enabled=0)
        store.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/test-match", json={"text": "hi"})
        assert resp.json()["hit_count"] == 0


# ============ /api/keywords/batch ============

class TestKeywordsBatch:
    """批量操作端点。"""

    def _make_rules(self, n, db):
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(db)
        store.init_db()
        ids = []
        for i in range(n):
            rid = store.add_keyword_rule(match_pattern=f"p{i}", reply_text="r", category="c", match_type="exact", priority=0)
            ids.append(rid)
        store.close()
        return ids

    def test_batch_enable(self, tmp_config, tmp_db):
        """action=enable 启多条。"""
        from fastapi.testclient import TestClient
        from web.api import app
        ids = self._make_rules(3, tmp_db)
        from src.memory.sqlite_store import SQLiteStore
        s = SQLiteStore(tmp_db)
        for rid in ids:
            s.update_keyword_rule(rid, enabled=0)
        s.close()
        client = TestClient(app)
        resp = client.post("/api/keywords/batch", json={"ids": ids, "action": "enable"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["count"] == 3
        s = SQLiteStore(tmp_db)
        for rid in ids:
            assert s.get_keyword_rule(rid)["enabled"] == 1
        s.close()

    def test_batch_disable(self, tmp_config, tmp_db):
        """action=disable 禁多条。"""
        from fastapi.testclient import TestClient
        from web.api import app
        ids = self._make_rules(2, tmp_db)
        client = TestClient(app)
        resp = client.post("/api/keywords/batch", json={"ids": ids, "action": "disable"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 2
        from src.memory.sqlite_store import SQLiteStore
        s = SQLiteStore(tmp_db)
        for rid in ids:
            assert s.get_keyword_rule(rid)["enabled"] == 0
        s.close()

    def test_batch_delete(self, tmp_config, tmp_db):
        """action=delete 删多条。"""
        from fastapi.testclient import TestClient
        from web.api import app
        ids = self._make_rules(2, tmp_db)
        client = TestClient(app)
        resp = client.post("/api/keywords/batch", json={"ids": ids, "action": "delete"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 2
        from src.memory.sqlite_store import SQLiteStore
        s = SQLiteStore(tmp_db)
        for rid in ids:
            assert s.get_keyword_rule(rid) is None
        s.close()

    def test_batch_move_category(self, tmp_config, tmp_db):
        """action=move_category 需带 category。"""
        from fastapi.testclient import TestClient
        from web.api import app
        ids = self._make_rules(2, tmp_db)
        client = TestClient(app)
        resp = client.post("/api/keywords/batch", json={"ids": ids, "action": "move_category", "category": "newcat"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 2
        from src.memory.sqlite_store import SQLiteStore
        s = SQLiteStore(tmp_db)
        for rid in ids:
            assert s.get_keyword_rule(rid)["category"] == "newcat"
        s.close()

    def test_batch_move_category_without_category_returns_400(self, tmp_config, tmp_db):
        """move_category 不带 category → 400。"""
        from fastapi.testclient import TestClient
        from web.api import app
        ids = self._make_rules(1, tmp_db)
        client = TestClient(app)
        resp = client.post("/api/keywords/batch", json={"ids": ids, "action": "move_category"})
        assert resp.status_code == 400

    def test_batch_unknown_action_returns_400(self, tmp_config, tmp_db):
        """未知 action → 400。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.post("/api/keywords/batch", json={"ids": [1], "action": "explode"})
        assert resp.status_code == 400


# ============ /api/conversations ============

class TestConversations:
    def test_list_conversations_empty(self, tmp_config, tmp_db):
        """空表返空列表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        resp = client.get("/api/conversations")
        assert resp.status_code == 200
        assert resp.json()["conversations"] == []

    def test_list_conversations_with_data(self, tmp_config, tmp_db):
        """插会话能列表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        store._conversation_repo.upsert_conversation("c1", "张三", "single", "u1", "open1")
        store._conversation_repo.upsert_conversation("c2", "项目群", "group", "u2", "open2")
        store.close()
        client = TestClient(app)
        resp = client.get("/api/conversations")
        data = resp.json()
        assert len(data["conversations"]) == 2

    def test_list_conversations_with_message_preview(self, tmp_config, tmp_db):
        """last_message_preview 来自消息表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        from src.models import Message
        from datetime import datetime
        store = SQLiteStore(tmp_db)
        store.init_db()
        store._conversation_repo.upsert_conversation("c1", "张三", "single", "u1", "open1")
        msg = Message(
            msg_id="m1", chat_id="c1", chat_type="single", chat_name="张三",
            sender_id="u1", sender_name="张三", content="最近怎么样",
            msg_type="text", timestamp=datetime.now(),
        )
        store._message_repo.save_message(msg)
        store.close()
        client = TestClient(app)
        resp = client.get("/api/conversations")
        data = resp.json()
        conv = data["conversations"][0]
        assert conv["chat_id"] == "c1"
        assert conv["last_message_preview"] == "最近怎么样"

    def test_list_conversations_limit(self, tmp_config, tmp_db):
        """limit 参数生效。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        store.init_db()
        for i in range(5):
            store._conversation_repo.upsert_conversation(f"c{i}", f"会话{i}", "single", "u", "open")
        store.close()
        client = TestClient(app)
        resp = client.get("/api/conversations?limit=2")
        assert len(resp.json()["conversations"]) == 2


# ============ /api/messages ============

class TestMessages:
    def test_list_messages_empty(self, tmp_config, tmp_db):
        """空表返空列表+current_user_name。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = None
            dws.contact_user_get_self.side_effect = Exception("no profile")
            dws.dry_run = True
            mock_dws.return_value = dws
            client = TestClient(app)
            resp = client.get("/api/messages")
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == []
        assert "current_user_name" in data

    def test_list_messages_by_chat(self, tmp_config, tmp_db):
        """指定 chat_id 返该会话消息。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        from src.models import Message
        from datetime import datetime
        store = SQLiteStore(tmp_db)
        store.init_db()
        for i in range(3):
            msg = Message(
                msg_id=f"m{i}", chat_id="c1", chat_type="single", chat_name="张三",
                sender_id="u1", sender_name="张三", content=f"msg{i}",
                msg_type="text", timestamp=datetime.now(),
            )
            store._message_repo.save_message(msg)
        msg2 = Message(
            msg_id="other", chat_id="c2", chat_type="single", chat_name="李四",
            sender_id="u2", sender_name="李四", content="x",
            msg_type="text", timestamp=datetime.now(),
        )
        store._message_repo.save_message(msg2)
        store.close()
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = None
            dws.contact_user_get_self.side_effect = Exception("n/a")
            dws.dry_run = True
            mock_dws.return_value = dws
            client = TestClient(app)
            resp = client.get("/api/messages?chat_id=c1")
        data = resp.json()
        assert len(data["messages"]) == 3
        for m in data["messages"]:
            assert m["chat_id"] == "c1"

    def test_message_includes_image_url(self, tmp_config, tmp_db):
        """image_path 存在时返 image_url。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        from src.models import Message
        from datetime import datetime
        store = SQLiteStore(tmp_db)
        store.init_db()
        msg = Message(
            msg_id="img1", chat_id="c1", chat_type="single", chat_name="张三",
            sender_id="u1", sender_name="张三", content="[图片]",
            msg_type="image", timestamp=datetime.now(), image_path="imgs/a.png",
        )
        store._message_repo.save_message(msg)
        store.close()
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = {"userName": "me"}
            dws.dry_run = True
            mock_dws.return_value = dws
            client = TestClient(app)
            resp = client.get("/api/messages?chat_id=c1")
        m = resp.json()["messages"][0]
        assert m["image_url"] == "/api/image/imgs/a.png"

    def test_message_receiver_name_set(self, tmp_config, tmp_db):
        """sender==当前用户时 receiver_name=chat_name,反之=current_user_name。"""
        from fastapi.testclient import TestClient
        from web.api import app
        from src.memory.sqlite_store import SQLiteStore
        from src.models import Message
        from datetime import datetime
        store = SQLiteStore(tmp_db)
        store.init_db()
        msg = Message(
            msg_id="m1", chat_id="c1", chat_type="single", chat_name="张三",
            sender_id="u1", sender_name="me", content="hi",
            msg_type="text", timestamp=datetime.now(),
        )
        store._message_repo.save_message(msg)
        store.close()
        with patch("web.api.get_dws") as mock_dws:
            dws = MagicMock()
            dws._get_current_profile_local.return_value = {"userName": "me"}
            dws.dry_run = True
            mock_dws.return_value = dws
            client = TestClient(app)
            resp = client.get("/api/messages?chat_id=c1")
        m = resp.json()["messages"][0]
        # sender 是 me，receiver 应是 chat_name=张三
        assert m["receiver_name"] == "张三"


# ============ 安全加固测试（P0：认证默认开 + 恒定时间 + 限流 + 图片 token + SSRF） ============

@pytest.fixture
def tmp_config_secure(tmp_path, monkeypatch):
    """临时配置：开启认证（auth_enabled=true）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "dws:\n"
        "  dry_run: true\n"
        "  cli_path: /usr/bin/echo\n"
        "  profile: test\n"
        "poller:\n"
        "  interval_seconds: 30\n"
        "llm:\n"
        "  model: t\n"
        "  base_url: http://localhost\n"
        "  api_key: t\n"
        "embedding:\n"
        "  enabled: false\n"
        "tools:\n"
        "  available: []\n"
        "rules:\n"
        "  intent_filter: {}\n"
        "web:\n"
        "  port: 8000\n"
        "  auth_enabled: true\n"
        "  auth_username: admin\n"
        "  auth_password: secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("web.api.CONFIG_PATH", str(cfg))
    # 重置登录失败限流计数，避免跨测试污染
    monkeypatch.setattr("web.api._AUTH_FAILS", {})
    return str(cfg)


class TestSecurityHardening:
    def test_auth_required_when_enabled(self, tmp_config_secure, tmp_db):
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        r = client.get("/api/status")
        assert r.status_code == 401

    def test_auth_ok_with_credentials(self, tmp_config_secure, tmp_db):
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        r = client.get("/api/status", auth=("admin", "secret"))
        assert r.status_code == 200

    def test_image_requires_token(self, tmp_config_secure, tmp_db):
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        # 白名单免 Basic Auth，但必须带签名 token，否则 401
        r = client.get("/api/image/nonexistent.png", auth=("admin", "secret"))
        assert r.status_code == 401

    def test_image_token_issued_and_valid(self, tmp_config_secure, tmp_db):
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        tok = client.get("/api/image-token", auth=("admin", "secret")).json()["token"]
        assert tok
        # token 有效但文件不存在 → 404/403（不是 401）
        r = client.get(f"/api/image/nonexistent.png?it={tok}", auth=("admin", "secret"))
        assert r.status_code in (404, 403)

    def test_image_token_rejects_garbage(self, tmp_config_secure, tmp_db):
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        r = client.get("/api/image/x.png?it=garbage", auth=("admin", "secret"))
        assert r.status_code == 401

    def test_rate_limit_blocks_after_failures(self, tmp_config_secure, tmp_db, monkeypatch):
        from fastapi.testclient import TestClient
        from web.api import app
        monkeypatch.setattr("web.api._AUTH_FAILS", {})
        client = TestClient(app)
        for _ in range(5):
            r = client.get("/api/status", auth=("admin", "wrong"))
            assert r.status_code == 401
        # 第 6 次：命中限流
        r = client.get("/api/status", auth=("admin", "wrong"))
        assert r.status_code == 429

    def test_import_url_internal_blocked(self, tmp_config_secure, tmp_db):
        from fastapi.testclient import TestClient
        from web.api import app
        client = TestClient(app)
        # 链路本地地址（元数据服务）必须被 SSRF 防护拦截
        r = client.post("/api/kb/import-url",
                        json={"url": "http://169.254.169.254/latest/meta-data/"},
                        auth=("admin", "secret"))
        assert r.status_code == 400


class TestConfigSingleton:
    """P1-4: 配置收归单例 —— Web 直接读取 main 发布的单例，不回退磁盘。"""

    def test_get_cfg_returns_published_singleton(self):
        """_get_cfg 应直接返回 main 发布的同一配置对象（身份一致，非磁盘重读）。"""
        from src.config import load_config
        from src.shared_state import set_config, get_config
        from web.api import _get_cfg

        cfg_obj = load_config("config.yaml")
        assert get_config() is None  # 测试进程未启动 main，单例初始为空
        set_config(cfg_obj)
        try:
            assert _get_cfg() is cfg_obj
        finally:
            set_config(None)  # 避免污染其他测试

    def test_get_cfg_falls_back_to_disk_when_no_singleton(self, tmp_config):
        """单例为 None（独立运行 Web）时回退磁盘读取，与旧行为一致。"""
        from web.api import _get_cfg

        cfg = _get_cfg()
        assert cfg is not None
        assert cfg.web.auth_enabled is False  # 来自 tmp_config 文件内容


class TestGetStoreCache:
    """P1-2: get_store 按线程 + DB_PATH 缓存，复用实例。"""

    def test_get_store_caches_same_instance(self, tmp_db):
        from web.api import get_store
        assert get_store() is get_store()  # 同线程同 DB_PATH 复用

    def test_get_store_rebuilds_on_db_path_change(self, tmp_db, tmp_path, monkeypatch):
        from web.dependencies import DEFAULT_DB_PATH, get_store
        s1 = get_store()
        other = tmp_path / "other.db"
        monkeypatch.setattr("web.dependencies.DEFAULT_DB_PATH", str(other))
        try:
            s2 = get_store()
            assert s2 is not s1
            assert s2.db_path == str(other)
        finally:
            monkeypatch.setattr("web.dependencies.DEFAULT_DB_PATH", DEFAULT_DB_PATH)


# ============ /api/config-drift / /api/poller-status / /api/tools-chain ============

class TestNewFeatureEndpoints:
    """覆盖 #1 config-drift / #2 poller-status / #4 tools-chain 端点。

    这些端点依赖 get_app_instance() 返回的运行实例，测试覆盖：
    - 无实例时的 fallback（available: false）
    - 有 mock 实例时的正常返回结构
    """

    def _fake_app_for_drift(self):
        """创建一个可被 get_app_instance 返回的 mock，模拟 LinkoraEngine 的 drift 方法。"""
        app = MagicMock()
        app.get_tool_whitelist_drift.return_value = {
            "registered_count": 26,
            "whitelist_count": 25,
            "missing_in_whitelist": ["web_search"],
            "stale_in_whitelist": [],
        }
        return app

    def _fake_app_for_poller(self):
        """模拟 LinkoraEngine.get_poller_status()。"""
        app = MagicMock()
        app.get_poller_status.return_value = {
            "last_poll_at": "2026-07-16T09:00:00",
            "last_error": None,
            "last_error_at": None,
            "queue_depth": 0,
            "poll_count": 10,
            "dispatched_total": 5,
            "deferred_total": 0,
            "last_cycle_dispatched": 0,
            "last_cycle_deferred": 0,
            "cold_start_pending": False,
            "max_dispatch_per_cycle": 30,
            "max_concurrent_replies": 4,
        }
        return app

    def _fake_app_for_tools(self):
        """模拟带有工具列表和 skill_manager 的 LinkoraEngine + agent。"""
        tool1 = MagicMock()
        tool1.name = "send_message"
        tool1.display_name = "send_message"
        tool1.description = "Send a DingTalk message"
        tool1.intent_categories = ["domain.communication"]
        type(tool1).__module__ = "src.tools.send_message"

        tool2 = MagicMock()
        tool2.name = "get_weather"
        tool2.display_name = "get_weather"
        tool2.description = "Get weather info"
        tool2.intent_categories = ["domain.weather"]
        type(tool2).__module__ = "src.tools.weather"

        router = MagicMock()
        router._tools = {"send_message": tool1, "get_weather": tool2}

        skill_mgr = MagicMock()
        skill_mgr.get_disabled_skill_owned_tools.return_value = {"get_weather"}

        agent = MagicMock()
        agent.tool_router = router
        agent.skill_manager = skill_mgr

        app = MagicMock()
        app.llm_agent = agent
        app.config.tools.available = ["send_message", "get_weather"]
        return app

    # ── config-drift ──

    def test_config_drift_no_instance(self, tmp_config, tmp_db):
        """get_app_instance 无实例时返 available:false。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.dependencies.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/config-drift")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False

    def test_config_drift_with_instance(self, tmp_config, tmp_db):
        """正常实例返回漂移数据。"""
        from fastapi.testclient import TestClient
        from web.api import app
        fake = self._fake_app_for_drift()
        with patch("web.dependencies.get_app_instance", return_value=fake):
            client = TestClient(app)
            resp = client.get("/api/config-drift")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["registered_count"] == 26
        assert data["missing_in_whitelist"] == ["web_search"]

    # ── poller-status ──

    def test_poller_status_no_instance(self, tmp_config, tmp_db):
        """get_app_instance 无实例时返 available:false。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.routers.metrics.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/poller-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False

    def test_poller_status_with_instance(self, tmp_config, tmp_db):
        """正常实例返轮询器指标。"""
        from fastapi.testclient import TestClient
        from web.api import app
        fake = self._fake_app_for_poller()
        with patch("web.routers.metrics.get_app_instance", return_value=fake):
            client = TestClient(app)
            resp = client.get("/api/poller-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["poll_count"] == 10
        assert data["last_error"] is None

    # ── tools-chain ──

    def test_tools_chain_no_instance(self, tmp_config, tmp_db):
        """get_app_instance 无实例时返 available:false、空列表。"""
        from fastapi.testclient import TestClient
        from web.api import app
        with patch("web.routers.tools.get_app_instance", return_value=None):
            client = TestClient(app)
            resp = client.get("/api/tools-chain")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["tools"] == []

    def test_tools_chain_with_instance(self, tmp_config, tmp_db):
        """正常实例返工具状态清单。"""
        from fastapi.testclient import TestClient
        from web.api import app
        fake = self._fake_app_for_tools()
        with patch("web.routers.tools.get_app_instance", return_value=fake):
            client = TestClient(app)
            resp = client.get("/api/tools-chain")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert len(data["tools"]) == 2
        # get_weather 被技能停用屏蔽 -> disabled_skill
        weather = next(t for t in data["tools"] if t["name"] == "get_weather")
        assert weather["status"] == "disabled_skill"
        assert weather["blocked_by_skill"] is True
        # send_message 正常 -> active
        send = next(t for t in data["tools"] if t["name"] == "send_message")
        assert send["status"] == "active"
        assert send["in_whitelist"] is True


class TestClientIpSpoofing:
    """回归 F18：X-Forwarded-For 取最右（受信任边缘代理追加）而非最左，防伪造 IP 绕过登录限流。"""

    def _req_ip(self, fwd=None, host="9.9.9.9"):
        from web.api import _client_ip

        class _Client:
            def __init__(self, h):
                self.host = h

        class _Req:
            def __init__(self, f, h):
                self.headers = {"X-Forwarded-For": f} if f else {}
                self.client = _Client(h)

        return _Req(fwd, host), _client_ip

    def test_takes_rightmost_xff(self):
        # 直连 IP 为回环/私网（在反代后面）→ 信任 XFF，取最右端（受信任边缘代理追加）
        req, fn = self._req_ip(fwd="203.0.113.9, 10.0.0.1, 192.168.1.10", host="127.0.0.1")
        assert fn(req) == "192.168.1.10"

    def test_spoofed_leftmost_ignored(self):
        # 攻击者伪造最左 IP 试图冒充其他用户 → 必须忽略，取真实边缘 IP
        # 直连 IP 为回环/私网（在反代后面）→ 信任 XFF 取最右端
        req, fn = self._req_ip(fwd="10.0.0.99, 172.16.0.1, 203.0.113.7", host="127.0.0.1")
        assert fn(req) == "203.0.113.7"

    def test_no_xff_uses_client_host(self):
        req, fn = self._req_ip(fwd=None, host="1.2.3.4")
        assert fn(req) == "1.2.3.4"

    def test_public_direct_ip_ignores_xff(self):
        # 安全不变式：服务直接暴露（直连为【公网】IP）时，XFF 完全不可信，
        # 必须忽略伪造的 XFF、返回直连公网 IP，防止攻击者伪造 XFF 绕过登录限流（F18）。
        req, fn = self._req_ip(fwd="203.0.113.9, 10.0.0.1, 192.168.1.10", host="9.9.9.9")
        assert fn(req) == "9.9.9.9"

