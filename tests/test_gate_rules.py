"""答复门禁（输入侧拦截）模块的单元 / 接口测试。

覆盖：
- GateRuleRepo + SQLiteStore 门面：增删改查、启用停用、分类、统计、命中计数
- src.gate.reply_gate 引擎：关键词 / 正则命中、未命中放行、禁用规则不拦截、
  坏正则 fail-open、优先级排序
- web.routers.gate_rules 路由：CRUD、校验、批量、命中测试（经 TestClient）

与 test_web_api_endpoints 共享同一套 tmp_db / tmp_config 隔离约定。
"""

from __future__ import annotations

import pytest

from web.api import app as _app  # noqa: F401  (确保路由已注册)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """临时 DB 路径，patch SQLiteStore 默认连接。"""
    db_path = tmp_path / "test_gate.db"
    monkeypatch.setattr("web.dependencies.DEFAULT_DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """最小 config.yaml，关闭鉴权与认证。"""
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
    """隔离跨测试的模块级缓存（门禁 TTL 缓存、配置单例）。"""
    import web.api as api
    from src.gate.reply_gate import invalidate_gate_cache
    from src.shared_state import set_config
    invalidate_gate_cache()
    if hasattr(api, "_cfg_cache"):
        api._cfg_cache = None
        api._cfg_cache_path = None
        api._cfg_cache_mtime = -1
    set_config(None)
    yield
    invalidate_gate_cache()
    from src.shared_state import set_config as _sc
    _sc(None)


# ============ Repo / 门面 ============

class TestGateRuleRepo:
    def test_crud_lifecycle(self, tmp_config, tmp_db):
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        rid = store.add_gate_rule(
            category="private_privacy", category_label="私人隐私",
            name="禁止询问婚史", match_type="keyword",
            pattern="婚史, 年龄", intercept_message="抱歉，不便回答",
            priority=5, enabled=1,
        )
        assert rid > 0
        rule = store.get_gate_rule(rid)
        assert rule is not None
        assert rule["category"] == "private_privacy"
        assert rule["category_label"] == "私人隐私"
        assert rule["hit_count"] == 0

        store.update_gate_rule(rid, name="禁止询问个人婚史", priority=9)
        updated = store.get_gate_rule(rid)
        assert updated is not None
        assert updated["name"] == "禁止询问个人婚史"
        assert updated["priority"] == 9

        assert store.count_gate_rules() == 1
        assert store.count_gate_rules(enabled=1) == 1
        store.delete_gate_rule(rid)
        assert store.get_gate_rule(rid) is None
        assert store.count_gate_rules() == 0

    def test_categories_roundtrip(self, tmp_config, tmp_db):
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        # 门面层只做原始存储（标签归一化是路由层的职责）
        store.add_gate_rule(category="illegal_info", category_label="",
                            name="梯子", match_type="keyword", pattern="vpn",
                            intercept_message="")
        cats = store.gate_rule_categories()
        assert ("illegal_info", "") in cats

    def test_stats_and_increment_hit(self, tmp_config, tmp_db):
        from src.memory.sqlite_store import SQLiteStore
        store = SQLiteStore(tmp_db)
        rid = store.add_gate_rule(category="negative_emotion", category_label="负面情绪",
                                  name="脏话", match_type="keyword", pattern="傻逼",
                                  intercept_message="")
        store.increment_gate_rule_hit(rid)
        store.increment_gate_rule_hit(rid)
        stats = store.gate_rules_stats()
        assert stats["total"] == 1
        assert stats["enabled"] == 1
        top = stats["top_hits"][0]
        assert top["id"] == rid and top["hit_count"] == 2


# ============ 引擎 ============

class TestReplyGateEngine:
    def _store(self, tmp_config, tmp_db):
        from src.memory.sqlite_store import SQLiteStore
        return SQLiteStore(tmp_db)

    def test_keyword_blocks(self, tmp_config, tmp_db):
        store = self._store(tmp_config, tmp_db)
        store.add_gate_rule(category="private_privacy", category_label="私人隐私",
                            name="婚史", match_type="keyword",
                            pattern="婚史, 家庭", intercept_message="拦")
        from src.gate.reply_gate import evaluate_gate_rules, invalidate_gate_cache
        invalidate_gate_cache()
        res = evaluate_gate_rules(store, "你结婚了吗，你的家庭情况怎样")
        assert res.blocked is True
        assert res.category == "private_privacy"

    def test_regex_blocks(self, tmp_config, tmp_db):
        store = self._store(tmp_config, tmp_db)
        store.add_gate_rule(category="illegal_info", category_label="违法信息",
                            name="梯子", match_type="regex",
                            pattern=r"梯[子仔]|vpn", intercept_message="拦")
        from src.gate.reply_gate import evaluate_gate_rules, invalidate_gate_cache
        invalidate_gate_cache()
        res = evaluate_gate_rules(store, "怎么用梯子翻墙")
        assert res.blocked is True
        assert res.match_type == "regex"

    def test_no_match_passes(self, tmp_config, tmp_db):
        store = self._store(tmp_config, tmp_db)
        store.add_gate_rule(category="private_privacy", category_label="私人隐私",
                            name="婚史", match_type="keyword", pattern="婚史",
                            intercept_message="")
        from src.gate.reply_gate import evaluate_gate_rules, invalidate_gate_cache
        invalidate_gate_cache()
        res = evaluate_gate_rules(store, "今天天气不错")
        assert res.blocked is False

    def test_disabled_rule_does_not_block(self, tmp_config, tmp_db):
        store = self._store(tmp_config, tmp_db)
        store.add_gate_rule(category="private_privacy", category_label="私人隐私",
                            name="婚史", match_type="keyword", pattern="婚史", enabled=0,
                            intercept_message="")
        from src.gate.reply_gate import evaluate_gate_rules, invalidate_gate_cache
        invalidate_gate_cache()
        res = evaluate_gate_rules(store, "你的婚史是怎样的")
        assert res.blocked is False

    def test_bad_regex_fail_open(self, tmp_config, tmp_db):
        """坏正则不得让整条消息被「伪拦截」，也不得抛异常。"""
        store = self._store(tmp_config, tmp_db)
        store.add_gate_rule(category="illegal_info", category_label="违法信息",
                            name="坏正则", match_type="regex",
                            pattern="([a-z]+", intercept_message="拦")
        from src.gate.reply_gate import evaluate_gate_rules, invalidate_gate_cache
        invalidate_gate_cache()
        # 坏正则匹配失败 -> 不拦截（fail-open）
        res = evaluate_gate_rules(store, "hello world")
        assert res.blocked is False

    def test_priority_ordering(self, tmp_config, tmp_db):
        store = self._store(tmp_config, tmp_db)
        store.add_gate_rule(category="other", category_label="其他", name="低优",
                            match_type="keyword", pattern="测试", priority=1,
                            intercept_message="")
        store.add_gate_rule(category="illegal_info", category_label="违法信息", name="高优",
                            match_type="keyword", pattern="测试", priority=99,
                            intercept_message="")
        from src.gate.reply_gate import evaluate_gate_rules, invalidate_gate_cache
        invalidate_gate_cache()
        res = evaluate_gate_rules(store, "这是一个测试")
        assert res.blocked is True
        assert res.rule_name == "高优"  # 优先级高的先命中


# ============ 路由 ============

class TestGateRuleRouter:
    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(_app)

    def test_list_empty_then_add(self, tmp_config, tmp_db):
        client = self._client()
        r = client.get("/api/gate-rules")
        assert r.status_code == 200
        assert r.json()["total"] == 0

        add = client.post("/api/gate-rules", json={
            "category": "private_privacy", "category_label": "私人隐私",
            "name": "婚史", "match_type": "keyword",
            "pattern": "婚史, 年龄", "intercept_message": "不便回答",
            "priority": 3, "enabled": 1,
        })
        assert add.status_code == 200 and add.json()["success"] is True
        rid = add.json()["id"]

        lst = client.get("/api/gate-rules")
        assert lst.json()["total"] == 1
        assert lst.json()["rules"][0]["id"] == rid

    def test_add_normalizes_category_label(self, tmp_config, tmp_db):
        """路由层归一化：空 category_label 按预设补全中文标签。"""
        client = self._client()
        add = client.post("/api/gate-rules", json={
            "category": "illegal_info", "category_label": "",
            "name": "梯子", "match_type": "keyword", "pattern": "梯子",
        })
        assert add.status_code == 200
        rid = add.json()["id"]
        rule = client.get(f"/api/gate-rules/{rid}").json()["rule"]
        assert rule["category"] == "illegal_info"
        assert rule["category_label"] == "违法信息"

    def test_add_validation(self, tmp_config, tmp_db):
        client = self._client()
        # 空 pattern
        bad = client.post("/api/gate-rules", json={"pattern": "  ", "match_type": "keyword"})
        assert bad.status_code == 400
        # 非法 match_type
        bad2 = client.post("/api/gate-rules", json={"pattern": "x", "match_type": "fuzzy"})
        assert bad2.status_code == 400

    def test_get_update_toggle_delete(self, tmp_config, tmp_db):
        client = self._client()
        rid = client.post("/api/gate-rules", json={
            "category": "other", "name": "t", "match_type": "keyword", "pattern": "x"
        }).json()["id"]
        assert client.get(f"/api/gate-rules/{rid}").status_code == 200
        assert client.get("/api/gate-rules/999999").status_code == 404

        upd = client.put(f"/api/gate-rules/{rid}", json={"name": "renamed", "priority": 7})
        assert upd.status_code == 200 and upd.json()["success"] is True
        assert client.get(f"/api/gate-rules/{rid}").json()["rule"]["name"] == "renamed"

        tog = client.post(f"/api/gate-rules/{rid}/toggle", json={})
        assert tog.json()["enabled"] == 0

        dele = client.delete(f"/api/gate-rules/{rid}")
        assert dele.status_code == 200 and dele.json()["success"] is True
        assert client.get(f"/api/gate-rules/{rid}").status_code == 404

    def test_batch_ops(self, tmp_config, tmp_db):
        client = self._client()
        ids = []
        for i in range(3):
            ids.append(client.post("/api/gate-rules", json={
                "category": "other", "name": f"r{i}", "match_type": "keyword",
                "pattern": f"p{i}", "enabled": 1,
            }).json()["id"])
        batch = client.post("/api/gate-rules/batch", json={"ids": ids, "action": "disable"})
        assert batch.json()["count"] == 3
        lst = client.get("/api/gate-rules?enabled=0")
        assert lst.json()["total"] == 3
        clean = client.post("/api/gate-rules/batch", json={"ids": ids, "action": "delete"})
        assert clean.json()["count"] == 3
        assert client.get("/api/gate-rules").json()["total"] == 0

    def test_test_match_endpoint(self, tmp_config, tmp_db):
        client = self._client()
        client.post("/api/gate-rules", json={
            "category": "illegal_info", "category_label": "违法信息",
            "name": "梯子", "match_type": "keyword", "pattern": "梯子, vpn",
            "intercept_message": "该内容无法回答",
        })
        hit = client.post("/api/gate-rules/test-match", json={"text": "怎么用梯子"})
        assert hit.status_code == 200
        body = hit.json()
        assert body["blocked"] is True
        assert body["category"] == "illegal_info"
        assert body["intercept_message"] == "该内容无法回答"

        miss = client.post("/api/gate-rules/test-match", json={"text": "今天吃什么"})
        assert miss.json()["blocked"] is False

        empty = client.post("/api/gate-rules/test-match", json={"text": ""})
        assert empty.status_code == 400
