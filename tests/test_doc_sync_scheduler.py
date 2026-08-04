"""文档自动同步调度器单元测试 — 完整覆盖。

覆盖：
- __init__ / _get_doc_hash / _store
- _sync_single_doc: 空内容、新文档、无本地缓存、非在线文档错误、embedding
- _run_sync: 无文档、多文档批量
- start/stop/sync_now 公共 API
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.doc_sync_scheduler import DocSyncScheduler
from src.memory.sqlite_store import SQLiteStore


# ============================================================================
# 辅助函数
# ============================================================================
def _make_scheduler(tmp_db_path, remote=None):
    dws = MagicMock()
    if remote is not None:
        dws.doc_read.return_value = remote
    return DocSyncScheduler(dws=dws, db_path=str(tmp_db_path)), dws


def _seed_local_doc(tmp_db_path, doc_id, content, last_modified, auto_sync=1):
    store = SQLiteStore(str(tmp_db_path))
    store.init_db()
    store._docs_repo.upsert_dingtalk_doc(
        doc_id=doc_id, title="T", doc_type="doc", url="u",
        content=content, last_modified=last_modified,
    )
    if auto_sync:
        # set auto_sync flag
        cur = store.conn.cursor()
        cur.execute("UPDATE dingtalk_docs SET auto_sync = ? WHERE doc_id = ?", (auto_sync, doc_id))
        store.conn.commit()
    store.close()


# ============================================================================
# __init__
# ============================================================================
class TestInit:
    def test_default_params(self, tmp_db_path):
        dws = MagicMock()
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        assert s.sync_interval == 3600
        assert s.embedding_client is None
        assert s.on_sync is None
        assert s._running is False
        assert s._thread is None

    def test_custom_params(self, tmp_db_path):
        dws = MagicMock()
        emb = MagicMock()
        cb = lambda r: None
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path),
                             sync_interval_seconds=600,
                             embedding_client=emb, on_sync=cb)
        assert s.sync_interval == 600
        assert s.embedding_client is emb
        assert s.on_sync is cb


# ============================================================================
# _get_doc_hash
# ============================================================================
class TestGetDocHash:
    def test_deterministic(self, tmp_db_path):
        s, _ = _make_scheduler(tmp_db_path)
        h1 = s._get_doc_hash("hello")
        h2 = s._get_doc_hash("hello")
        assert h1 == h2
        assert len(h1) == 16

    def test_different_content(self, tmp_db_path):
        s, _ = _make_scheduler(tmp_db_path)
        assert s._get_doc_hash("a") != s._get_doc_hash("b")


# ============================================================================
# _store
# ============================================================================
class TestStore:
    def test_creates_independent_instance(self, tmp_db_path):
        s, _ = _make_scheduler(tmp_db_path)
        store = s._store()
        assert isinstance(store, SQLiteStore)
        assert store.db_path == str(tmp_db_path)
        store.close()


# ============================================================================
# _sync_single_doc — 各种路径
# ============================================================================
class TestSyncSingleDoc:

    # --- 空内容 ---
    def test_empty_content(self, tmp_db_path):
        s, _ = _make_scheduler(tmp_db_path, {"content": "", "lastModified": "X"})
        result = s._sync_single_doc("DOC1")
        assert result["doc_id"] == "DOC1"
        assert result["error"] == "文档内容为空"

    def test_markdown_field_fallback(self, tmp_db_path):
        """优先取 content，其次 markdown 字段"""
        _seed_local_doc(tmp_db_path, "DOC1", "old", "2026-01-01")
        s, _ = _make_scheduler(tmp_db_path, {"markdown": "new content", "lastModified": "2026-02-01"})
        result = s._sync_single_doc("DOC1")
        assert result["changed"] is True

    # --- 新文档（无本地缓存）---
    def test_new_doc_no_local(self, tmp_db_path):
        s, _ = _make_scheduler(tmp_db_path, {
            "content": "全新文档内容",
            "lastModified": "2026-07-11T10:00:00",
            "title": "新标题",
            "doc_type": "doc",
            "url": "https://example.com",
        })
        result = s._sync_single_doc("NEW_DOC")
        assert result["changed"] is True
        assert result["error"] is None
        assert result.get("kb_doc_id") is not None

    # --- DWS 异常处理 ---
    def test_dws_nonretryable_error(self, tmp_db_path):
        from src.dws_adapter import DwsNonRetryableError
        dws = MagicMock()
        dws.doc_read.side_effect = DwsNonRetryableError("普通不可重试错误")
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        result = s._sync_single_doc("DOC1")
        assert result["error"] is not None

    def test_dws_error(self, tmp_db_path):
        from src.dws_adapter import DwsError
        dws = MagicMock()
        dws.doc_read.side_effect = DwsError("DWS 通用错误")
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        result = s._sync_single_doc("DOC1")
        assert result["error"] is not None

    def test_not_online_doc_disables_auto_sync(self, tmp_db_path):
        from src.dws_adapter import DwsNonRetryableError
        _seed_local_doc(tmp_db_path, "DOC1", "old", "2026-01-01", auto_sync=1)

        dws = MagicMock()
        dws.doc_read.side_effect = DwsNonRetryableError("当前节点不是钉钉在线文档")
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        result = s._sync_single_doc("DOC1")

        assert "节点不是钉钉在线文档" in result["error"]
        # auto_sync 应已被关闭
        store = SQLiteStore(str(tmp_db_path))
        store.init_db()
        cur = store.conn.cursor()
        cur.execute("SELECT auto_sync FROM dingtalk_docs WHERE doc_id = ?", ("DOC1",))
        row = cur.fetchone()
        assert row is not None
        assert row["auto_sync"] == 0
        store.close()

    def test_generic_exception(self, tmp_db_path):
        dws = MagicMock()
        dws.doc_read.side_effect = ValueError("未知异常")
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        result = s._sync_single_doc("DOC1")
        assert "未知异常" in result["error"]

    # --- embedding 路径 ---
    def test_embedding_enabled(self, tmp_db_path):
        emb = MagicMock()
        emb.is_enabled = True
        emb.embed_with_retry.return_value = [0.1, 0.2, 0.3]

        dws = MagicMock()
        dws.doc_read.return_value = {
            "content": "需要嵌入的文档内容 " * 20,
            "lastModified": "2026-07-11T10:00:00",
            "title": "嵌入测试",
        }
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path), embedding_client=emb)
        result = s._sync_single_doc("DOC_EMB")

        assert result["changed"] is True
        assert emb.embed_with_retry.called

    def test_embedding_disabled(self, tmp_db_path):
        emb = MagicMock()
        emb.is_enabled = False

        dws = MagicMock()
        dws.doc_read.return_value = {
            "content": "文档内容",
            "lastModified": "2026-07-11T10:00:00",
        }
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path), embedding_client=emb)
        result = s._sync_single_doc("DOC_NOEMB")

        assert result["changed"] is True
        emb.embed.assert_not_called()

    def test_embedding_none_result(self, tmp_db_path):
        emb = MagicMock()
        emb.is_enabled = True
        emb.embed_with_retry.return_value = []  # embedding 失败返回空（生产 embed_with_retry 返回 []）

        dws = MagicMock()
        dws.doc_read.return_value = {
            "content": "文档内容",
            "lastModified": "2026-07-11T10:00:00",
        }
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path), embedding_client=emb)
        result = s._sync_single_doc("DOC_EMB_NONE")

        assert result["changed"] is True
        # 不应因 embedding 失败而抛异常

    # --- on_sync 回调 ---
    def test_on_sync_callback(self, tmp_db_path):
        cb = MagicMock()
        dws = MagicMock()
        dws.doc_read.return_value = {
            "content": "内容",
            "lastModified": "2026-07-11T10:00:00",
        }
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path), on_sync=cb)
        s._sync_single_doc("DOC_CB")
        cb.assert_called_once()

    # --- 已有 KB 文档时重建 ---
    def test_reindex_existing_kb_doc(self, tmp_db_path):
        """已有 KB 文档 → 先删除旧的再重建"""
        content = "被替换的内容"
        _seed_local_doc(tmp_db_path, "DOC_REIDX", "old", "2026-01-01")

        s, _ = _make_scheduler(tmp_db_path, {
            "content": content,
            "lastModified": "2026-07-11T10:00:00",
        })

        # 先手动插入一条 KB 文档记录
        store = SQLiteStore(str(tmp_db_path))
        store.init_db()
        kb_id = store._kb_repo.add_kb_document(
            title="T", doc_type="dingtalk", source="dingtalk",
            source_id="DOC_REIDX", url="u", content="old",
        )
        store.close()

        result = s._sync_single_doc("DOC_REIDX")
        assert result["changed"] is True

        # 旧 KB 文档应已删除
        store = SQLiteStore(str(tmp_db_path))
        store.init_db()
        cur = store.conn.cursor()
        cur.execute("SELECT id FROM kb_documents WHERE id = ?", (kb_id,))
        assert cur.fetchone() is None
        store.close()

    # --- lastModified / modified_at 字段 fallback ---
    def test_modified_at_fallback(self, tmp_db_path):
        _seed_local_doc(tmp_db_path, "DOC1", "old", "2026-01-01")
        s, _ = _make_scheduler(tmp_db_path, {
            "content": "new",
            "modified_at": "2026-07-11",
        })
        result = s._sync_single_doc("DOC1")
        assert result["changed"] is True


# ============================================================================
# _run_sync — 批量轮询
# ============================================================================
class TestRunSync:
    def test_no_auto_sync_docs(self, tmp_db_path):
        s, _ = _make_scheduler(tmp_db_path)
        results = s._run_sync()
        assert results == []

    def test_multiple_docs(self, tmp_db_path):
        _seed_local_doc(tmp_db_path, "DOC1", "c1", "2026-01-01", auto_sync=1)
        _seed_local_doc(tmp_db_path, "DOC2", "c2", "2026-01-01", auto_sync=1)

        dws = MagicMock()
        dws.doc_read.side_effect = [
            {"content": "new1", "lastModified": "2026-07-11"},
            {"content": "new2", "lastModified": "2026-07-11"},
        ]
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        results = s._run_sync()

        assert len(results) == 2
        assert all(r["changed"] for r in results)


# ============================================================================
# start / stop / sync_now — 公共 API
# ============================================================================
class TestLifecycle:
    @pytest.fixture
    def sched(self, tmp_db_path):
        dws = MagicMock()
        return DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))

    def test_start_creates_thread(self, sched):
        sched.start()
        assert sched._running is True
        assert sched._thread is not None
        assert sched._thread.daemon is True
        sched.stop()

    def test_double_start_ignored(self, sched):
        sched.start()
        t1 = sched._thread
        sched.start()
        assert sched._thread is t1
        sched.stop()

    def test_stop(self, sched):
        sched.start()
        sched.stop()
        assert sched._running is False

    def test_stop_when_not_running(self, sched):
        sched.stop()  # 不抛异常

    def test_sync_now_single(self, sched):
        sched.dws.doc_read.return_value = {"content": "x", "lastModified": "2026-01-01"}
        result = sched.sync_now("DOC_SINGLE")
        assert isinstance(result, dict)
        assert result["doc_id"] == "DOC_SINGLE"

    def test_sync_now_all(self, sched):
        results = sched.sync_now()  # 无自动同步文档 → 空列表
        assert isinstance(results, list)
        assert results == []


# ============================================================================
# _sync_single_doc — 时间戳对齐 / 非钉钉文档 / 异常路径
# ============================================================================
class TestSyncSingleDocEdgeCases:
    def test_same_content_timestamp_changed(self, tmp_db_path):
        """内容未变仅时间戳变化 → 对齐本地缓存，不触发重嵌入。"""
        _seed_local_doc(tmp_db_path, "DOC_TS", "same_content", "2026-01-01")
        s, _ = _make_scheduler(tmp_db_path, {
            "content": "same_content",
            "lastModified": "2026-07-11",
        })
        result = s._sync_single_doc("DOC_TS")
        assert result["doc_id"] == "DOC_TS"
        store = SQLiteStore(str(tmp_db_path))
        store.init_db()
        local = store._docs_repo.get_dingtalk_doc("DOC_TS")
        assert local["last_modified"] == "2026-07-11"
        store.close()

    def test_same_content_same_timestamp_skip(self, tmp_db_path):
        """内容与时间戳均未变 → 跳过。"""
        _seed_local_doc(tmp_db_path, "DOC_SKIP", "same", "2026-07-11")
        s, _ = _make_scheduler(tmp_db_path, {
            "content": "same",
            "lastModified": "2026-07-11",
        })
        result = s._sync_single_doc("DOC_SKIP")
        assert result["changed"] is False

    def test_not_dingtalk_doc_disables_auto_sync(self, tmp_db_path):
        """非钉钉在线文档 → 禁用自动同步。"""
        from src.dws_adapter import DwsNonRetryableError
        _seed_local_doc(tmp_db_path, "DOC_BAD", "x", "2026-01-01")

        dws = MagicMock()
        dws.doc_read.side_effect = DwsNonRetryableError(
            "当前节点不是钉钉在线文档: DOC_BAD"
        )
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))
        result = s._sync_single_doc("DOC_BAD")
        assert "已禁用自动同步" in result.get("error", "")

    def test_disable_sync_db_exception(self, tmp_db_path, monkeypatch):
        """禁用自动同步时 DB 异常 → 记录但不传播。"""
        from src.dws_adapter import DwsNonRetryableError
        from src.memory.sqlite_store import SQLiteStore

        _seed_local_doc(tmp_db_path, "DOC_BROKEN", "x", "2026-01-01")

        dws = MagicMock()
        dws.doc_read.side_effect = DwsNonRetryableError(
            "当前节点不是钉钉在线文档: DOC_BROKEN"
        )
        s = DocSyncScheduler(dws=dws, db_path=str(tmp_db_path))

        def _raise(*a, **kw):
            raise RuntimeError("db write failure")
        monkeypatch.setattr(SQLiteStore, "_docs_repo", MagicMock(set_doc_auto_sync=_raise))

        result = s._sync_single_doc("DOC_BROKEN")
        assert "已禁用自动同步" in result.get("error", "")


# ============================================================================
# _sync_loop — 后台线程循环
# ============================================================================
class TestSyncLoop:
    def test_loop_runs_and_stops(self, tmp_db_path):
        import time

        _seed_local_doc(tmp_db_path, "DOC_LOOP", "c", "2026-01-01")
        dws = MagicMock()
        dws.doc_read.return_value = {
            "content": "c", "lastModified": "2026-01-01",
        }

        s = DocSyncScheduler(
            dws=dws, db_path=str(tmp_db_path),
            sync_interval_seconds=3600,
        )
        s.start()
        time.sleep(0.5)
        s.stop()
        s._thread.join(timeout=2)
        assert not s._thread.is_alive()

    def test_loop_exception_does_not_crash(self, tmp_db_path):
        import time

        _seed_local_doc(tmp_db_path, "DOC_CRASH", "x", "2026-01-01")
        dws = MagicMock()
        call_count = [0]
        def _read(doc_id):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise RuntimeError("boom")
            return {"content": "x", "lastModified": "2026-01-01"}
        dws.doc_read.side_effect = _read

        s = DocSyncScheduler(
            dws=dws, db_path=str(tmp_db_path),
            sync_interval_seconds=3600,
        )
        s.start()
        time.sleep(2.0)
        s.stop()
        s._thread.join(timeout=2)
        assert not s._thread.is_alive()
