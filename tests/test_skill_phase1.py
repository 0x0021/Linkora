"""Phase 1 单一真源：Skill 支持 intent_categories + effective_intent_keywords 测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.skills.loader import SkillLoader


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    d = root / "data" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return d


def test_skill_parses_intent_categories():
    """SKILL.md 声明的 intent_categories 被解析。"""
    with tempfile.TemporaryDirectory() as td:
        _write_skill(Path(td), "weather", "name: weather\nintent_categories:\n- domain.weather\nweight: 1.0\n")
        loader = SkillLoader(td)
        skill = loader.load(str(Path(td) / "data" / "skills" / "weather"))
        assert skill is not None
        assert skill.intent_categories == ["domain.weather"]


def test_skill_effective_resolves_categories():
    """声明 intent_categories 时 effective_intent_keywords 经注册表解析（覆盖噪声自动推导）。"""
    with tempfile.TemporaryDirectory() as td:
        # 故意在 description 写噪声词，验证不会被自动推导污染
        fm = "name: weather\nintent_categories:\n- domain.weather\nweight: 1.0\ndescription: Get current weather and forecasts (no API key required).\n"
        _write_skill(Path(td), "weather", fm)
        loader = SkillLoader(td)
        skill = loader.load(str(Path(td) / "data" / "skills" / "weather"))
        eff = skill.effective_intent_keywords
        assert "天气" in eff          # 来自 domain.weather
        assert "Get" not in eff       # 噪声自动推导词被忽略
        assert "API" not in eff


def test_skill_effective_falls_back_to_keywords():
    """未声明 intent_categories 时回退到字面/自动推导 intent_keywords（向后兼容）。"""
    with tempfile.TemporaryDirectory() as td:
        fm = "name: mytool\nintent_keywords:\n- 查一下\n- 帮我搜\nweight: 0.5\ndescription: 搜索工具\n"
        _write_skill(Path(td), "mytool", fm)
        loader = SkillLoader(td)
        skill = loader.load(str(Path(td) / "data" / "skills" / "mytool"))
        assert skill.effective_intent_keywords == ["查一下", "帮我搜"]


def test_skill_router_uses_effective_keywords():
    """SkillRouter 经 effective_intent_keywords 匹配（category-based 路由）。"""
    from src.skills.manager import SkillManager
    from src.skills.router import SkillRouter
    with tempfile.TemporaryDirectory() as td:
        fm = "name: weather\nintent_categories:\n- domain.weather\nweight: 1.0\ndescription: 天气查询\n"
        _write_skill(Path(td), "weather", fm)
        mgr = SkillManager(td)
        mgr.reload()
        router = SkillRouter(mgr)
        name, _ = router.route("今天天气怎么样要不要带伞")
        assert name == "weather"
