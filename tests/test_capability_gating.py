"""能力门控（P0-2）与搜索诚实退出（P0-1）回归测试。

覆盖：
- resolve_capabilities 在 web 模式（embedding_client=None）与 worker 模式（已启用）下
  正确推断 "embedding" 能力。
- register_builtin_tools 对 requires_capabilities 未满足的工具跳过注册（recall_memory 不应
  出现在 web 模式工具清单中），能力满足时正常注册。
- RecallMemoryTool 声明 requires_capabilities=["embedding"]，SaveMemoryTool 不依赖 embedding。
- 技能 frontmatter 的 requires_capabilities 解析 + skill_capabilities_satisfied 门控判定。
- search.py 的 evaluate_search_outcome：零结果 → 非零退出并区分「能力缺失」与「搜了没找到」。
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

from src.skills.loader import Skill, SkillLoader, skill_capabilities_satisfied
from src.tools.base import BaseTool
from src.tools.registry import register_builtin_tools, resolve_capabilities


# ── 1. resolve_capabilities ────────────────────────────────────────────────

def _fake_ec(enabled: bool):
    class _EC:
        pass
    _EC.enabled = enabled
    return _EC()


def test_resolve_capabilities_no_embedding_when_none():
    caps = resolve_capabilities({"embedding_client": None})
    assert "embedding" not in caps


def test_resolve_capabilities_embedding_when_enabled():
    caps = resolve_capabilities({"embedding_client": _fake_ec(True)})
    assert "embedding" in caps


def test_resolve_capabilities_no_embedding_when_disabled():
    caps = resolve_capabilities({"embedding_client": _fake_ec(False)})
    assert "embedding" not in caps


# ── 2. 工具注册能力门控 ────────────────────────────────────────────────────

class _FakeRouter:
    def __init__(self):
        self.names: list[str] = []

    def register(self, tool):
        self.names.append(tool.name)


class _FakeEmbedTool(BaseTool):
    name = "fake_embed"
    description = "需要 embedding 能力的工具"
    requires_capabilities = ["embedding"]

    def execute(self, args):  # pragma: no cover - 不进入执行
        return {}


class _FakePlainTool(BaseTool):
    name = "fake_plain"
    description = "无能力依赖的工具"

    def execute(self, args):  # pragma: no cover
        return {}


def test_register_skips_unmet_capability(monkeypatch):
    """embedding 不可用时，依赖 embedding 的工具不注册；无依赖工具正常注册。"""
    monkeypatch.setattr(
        "src.tools.registry.BUILTIN_TOOL_MANIFEST",
        [_FakeEmbedTool, _FakePlainTool],
    )
    router = _FakeRouter()
    register_builtin_tools(router, {"embedding_client": None})
    assert "fake_plain" in router.names
    assert "fake_embed" not in router.names


def test_register_keeps_tool_when_capability_present(monkeypatch):
    """embedding 可用时，依赖 embedding 的工具正常注册。"""
    monkeypatch.setattr(
        "src.tools.registry.BUILTIN_TOOL_MANIFEST",
        [_FakeEmbedTool, _FakePlainTool],
    )
    router = _FakeRouter()
    register_builtin_tools(router, {"embedding_client": _fake_ec(True)})
    assert "fake_embed" in router.names
    assert "fake_plain" in router.names


def test_recall_memory_requires_embedding_not_save():
    """回归：recall_memory 声明 embedding 依赖，save_memory 不依赖（web 模式不该被误删）。"""
    from src.tools.memory import RecallMemoryTool, SaveMemoryTool

    assert "embedding" in (RecallMemoryTool.requires_capabilities or [])
    assert not (SaveMemoryTool.requires_capabilities or [])


# ── 3. 技能能力门控 ────────────────────────────────────────────────────────

def test_skill_capabilities_satisfied():
    s = Skill(name="x", description="y", body="", requires_capabilities=["playwright"])
    assert skill_capabilities_satisfied(s, set()) == ["playwright"]
    assert skill_capabilities_satisfied(s, {"playwright"}) == []
    # 无声明依赖的技能始终可注册
    s2 = Skill(name="z", description="w", body="")
    assert skill_capabilities_satisfied(s2, set()) == []


def test_loader_parses_requires_capabilities(tmp_path: Path):
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: demo-skill
            description: 演示技能
            requires_capabilities:
            - playwright
            - network
            ---
            # demo
            """
        ),
        encoding="utf-8",
    )
    skill = SkillLoader(str(tmp_path)).load(str(skill_dir))
    assert skill is not None
    assert skill.requires_capabilities == ["playwright", "network"]


# ── 4. search.py 诚实退出（P0-1）─────────────────────────────────────────────

_SEARCH_PATH = (
    Path(__file__).resolve().parent.parent
    / "data/skills/web-composite-search/scripts/search.py"
)


def _load_search_module():
    spec = importlib.util.spec_from_file_location("linkora_test_websearch", _SEARCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_evaluate_search_outcome_ok():
    mod = _load_search_module()
    code, err = mod.evaluate_search_outcome(
        {"aggregated_results": [{"title": "x"}], "metadata": {"playwright_available": True}}
    )
    assert code == 0 and err is None


def test_evaluate_search_outcome_unavailable():
    mod = _load_search_module()
    code, err = mod.evaluate_search_outcome(
        {"aggregated_results": [], "metadata": {"playwright_available": False}}
    )
    assert code == 3
    assert err is not None and "不可用" in err


def test_evaluate_search_outcome_no_results():
    mod = _load_search_module()
    code, err = mod.evaluate_search_outcome(
        {"aggregated_results": [], "metadata": {"playwright_available": True}}
    )
    assert code == 3
    assert err is not None and "未返回" in err


def test_search_main_exits_nonzero_on_empty(monkeypatch, capsys):
    """main() 在零结果时非零退出并把明确原因写到 stderr，供 SkillTool 判为失败。"""
    mod = _load_search_module()

    class _FakeSearch:
        def search(self, q, intent=None, max_results=10):
            return {
                "query_analysis": {},
                "aggregated_results": [],
                "metadata": {"playwright_available": False},
            }

    monkeypatch.setattr(mod, "CompositeSearch", _FakeSearch)
    monkeypatch.setattr(sys, "argv", ["search.py", "珞石机器人股票"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 3
    assert "不可用" in capsys.readouterr().err


# ── 5. 路由能力门控 ────────────────────────────────────────────────────────

def test_router_capability_ok():
    from src.skills.router import SkillRouter

    class _Mgr:
        def list_all(self):
            return []

        def activate_prompt(self, name):
            return ""

    s_ok = Skill(name="a", description="d", body="", requires_capabilities=[])
    s_need = Skill(name="b", description="d", body="", requires_capabilities=["playwright"])

    # 已知能力集：缺能力技能被过滤
    r = SkillRouter(_Mgr(), available_capabilities={"embedding"})
    assert r._capability_ok(s_ok) is True
    assert r._capability_ok(s_need) is False

    # available_capabilities=None → 不做能力过滤（向后兼容旧调用方）
    r2 = SkillRouter(_Mgr(), available_capabilities=None)
    assert r2._capability_ok(s_need) is True

