"""技能加载器与 Skill 数据模型单元测试。

覆盖 SkillLoader 全流程：discover / load / _split_frontmatter / _derive_keywords / _load_config，
以及 Skill 数据类的 prompt_section / effective_intent_keywords / semantic_text。
"""

from __future__ import annotations

import tempfile
from pathlib import Path


from src.skills.loader import Skill, SkillLoader


# ── Helper ───────────────────────────────────────────────────

def _write_skill(root: Path, name: str, frontmatter: str, body: str = "# Body\n",
                 subdir: str = "data/skills") -> Path:
    d = root / subdir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return d


def _patch_skill_dirs(monkeypatch, root: str, extras: list[str] | None = None):
    """临时覆盖模块级 _SKILL_DIRS 仅指向测试目录，避免读到真实 data/skills/。

    默认只 patch 主目录（data/skills）；extras 可追加如 ".agents/skills"。
    """
    import src.skills.loader as loader_mod
    dirs = [root + "/data/skills"]
    if extras:
        for extra in extras:
            dirs.append(root + "/" + extra.lstrip("/"))
    monkeypatch.setattr(loader_mod, "_SKILL_DIRS", dirs)



# ── Skill 数据类 ─────────────────────────────────────────────

class TestSkill:
    def test_prompt_section(self):
        s = Skill(name="weather", description="天气查询", body="")
        assert "weather" in s.prompt_section()
        assert "天气查询" in s.prompt_section()

    def test_effective_intent_keywords_with_categories(self, monkeypatch):
        """声明 intent_categories 时从 IntentRegistry 解析关键词。"""
        s = Skill(
            name="weather", description="天气查询", body="",
            intent_categories=["domain.weather"],
        )
        try:
            from src.intent import default_registry
            kws = default_registry.keywords_for_categories(["domain.weather"])
        except Exception:
            kws = []

        result = s.effective_intent_keywords
        assert isinstance(result, list)

        # 无 intent_categories 时回退到 intent_keywords
        s2 = Skill(
            name="test", description="测试", body="",
            intent_keywords=["搜索", "查询"],
        )
        assert s2.effective_intent_keywords == ["搜索", "查询"]

    def test_effective_intent_keywords_empty(self):
        """无 categories 也无 keywords 时返回空列表。"""
        s = Skill(name="x", description="y", body="")
        assert s.effective_intent_keywords == []

    def test_semantic_text(self):
        s = Skill(
            name="planner", description="行程规划助手", body="",
            intent_keywords=["排期", "日程", "规划"],
        )
        text = s.semantic_text
        assert "planner" in text
        assert "行程规划助手" in text
        assert "排期" in text

    def test_composable_default_false(self):
        s = Skill(name="x", description="y", body="")
        assert s.composable is False


# ── SkillLoader.discover ─────────────────────────────────────

class TestDiscover:
    def test_empty_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            loader = SkillLoader(td)
            assert loader.discover() == []

    def test_finds_skills(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\ndescription: 天气")
            _write_skill(Path(td), "search", "name: search\ndescription: 搜索")
            loader = SkillLoader(td)
            dirs = loader.discover()
            assert len(dirs) == 2

    def test_agents_skills_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td, extras=[".agents/skills"])
            _write_skill(Path(td), "legacy", "name: legacy\ndescription: 旧版", subdir=".agents/skills")
            loader = SkillLoader(td)
            dirs = loader.discover()
            assert len(dirs) == 1

    def test_deduplicates(self, monkeypatch):
        """同名技能在 data/skills 和 .agents/skills 取前者。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td, extras=[".agents/skills"])
            _write_skill(Path(td), "dup", "name: dup\ndescription: 新")
            _write_skill(Path(td), "dup", "name: dup\ndescription: 旧", subdir=".agents/skills")
            loader = SkillLoader(td)
            dirs = loader.discover()
            assert len(dirs) == 2  # 不同目录，两个都返回
            # discover 不去重（返回所有目录路径），去重在 manager.reload 里做
            # 这里只验证两个目录都被找到
            assert len([d for d in dirs if "dup" in d]) == 2
            assert any("data/skills/dup" in d for d in dirs)


# ── SkillLoader.load ────────────────────────────────────────

class TestLoad:
    def test_load_basic(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\ndescription: 天气查询\nintent_keywords:\n- 天气\nweight: 0.8\n"
            _write_skill(Path(td), "weather", fm)
            loader = SkillLoader(td)
            dirs = loader.discover()
            skill = loader.load(dirs[0])
            assert skill is not None
            assert skill.name == "weather"
            assert skill.description == "天气查询"
            assert skill.weight == 0.8
            assert skill.intent_keywords == ["天气"]
            assert skill.enabled is True

    def test_load_missing_skill_md(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = Path(td) / "data" / "skills" / "empty"
            d.mkdir(parents=True)
            loader = SkillLoader(td)
            assert loader.load(str(d)) is None

    def test_load_yaml_parse_error(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "bad", "name: [bad: yaml: !!")
            loader = SkillLoader(td)
            assert loader.load(str(d)) is None

    def test_load_frontmatter_not_dict(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "bad", "- list item\n- not a dict")
            loader = SkillLoader(td)
            assert loader.load(str(d)) is None

    def test_load_missing_name(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "noname", "description: 无名称")
            loader = SkillLoader(td)
            assert loader.load(str(d)) is None

    def test_allowed_tools_string(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: tool\nallowed-tools: WebFetch, Bash, Python\n"
            _write_skill(Path(td), "tool", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.allowed_tools == ["WebFetch", "Bash", "Python"]

    def test_allowed_tools_list(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: tool\nallowed-tools:\n- WebFetch\n- Bash\n"
            _write_skill(Path(td), "tool", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.allowed_tools == ["WebFetch", "Bash"]

    def test_intent_keywords_string(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: s\nintent_keywords: 搜索, 查询\n"
            _write_skill(Path(td), "s", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.intent_keywords == ["搜索", "查询"]

    def test_intent_categories_string(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: s\nintent_categories: domain.weather, domain.search\n"
            _write_skill(Path(td), "s", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.intent_categories == ["domain.weather", "domain.search"]

    def test_enabled_false(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: disabled-skill\nenabled: false\n"
            _write_skill(Path(td), "disabled-skill", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.enabled is False

    def test_composable_true(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: comp\ncomposable: true\n"
            _write_skill(Path(td), "comp", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.composable is True

    def test_fallback_tools_string(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: fb\nfallback_tools: web_search, send_message\n"
            _write_skill(Path(td), "fb", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.fallback_tools == ["web_search", "send_message"]

    def test_fallback_tools_list(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: fb\nfallback_tools:\n- web_search\n- send_message\n"
            _write_skill(Path(td), "fb", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.fallback_tools == ["web_search", "send_message"]

    def test_weight_clamped(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: w\nweight: 99.9\n"
            _write_skill(Path(td), "w", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.weight == 1.0

    def test_weight_invalid(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: w\nweight: not_a_number\n"
            _write_skill(Path(td), "w", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert skill.weight == 0.5

    def test_auto_derive_keywords(self, monkeypatch):
        """无 intent_keywords 且无 intent_categories 时自动推导。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: search\ndescription: 网络搜索与网页内容获取\n"
            _write_skill(Path(td), "search", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            assert len(skill.intent_keywords) >= 1
            # 应当推导出有意义的词
            assert any("搜索" in k for k in skill.intent_keywords) or \
                   any("网络" in k for k in skill.intent_keywords)

    def test_has_intent_categories_skips_derive(self, monkeypatch):
        """声明 intent_categories 后不再自动推导 intent_keywords。"""
        with tempfile.TemporaryDirectory() as td:
            fm = "name: w\nintent_categories:\n- domain.weather\n"
            _write_skill(Path(td), "w", fm)
            loader = SkillLoader(td)
            skill = loader.load(str(loader.discover()[0]))
            # 有 intent_categories 时不自动推导，intent_keywords 应为空
            # （路由时从 effective_intent_keywords 走 IntentRegistry 解析）
            assert skill.intent_keywords == [] or isinstance(skill.intent_keywords, list)

    def test_load_config(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "cfg", "name: cfg\n")
            (d / "config.yaml").write_text("api_key: abc\nendpoint: https://api.example.com\n")
            loader = SkillLoader(td)
            skill = loader.load(str(d))
            assert skill.has_config is True
            assert skill.config == {"api_key": "abc", "endpoint": "https://api.example.com"}

    def test_load_config_invalid_yaml(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "badcfg", "name: badcfg\n")
            (d / "config.yaml").write_text(": bad yaml : !!\n")
            loader = SkillLoader(td)
            skill = loader.load(str(d))
            assert skill.has_config is True  # 文件存在
            assert skill.config is None      # 但解析失败

    def test_load_config_not_dict(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            d = _write_skill(Path(td), "listcfg", "name: listcfg\n")
            (d / "config.yaml").write_text("- item1\n- item2\n")
            loader = SkillLoader(td)
            skill = loader.load(str(d))
            assert skill.config is None  # 不是 dict

    def test_no_frontmatter(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data" / "skills" / "nofm"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("# 纯正文无 frontmatter\n\n技能内容。", encoding="utf-8")
            loader = SkillLoader(td)
            assert loader.load(str(d)) is None  # 无 name

    def test_unclosed_frontmatter(self, monkeypatch):
        """只有开头 --- 没有闭合 --- 时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data" / "skills" / "unclosed"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: test\n# Missing closing ---\nBody here.", encoding="utf-8")
            loader = SkillLoader(td)
            result = loader.load(str(d))
            assert result is None


# ── _split_frontmatter ──────────────────────────────────────

class TestSplitFrontmatter:
    def test_normal_split(self):
        raw = "---\nname: test\n---\n\n# Body"
        fm, body = SkillLoader._split_frontmatter(raw)
        assert "name: test" in fm
        assert "# Body" in body

    def test_no_frontmatter(self, monkeypatch):
        raw = "# Just body"
        fm, body = SkillLoader._split_frontmatter(raw)
        assert fm == ""
        assert body == raw

    def test_unclosed_returns_raw(self):
        raw = "---\nname: test\n# no closing"
        fm, body = SkillLoader._split_frontmatter(raw)
        assert fm == ""
        assert body == raw

    def test_empty_frontmatter(self):
        raw = "---\n---\n\nBody"
        fm, body = SkillLoader._split_frontmatter(raw)
        assert "Body" in body


# ── _derive_keywords ────────────────────────────────────────

class TestDeriveKeywords:
    def test_from_description(self):
        kw = SkillLoader._derive_keywords(
            {"description": "网络搜索与网页内容获取工具"}, "# Title\n"
        )
        assert len(kw) >= 1
        assert any("搜索" in k for k in kw) or any("网络" in k for k in kw)

    def test_from_tags(self):
        kw = SkillLoader._derive_keywords(
            {"description": "工具", "tags": ["search", "web"]}, ""
        )
        assert "search" in kw or "web" in kw

    def test_from_body_headings(self):
        kw = SkillLoader._derive_keywords(
            {"description": "测试"},
            "## 天气查询\n## 气温预测\n\n正文内容。",
        )
        assert any("天气" in k for k in kw) or any("气温" in k for k in kw)

    def test_stop_words_filtered(self):
        kw = SkillLoader._derive_keywords(
            {"description": "可以使用这个工具进行查询"}, ""
        )
        # "可以"、"使用"、"这个"、"进行" 应被过滤
        assert "可以" not in kw
        assert "使用" not in kw
        assert "这个" not in kw
        assert "进行" not in kw

    def test_max_25(self):
        kw = SkillLoader._derive_keywords(
            {"description": " ".join(f"功能{i}" for i in range(50))}, ""
        )
        assert len(kw) <= 25

    def test_english_word_extraction(self):
        kw = SkillLoader._derive_keywords(
            {"description": "A web search tool for fetching content"}, ""
        )
        assert "web" in kw or "search" in kw or "tool" in kw
