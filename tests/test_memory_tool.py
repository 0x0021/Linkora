"""记忆工具（recall_memory）单元测试。

覆盖 top_k 参数健壮解析：LLM 可能传字符串型数字（'5'）、中文数字（'五'）、
浮点字符串（'3.7'）或 None。recall_memory 内部对 top_k 做 [:top_k*2] 切片，
遇非 int 会抛 TypeError 使工具崩溃。
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.tools.memory import RecallMemoryTool


def _make_tool():
    store = MagicMock()
    store._memory_repo.recall_memory.return_value = []
    emb = MagicMock()
    emb.enabled = True
    emb.embed.return_value = [0.1, 0.2, 0.3]
    return RecallMemoryTool(store, emb), store


class TestRecallTopK:
    def test_string_number_does_not_crash(self):
        tool, store = _make_tool()
        r = tool.execute({"query": "测试", "top_k": "5"})
        assert "memories" in r
        assert store._memory_repo.recall_memory.call_args[0][1] == 5

    def test_chinese_number_falls_back(self):
        tool, store = _make_tool()
        tool.execute({"query": "测试", "top_k": "五"})
        assert store._memory_repo.recall_memory.call_args[0][1] == 5

    def test_float_string_truncated(self):
        tool, store = _make_tool()
        tool.execute({"query": "测试", "top_k": "3.7"})
        assert store._memory_repo.recall_memory.call_args[0][1] == 3

    def test_none_falls_back(self):
        tool, store = _make_tool()
        tool.execute({"query": "测试", "top_k": None})
        assert store._memory_repo.recall_memory.call_args[0][1] == 5

    def test_clamped_upper(self):
        tool, store = _make_tool()
        tool.execute({"query": "测试", "top_k": 100})
        assert store._memory_repo.recall_memory.call_args[0][1] == 50

    def test_clamped_lower(self):
        tool, store = _make_tool()
        tool.execute({"query": "测试", "top_k": -3})
        assert store._memory_repo.recall_memory.call_args[0][1] == 1


class TestRecallGuards:
    def test_empty_query(self):
        tool, _ = _make_tool()
        assert tool.execute({"query": "  "}).get("error")

    def test_embedding_disabled(self):
        store = MagicMock()
        emb = MagicMock()
        emb.enabled = False
        tool = RecallMemoryTool(store, emb)
        assert tool.execute({"query": "测试"}).get("error")

    def test_embedding_returns_none(self):
        """嵌入返回空/None 时返回明确错误。"""
        store = MagicMock()
        emb = MagicMock()
        emb.enabled = True
        emb.embed.return_value = None
        tool = RecallMemoryTool(store, emb)
        r = tool.execute({"query": "测试"})
        assert r.get("error") == "failed to create query embedding"

    def test_nonempty_results(self):
        """非空记忆结果正确格式化。"""
        store = MagicMock()
        store._memory_repo.recall_memory.return_value = [
            {"content": "记忆1", "source": "chat", "similarity": 0.95, "created_at": "2026-01-01"},
            {"content": "记忆2", "source": "manual", "similarity": 0.82, "created_at": "2026-02-01"},
        ]
        emb = MagicMock()
        emb.enabled = True
        emb.embed.return_value = [0.1, 0.2]
        tool = RecallMemoryTool(store, emb)
        r = tool.execute({"query": "测试"})
        assert r["count"] == 2
        assert r["memories"][0]["content"] == "记忆1"
        assert r["memories"][0]["similarity"] == 0.95


# ============ SaveMemoryTool 去重测试 ============

from src.tools.memory import SaveMemoryTool


def _make_save_tool(is_dup=False):
    store = MagicMock()
    store._memory_repo.check_memory_duplicate.return_value = is_dup
    store._memory_repo.save_memory.return_value = 42
    emb = MagicMock()
    emb.enabled = True
    emb.embed.return_value = [0.1, 0.2, 0.3]
    return SaveMemoryTool(store, emb), store


class TestSaveMemoryDedup:
    def test_duplicate_is_skipped(self):
        """命中去重时应跳过保存，不调 save_memory。"""
        tool, store = _make_save_tool(is_dup=True)
        r = tool.execute({"content": "张三负责采购", "sender_id": "u1"})
        assert r.get("skipped") is True
        assert r.get("reason") == "duplicate"
        store._memory_repo.save_memory.assert_not_called()

    def test_non_duplicate_saves(self):
        """未命中去重时正常保存。"""
        tool, store = _make_save_tool(is_dup=False)
        r = tool.execute({"content": "李四负责财务", "sender_id": "u1"})
        assert r.get("success") is True
        assert r.get("skipped") is None
        store._memory_repo.save_memory.assert_called_once()

    def test_dedup_scoped_by_sender(self):
        """去重检查应带上 sender_id 做范围隔离。"""
        tool, store = _make_save_tool(is_dup=False)
        tool.execute({"content": "内容", "sender_id": "u9"})
        _, kwargs = store._memory_repo.check_memory_duplicate.call_args
        assert kwargs.get("sender_id") == "u9"

    def test_blank_content_rejected(self):
        """纯空白内容应被拒绝且不查重。"""
        tool, store = _make_save_tool()
        r = tool.execute({"content": "   "})
        assert r.get("error")
        store._memory_repo.check_memory_duplicate.assert_not_called()
