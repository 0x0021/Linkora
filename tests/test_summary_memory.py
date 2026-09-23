"""摘要化记忆模式单元测试（2026-09-23 改造）。

覆盖：
- 公共记忆主题分桶（public_memory_topic_key）
- 摘要的「存储/检索/更新」：upsert_summary 同组只留一行且内容被更新
- 待整合缓冲：add_pending_fact / get_pending_groups / get_pending_facts / delete_pending_for
- 缺失 embedding 回填：backfill_summary_embeddings
- recall_memory 对摘要行的召回与 (scope, sender_id) 隔离仍成立
- 合并兜底：merge_memories_into_summary 在 LLM 失败时退回结构化去重
"""
from __future__ import annotations

import json

import pytest

from src.llm.agent_steps.reply import merge_memories_into_summary
from src.memory.memory_repo import public_memory_topic_key
from src.memory.sqlite_store import SQLiteStore


_VEC = [0.0, 1.0, 0.0]


# ============ 主题分桶 ============

class TestPublicTopicKey:
    def test_network_bucket(self):
        assert public_memory_topic_key("内网访问地址 10.0.0.1") == "public:网络与访问"
        assert public_memory_topic_key("软件资源站 http://x") == "public:网络与访问"

    def test_process_bucket(self):
        assert public_memory_topic_key("OA 审批流程") == "public:流程与审批"

    def test_people_bucket(self):
        assert public_memory_topic_key("张三负责采购交接") == "public:人员与组织"

    def test_system_bucket(self):
        assert public_memory_topic_key("账号登录工号") == "public:系统与账号"

    def test_fallback_other(self):
        assert public_memory_topic_key("今天天气不错") == "public:其他"


# ============ 摘要 upsert / get / recall ============

@pytest.fixture
def store(tmp_db_path):
    s = SQLiteStore(db_path=str(tmp_db_path))
    s.init_db()
    return s


class TestSummaryUpsert:
    def test_insert_then_update_same_group(self, store):
        repo = store._memory_repo
        repo.upsert_summary(scope="personal", group_key="u1", content="v1", embedding=_VEC,
                            sender_id="u1", sender_name="A")
        repo.upsert_summary(scope="personal", group_key="u1", content="v2", embedding=_VEC,
                            sender_id="u1", sender_name="A")
        rows = store.conn.execute(
            "SELECT content, kind, group_key, sender_id FROM memories "
            "WHERE scope='personal' AND group_key='u1'"
        ).fetchall()
        assert len(rows) == 1, "同组摘要必须只有一行（更新语义，而非新增逐条）"
        assert rows[0]["content"] == "v2"
        assert rows[0]["kind"] == "summary"
        assert rows[0]["sender_id"] == "u1"

    def test_different_groups_separate_rows(self, store):
        repo = store._memory_repo
        repo.upsert_summary(scope="personal", group_key="u1", content="A1", embedding=_VEC, sender_id="u1")
        repo.upsert_summary(scope="personal", group_key="u2", content="A2", embedding=_VEC, sender_id="u2")
        assert repo.get_summary("personal", "u1")["content"] == "A1"
        assert repo.get_summary("personal", "u2")["content"] == "A2"

    def test_recall_returns_summary_by_sender(self, store):
        repo = store._memory_repo
        repo.upsert_summary(scope="personal", group_key="u_a", content="A 的工资", embedding=_VEC, sender_id="u_a")
        repo.upsert_summary(scope="personal", group_key="u_b", content="B 的住址", embedding=_VEC, sender_id="u_b")
        repo.upsert_summary(scope="public", group_key="public:其他", content="公司年假10天", embedding=_VEC, sender_id="")
        # A 召回：A 的摘要 + 公共摘要，不含 B
        recs = repo.recall_memory(_VEC, top_k=10, sender_id="u_a")
        contents = {r["content"] for r in recs}
        assert "A 的工资" in contents
        assert "公司年假10天" in contents
        assert "B 的住址" not in contents

    def test_recall_public_only_when_no_sender(self, store):
        repo = store._memory_repo
        repo.upsert_summary(scope="personal", group_key="u_a", content="A 的工资", embedding=_VEC, sender_id="u_a")
        repo.upsert_summary(scope="public", group_key="public:其他", content="公司年假10天", embedding=_VEC, sender_id="")
        recs = repo.recall_memory(_VEC, top_k=10)  # 无 sender_id
        contents = {r["content"] for r in recs}
        assert contents == {"公司年假10天"}
        assert "A 的工资" not in contents


# ============ pending 缓冲 ============

class TestPendingBuffer:
    def test_add_and_group_and_delete(self, store):
        repo = store._memory_repo
        repo.add_pending_fact(scope="personal", group_key="u1", content="事实1", sender_name="A")
        repo.add_pending_fact(scope="personal", group_key="u1", content="事实2", sender_name="A")
        repo.add_pending_fact(scope="public", group_key="public:网络与访问", content="事实3")
        groups = repo.get_pending_groups()
        assert ("personal", "u1") in groups
        assert ("public", "public:网络与访问") in groups
        assert repo.get_pending_facts("personal", "u1") == ["事实1", "事实2"]
        assert repo.delete_pending_for("personal", "u1") == 2
        assert repo.get_pending_facts("personal", "u1") == []
        assert ("personal", "u1") not in repo.get_pending_groups()

    def test_pending_fact_exists_dedup(self, store):
        repo = store._memory_repo
        repo.add_pending_fact(scope="personal", group_key="u1", content="重复事实")
        assert repo.pending_fact_exists(scope="personal", group_key="u1", content="重复事实") is True
        assert repo.pending_fact_exists(scope="personal", group_key="u1", content="别的") is False


# ============ embedding 回填 ============

class TestBackfill:
    def test_backfill_fills_null_embedding(self, store):
        repo = store._memory_repo
        # 写入一条无 embedding 的摘要（模拟迁移后/合并时无 embedding 的情况）
        repo.upsert_summary(scope="personal", group_key="u1", content="摘要文本", embedding=None, sender_id="u1")
        assert repo.get_summary("personal", "u1")["embedding"] in (None, "null", "[]", "")

        class _FakeEmb:
            enabled = True

            def embed(self, _text):
                return _VEC

        n = repo.backfill_summary_embeddings(_FakeEmb())
        assert n == 1
        emb = repo.get_summary("personal", "u1")["embedding"]
        assert json.loads(emb) == _VEC


# ============ 合并兜底 ============

class TestMergeFallback:
    def test_fallback_on_llm_failure(self):
        class _FailAgent:
            class client:
                @staticmethod
                def chat(*a, **k):
                    raise RuntimeError("LLM 挂了")

        out = merge_memories_into_summary(_FailAgent(), "旧摘要条目A", ["新事实1", "新事实1", "新事实2"])
        assert "旧摘要条目A" in out
        assert "新事实1" in out
        assert "新事实2" in out
        # 重复的新事实1 应被去重（只出现一次）
        assert out.count("新事实1") == 1

    def test_merge_no_old_summary(self):
        class _FailAgent:
            class client:
                @staticmethod
                def chat(*a, **k):
                    raise RuntimeError("LLM 挂了")

        out = merge_memories_into_summary(_FailAgent(), "", ["事实甲", "事实乙"])
        assert "事实甲" in out and "事实乙" in out
