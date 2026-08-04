"""记忆安全隔离测试（个人 vs 公共）。

核心需求（来自用户）：
- 个人记忆 = 我和对方私有的记忆，绝不能出现在第三方；
- 公共记忆 = 可以和所有人分享的信息。

本测试断言：
1. 用户 A 的个人记忆，绝不被用户 B（第三方）召回；
2. 用户 B 的个人记忆，绝不被用户 A 召回；
3. 缺少 sender_id 的调用（系统/匿名）只返回公共记忆，不泄露任何个人记忆；
4. 公共记忆对所有人（含任意 sender_id、含无 sender_id）都可见。
"""
from __future__ import annotations


from src.memory.sqlite_store import SQLiteStore
from src.tools.memory import RecallMemoryTool


def _make_store(tmp_db_path):
    store = SQLiteStore(db_path=str(tmp_db_path))
    store.init_db()
    return store


_VEC = [0.0, 1.0, 0.0]  # 统一向量，便于断言"是否出现在结果集中"而非依赖相似度


def _seed(store):
    # A 的个人私密记忆（高敏感）
    store._memory_repo.save_memory(
        key="a-priv", content="A的工资是3万，别告诉别人", source="chat",
        sender_id="u_a", sender_name="A", scope="personal", embedding=_VEC,
    )
    # B 的个人私密记忆
    store._memory_repo.save_memory(
        key="b-priv", content="B的家庭住址在幸福路1号", source="chat",
        sender_id="u_b", sender_name="B", scope="personal", embedding=_VEC,
    )
    # 一条明确的公共记忆
    store._memory_repo.save_memory(
        key="pub", content="公司年假为10天，全员适用", source="manual",
        sender_id="", scope="public", embedding=_VEC,
    )


def _contents(recs):
    return {r["content"] for r in recs}


class TestPersonalMemoryIsolation:
    """个人记忆绝不对第三方可见。"""

    def test_a_cannot_see_b_personal(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        _seed(store)
        recs = store._memory_repo.recall_memory(_VEC, top_k=10, sender_id="u_a")
        contents = _contents(recs)
        assert "A的工资是3万，别告诉别人" in contents
        assert "公司年假为10天，全员适用" in contents
        # 关键：B 的个人记忆绝不能泄漏给 A
        assert "B的家庭住址在幸福路1号" not in contents

    def test_b_cannot_see_a_personal(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        _seed(store)
        recs = store._memory_repo.recall_memory(_VEC, top_k=10, sender_id="u_b")
        contents = _contents(recs)
        assert "B的家庭住址在幸福路1号" in contents
        assert "公司年假为10天，全员适用" in contents
        assert "A的工资是3万，别告诉别人" not in contents

    def test_no_sender_only_public(self, tmp_db_path):
        """无 sender_id（系统/匿名调用）只返回公共记忆，绝不泄露个人。"""
        store = _make_store(tmp_db_path)
        _seed(store)
        recs = store._memory_repo.recall_memory(_VEC, top_k=10)  # 无 sender_id, 无 chat_id
        contents = _contents(recs)
        assert contents == {"公司年假为10天，全员适用"}
        assert "A的工资是3万，别告诉别人" not in contents
        assert "B的家庭住址在幸福路1号" not in contents

    def test_unrelated_third_party_only_public(self, tmp_db_path):
        """完全无关的第三方 C 只能看到公共记忆。"""
        store = _make_store(tmp_db_path)
        _seed(store)
        recs = store._memory_repo.recall_memory(_VEC, top_k=10, sender_id="u_c")
        contents = _contents(recs)
        assert contents == {"公司年假为10天，全员适用"}
        assert "A的工资是3万，别告诉别人" not in contents
        assert "B的家庭住址在幸福路1号" not in contents


class TestPublicMemorySharing:
    """公共记忆对所有人可见。"""

    def test_public_visible_to_all_callers(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        _seed(store)
        for sid in ("u_a", "u_b", "u_c", ""):
            recs = store._memory_repo.recall_memory(_VEC, top_k=10, sender_id=sid)
            assert "公司年假为10天，全员适用" in _contents(recs)


class TestRecallToolEnforcesIsolation:
    """工具层（LLM 实际调用入口）也按 sender_id 隔离。"""

    def test_tool_respects_sender_id(self, tmp_db_path):
        store = _make_store(tmp_db_path)
        _seed(store)

        # 轻量 EmbeddingClient 替身，避免依赖模型服务
        class _FakeEmb:
            enabled = True

            def embed(self, _text):
                return _VEC

        tool = RecallMemoryTool(store=store, embedding_client=_FakeEmb())
        # 模拟 LLM 以 A 的身份召回
        res = tool.execute({"query": "工资", "sender_id": "u_a"})
        mems = res.get("memories", [])
        contents = {m["content"] for m in mems}
        assert "A的工资是3万，别告诉别人" in contents
        assert "B的家庭住址在幸福路1号" not in contents
        # 模拟 LLM 以 B 的身份召回
        res_b = tool.execute({"query": "住址", "sender_id": "u_b"})
        mems_b = res_b.get("memories", [])
        contents_b = {m["content"] for m in mems_b}
        assert "B的家庭住址在幸福路1号" in contents_b
        assert "A的工资是3万，别告诉别人" not in contents_b
