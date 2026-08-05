"""e2e 集成测试：用 FastAPI TestClient 覆盖一条完整链路。

链路一（知识库）：HTTP 上传文档 → 落 SQLite 分块/向量 → 列表可见 →
向量检索（RAG query）真实命中上传内容。边界（embedding 模型）用确定性
FakeEmbeddingClient mock，避免真实网络；LLM 在 query 路径不参与检索。

链路二（配置审计闭环）：HTTP 恢复默认配置 → 原子写回 config.yaml →
审计模块落 audit 记录（config_write）。把 #2 审计能力与 HTTP 层端到端打通。

策略：真实 SQLiteStore + tmp 配置文件 + 临时审计日志路径，仅 mock 外部依赖。
"""
from __future__ import annotations

import json

import pytest

from src.audit import set_audit_log_path


class FakeEmbeddingClient:
    """确定性伪 embedding：所有文本映射到同一非零常量向量。

    目的仅是让 faiss / 余弦检索在 e2e 中稳定命中（相似度=1.0），不验证
    真实语义质量——那是单元测试的职责。
    """

    DIM = 64

    def embed(self, text: str) -> list[float]:
        return [1.0] * self.DIM

    def embed_with_retry(self, text: str, max_retries: int = 3) -> list[float] | None:
        return self.embed(text)


@pytest.fixture
def e2e_db(tmp_path, monkeypatch):
    db_path = tmp_path / "e2e.db"
    monkeypatch.setattr("web.dependencies.DEFAULT_DB_PATH", str(db_path))
    return str(db_path)


@pytest.fixture
def e2e_config(tmp_path, monkeypatch):
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
        "  api_key: ''\n"
        "embedding:\n"
        "  enabled: true\n"
        "  model: fake-embed\n"
        "  provider: local\n"
        "rag:\n"
        "  chunk_size: 200\n"
        "  chunk_overlap: 20\n"
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
def _clear_cfg_cache():
    """隔离 web.api 的配置磁盘缓存与单例，避免跨测试串味。"""
    import web.api as api
    if hasattr(api, "_cfg_cache"):
        api._cfg_cache = None
        api._cfg_cache_path = None
        api._cfg_cache_mtime = -1
    from src.shared_state import set_config
    set_config(None)
    yield
    if hasattr(api, "_cfg_cache"):
        api._cfg_cache = None
        api._cfg_cache_path = None
        api._cfg_cache_mtime = -1
    set_config(None)


@pytest.fixture
def fake_embedding(monkeypatch):
    monkeypatch.setattr(
        "web.api._get_embedding_client",
        lambda embedding_config: FakeEmbeddingClient(),
    )


@pytest.fixture
def audit_tmp(tmp_path):
    p = tmp_path / "audit.log"
    set_audit_log_path(p)
    yield p
    set_audit_log_path(None)


class TestKbPipelineE2E:
    def test_upload_then_query_retrieves_chunk(self, e2e_db, e2e_config, fake_embedding):
        """完整链路：上传文档 → 列表可见 → 向量检索命中原文。"""
        from fastapi.testclient import TestClient
        from web.api import app

        client = TestClient(app)
        content = "灵桥(Linkora)是面向钉钉的数字分身，支持审批转交与知识库检索。"
        # 1) 上传
        resp = client.post("/api/kb/documents", json={
            "title": "灵桥介绍",
            "doc_type": "markdown",
            "source": "e2e",
            "content": content,
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert body["id"] > 0
        assert body["chunks"] >= 1

        # 2) 列表可见
        lst = client.get("/api/kb/documents")
        assert lst.status_code == 200
        lst_body = lst.json()
        docs = lst_body.get("documents", []) if isinstance(lst_body, dict) else lst_body
        titles = [d["title"] for d in docs]
        assert "灵桥介绍" in titles

        # 3) 向量检索命中原文（RAG query）
        q = client.post("/api/kb/query", json={
            "query": content,
            "top_k": 5,
            "min_similarity": 0.0,
        })
        assert q.status_code == 200, q.text
        qbody = q.json()
        assert qbody["success"] is True
        assert qbody["results"], "上传内容应被检索命中"
        assert any(content[:10] in r["content"] for r in qbody["results"])

    def test_stats_reflects_uploaded_doc(self, e2e_db, e2e_config, fake_embedding):
        from fastapi.testclient import TestClient
        from web.api import app

        client = TestClient(app)
        client.post("/api/kb/documents", json={
            "title": "统计测试文档",
            "doc_type": "text",
            "source": "e2e",
            "content": "用于验证 kb/stats 端到端反映入库文档数。",
        })
        stats = client.get("/api/kb/stats")
        assert stats.status_code == 200
        data = stats.json()
        # kb_stats 返回 total_documents 字段（端到端验证入库文档计数）
        assert data.get("total_documents", 0) >= 1


class TestConfigAuditE2E:
    def test_restore_default_writes_audit(self, e2e_config, audit_tmp):
        """配置恢复默认 → 原子写回 → 审计模块落 config_write 记录。"""
        from fastapi.testclient import TestClient
        from web.api import app

        client = TestClient(app)
        resp = client.post("/api/config/default")
        assert resp.status_code == 200, resp.text
        assert resp.json().get("success") is True

        # 审计文件应含一条 config_write 记录（闭环 #2 审计能力）
        assert audit_tmp.exists()
        lines = audit_tmp.read_text(encoding="utf-8").splitlines()
        assert lines, "审计日志应为空文件以外有内容"
        records = [json.loads(line) for line in lines]
        cfg_audits = [r for r in records if r["event"] == "config_write"]
        assert cfg_audits, "应记录一条 config_write 审计"
        assert cfg_audits[0]["action"] == "update_config"
        assert cfg_audits[0]["status"] == "success"
