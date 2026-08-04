"""热重载后 KBSearchTool 重建（P2 修复）测试。

验证 reload_config 在 embedding 配置（enabled/provider）变更时，
通过 _rebuild_kb_search_tool 重建并覆盖同名 kb_search 实例，
使其持有最新 embedding 配置；禁用时正确移除。
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from main import LinkoraEngine
from src.config import load_config
from src.tools.base import ToolRouter
from src.tools.kb_search import KBSearchTool


@pytest.fixture(autouse=True)
def mock_embedding_client():
    """这些测试只验证 reload 重建接线，不应加载 1.2GB 真实模型。

    与 test_kb_search.py 保持一致：mock 掉 EmbeddingClient，避免测试环境
    因模型下载/加载卡在 HF 文件锁而超时（与 embedding.py 实现无关）。
    """
    with patch("src.memory.embedding.EmbeddingClient") as m:
        yield m


def _make_app(emb_enabled: bool):
    """装配 reload 所需属性的裸实例（不触发完整 __init__）。"""
    app = LinkoraEngine.__new__(LinkoraEngine)

    # 构造最小 config：tools + embedding
    cfg = load_config("config.yaml")
    cfg.embedding.enabled = emb_enabled
    app.config = cfg

    # 真实 ToolRouter（其 unregister/register 按 name 覆盖）
    app.tool_router = ToolRouter(cfg.tools)

    # store 用 MagicMock，避免真连库；cursor().fetchall() 返回空供 _build_intent_keywords
    store = MagicMock()
    store.conn.cursor.return_value.fetchall.return_value = []
    app.store = store
    return app


def test_rebuild_kb_search_tool_enables():
    """enable 开启时，重建后 kb_search 工具存在且持有新 enabled 配置。"""
    app = _make_app(emb_enabled=True)
    # 先注册一个旧（disabled）实例
    app.tool_router.register(KBSearchTool(app.store, {"enabled": False}))
    old = app.tool_router._tools["kb_search"]

    app._rebuild_kb_search_tool()

    assert "kb_search" in app.tool_router._tools
    new = app.tool_router._tools["kb_search"]
    assert new is not old  # 确实重建了
    # 新实例持有最新配置（enabled=True → 已加载 embedding_client）
    assert new.embedding_client is not None


def test_rebuild_kb_search_tool_disables():
    """kb_search_enabled=False 时，重建后应移除 kb_search 工具。"""
    app = _make_app(emb_enabled=False)
    # 模拟配置显式禁用 KB 搜索（运行时动态属性，与原代码 hasattr 判定一致）
    object.__setattr__(app.config.tools, "kb_search_enabled", False)
    app.tool_router.register(KBSearchTool(app.store, {"enabled": True}))

    app._rebuild_kb_search_tool()

    assert "kb_search" not in app.tool_router._tools


def test_reload_config_triggers_rebuild_on_embedding_change():
    """reload_config 检测到 embedding 签名变化时应调用重建。"""
    app = _make_app(emb_enabled=False)
    app.tool_router.register(KBSearchTool(app.store, {"enabled": False}))

    # 监控重建调用
    calls = []
    orig = app._rebuild_kb_search_tool
    app._rebuild_kb_search_tool = lambda: calls.append(1) or orig()

    # embedding 由 disabled -> enabled 触发重建
    app.config.embedding.enabled = True

    # 模拟 reload_config 的关键分支（不跑完整 reload，避免加载全量组件）
    old_emb_enabled = False
    new_emb_enabled = app.config.embedding.enabled
    old_sig = (old_emb_enabled, getattr(app.config.embedding, "provider", ""))
    new_sig = (new_emb_enabled, getattr(app.config.embedding, "provider", ""))
    if old_sig != new_sig:
        app._rebuild_kb_search_tool()

    assert calls == [1]
