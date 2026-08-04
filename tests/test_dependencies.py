"""web/dependencies.py 基本测试。

覆盖 get_store（per-thread 缓存、路径切换重建）、_normalize_market_skill 边界值。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from web import dependencies


@pytest.fixture(autouse=True)
def _clear_store_cache():
    """每个测试后关闭并清空 per-thread 缓存，避免连接泄漏与跨测试串味。"""
    yield
    stores = getattr(dependencies._store_local, "stores", None)
    if stores:
        for _s in list(stores.values()):
            try:
                _s.close()
            except Exception:
                pass
        dependencies._store_local.stores = {}


# ============ get_store ============


def test_get_store_returns_sqlite_store(tmp_path):
    """get_store 返回 SQLiteStore 实例。"""
    db = str(tmp_path / "test.db")
    with patch.object(dependencies, "_resolve_platform_path", lambda p: db):
        with patch.object(dependencies.SQLiteStore, "init_db", return_value=None):
            store = dependencies.get_store("dingtalk")
            from src.memory.sqlite_store import SQLiteStore
            assert isinstance(store, SQLiteStore)


def test_get_store_per_thread_cache(tmp_path):
    """同一线程重复调用 get_store 应返回同一实例。"""
    db = str(tmp_path / "test.db")
    with patch.object(dependencies, "_resolve_platform_path", lambda p: db):
        with patch.object(dependencies.SQLiteStore, "init_db", return_value=None):
            s1 = dependencies.get_store("dingtalk")
            s2 = dependencies.get_store("dingtalk")
            assert s1 is s2


def test_get_store_db_path_changed(tmp_path):
    """解析路径切换后应重建 Store 实例。"""
    db1 = str(tmp_path / "db1.db")
    db2 = str(tmp_path / "db2.db")
    state = {"path": db1}
    with patch.object(dependencies, "_resolve_platform_path", lambda p: state["path"]):
        with patch.object(dependencies.SQLiteStore, "init_db", return_value=None):
            s1 = dependencies.get_store("dingtalk")
        state["path"] = db2
        with patch.object(dependencies.SQLiteStore, "init_db", return_value=None):
            s2 = dependencies.get_store("dingtalk")
        assert s1 is not s2


# ============ _normalize_market_skill ============


def test_normalize_full_item():
    """完整字段的 skill 归一化。"""
    item = {
        "slug": "my-skill",
        "name": "My Skill",
        "ownerName": "author1",
        "description_zh": "中文描述",
        "description": "English desc",
        "category": "tools",
        "subCategories": [{"name": "sub1"}, {"name": "sub2"}],
        "tags": ["tag1"],
        "downloads": 100,
        "stars": 50,
        "installs": 200,
        "score": 4.5,
        "version": "1.0.0",
        "verified": True,
        "iconUrl": "https://img.example.com/icon.png",
        "homepage": "https://example.com",
        "source": "github",
        "created_at": 1700000000,
        "updated_at": 1710000000,
        "labels": {"requires_api_key": "true"},
    }
    result = dependencies._normalize_market_skill(item)
    assert result["slug"] == "my-skill"
    assert result["name"] == "My Skill"
    assert result["author"] == "author1"
    assert result["description"] == "中文描述"
    assert result["description_en"] == "English desc"
    assert result["subCategories"] == ["sub1", "sub2"]
    assert result["stars"] == 50
    assert result["requires_api_key"] is True


def test_normalize_minimal_item():
    """仅含 slug 的最小 skill 应不抛异常。"""
    item = {"slug": "minimal"}
    result = dependencies._normalize_market_skill(item)
    assert result["slug"] == "minimal"
    assert result["name"] == "minimal"
    assert result["author"] == ""
    assert result["tags"] == []
    assert result["stars"] == 0
    assert result["requires_api_key"] is False


def test_normalize_labels_not_true():
    """labels.requires_api_key 非 'true' 时不触发。"""
    item = {"slug": "s", "labels": {"requires_api_key": "false"}}
    assert dependencies._normalize_market_skill(item)["requires_api_key"] is False
    item2 = {"slug": "s2", "labels": {"requires_api_key": "FALSE"}}
    assert dependencies._normalize_market_skill(item2)["requires_api_key"] is False


def test_normalize_no_subcategories():
    """无 subCategories 时应返回空列表。"""
    assert dependencies._normalize_market_skill({"slug": "s"})["subCategories"] == []


def test_normalize_missing_labels():
    """无 labels 字段时 requires_api_key 应为 False。"""
    assert dependencies._normalize_market_skill({"slug": "s"})["requires_api_key"] is False


# ============ _get_project_root ============


def test_get_project_root():
    """应返回项目根目录（web/ 上级）。"""
    root = dependencies._get_project_root()
    assert root.name == "Linkora"
    assert (root / "web" / "dependencies.py").exists()
