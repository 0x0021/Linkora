"""ToolRegistry 边界用例（P3-7 加固）。

聚焦清单/依赖注入/平台映射的纯逻辑，不真正实例化全部工具（避免重 IO 依赖）。
"""
from __future__ import annotations

from src.tools.base import BaseTool
from src.tools.registry import (
    BUILTIN_TOOL_MANIFEST,
    build_tool,
    get_builtin_tool_platforms,
)


def test_manifest_nonempty_and_count():
    # 38 工具为对外承诺，新增须同步 manifest
    assert len(BUILTIN_TOOL_MANIFEST) >= 38


def test_manifest_names_unique():
    names = [getattr(c, "name", c.__name__) for c in BUILTIN_TOOL_MANIFEST]
    assert len(names) == len(set(names)), "工具名重复"


def test_manifest_all_subclass_base():
    for c in BUILTIN_TOOL_MANIFEST:
        assert issubclass(c, BaseTool)


def test_get_builtin_tool_platforms_keys_match_manifest():
    mapping = get_builtin_tool_platforms()
    manifest_names = {getattr(c, "name", c.__name__) for c in BUILTIN_TOOL_MANIFEST}
    assert set(mapping.keys()) == manifest_names


def test_build_tool_missing_required_service_returns_none():
    # 缺必需服务（如 dws）时构建应失败返回 None，而非抛异常中断注册
    cls = BUILTIN_TOOL_MANIFEST[0]
    assert build_tool(cls, {}) is None
