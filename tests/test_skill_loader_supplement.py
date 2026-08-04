"""SkillLoader 补充测试：YAML 解析失败、intent_categories 字符串、composable、config 异常等边缘路径。"""
from __future__ import annotations

import tempfile
from pathlib import Path


from src.skills.loader import Skill, SkillLoader


def _write_skill_md(root: Path, name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    d = root / "data/skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return d


class TestLoadEdgeCases:
    def test_yaml_parse_error(self):
        """frontmatter 中包含无效 YAML 时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "bad", 'name: test\nkey: {{ invalid yaml', "text")
            loader = SkillLoader(td)
            skill = loader.load(str(Path(td) / "data/skills" / "bad"))
            assert skill is None

    def test_frontmatter_not_dict(self):
        """frontmatter 解析结果不是 dict 时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "list", "- item1\n- item2", "text")
            loader = SkillLoader(td)
            skill = loader.load(str(Path(td) / "data/skills" / "list"))
            assert skill is None

    def test_missing_name_field(self):
        """缺少 name 字段时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "noname", "description: test")
            loader = SkillLoader(td)
            skill = loader.load(str(Path(td) / "data/skills" / "noname"))
            assert skill is None

    def test_missing_skill_md(self):
        """目录无 SKILL.md 时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data/skills" / "empty_dir"
            d.mkdir(parents=True, exist_ok=True)
            loader = SkillLoader(td)
            skill = loader.load(str(d))
            assert skill is None

    def test_intent_categories_as_string(self):
        """intent_categories 为逗号分隔字符串时正确解析。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "cat_str",
                            "name: cat_str\ndescription: test\nintent_categories: domain.weather, domain.travel")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "cat_str"))
            assert s.intent_categories == ["domain.weather", "domain.travel"]

    def test_composable_true(self):
        """composable: true 时正确解析。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "comp",
                            "name: comp\ndescription: test\ncomposable: true")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "comp"))
            assert s.composable is True

    def test_fallback_tools_as_string(self):
        """fallback_tools 为逗号分隔字符串时正确解析。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "fb_str",
                            "name: fb_str\ndescription: test\nfallback_tools: tool_a, tool_b")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "fb_str"))
            assert s.fallback_tools == ["tool_a", "tool_b"]

    def test_weight_clamped(self):
        """权重超过 [0,1] 范围时被钳制。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "w", "name: w\ndescription: test\nweight: 1.5")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "w"))
            assert s.weight == 1.0

            _write_skill_md(Path(td), "w2", "name: w2\ndescription: test\nweight: -0.5")
            s2 = loader.load(str(Path(td) / "data/skills" / "w2"))
            assert s2.weight == 0.0

    def test_weight_invalid(self):
        """非数字权重回退默认值。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "bad_w", "name: bad_w\ndescription: test\nweight: abc")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "bad_w"))
            assert s.weight == 0.5

    def test_enabled_false(self):
        """enabled: false 时技能被禁用。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "disabled", "name: disabled\ndescription: test\nenabled: false")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "disabled"))
            assert s.enabled is False

    def test_auto_derive_keywords(self):
        """无 intent_keywords 且无 intent_categories 时自动推导。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "auto_kw",
                            "name: auto_kw\ndescription: NLP processing engine for text\ntags:\n  - transport\n  - ai",
                            body="## 功能介绍\n## 使用方法")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "auto_kw"))
            assert len(s.intent_keywords) > 0
            # 确认包含英文单词（≥3 字母）和中文词组
            has_en = any(not any('\u4e00' <= c <= '\u9fff' for c in k) and len(k) >= 3
                        for k in s.intent_keywords)
            assert has_en, f"Expected English keywords but got {s.intent_keywords}"

    def test_config_yaml_error(self):
        """config.yaml 解析失败时 config 为 None 且不崩溃。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data/skills" / "bad_config"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text("---\nname: bad_config\ndescription: test\n---\n\ntext", encoding="utf-8")
            (d / "config.yaml").write_text(": : : bad yaml [[[", encoding="utf-8")
            loader = SkillLoader(td)
            s = loader.load(str(d))
            assert s is not None
            assert s.config is None

    def test_config_not_dict(self):
        """config.yaml 内容不是 dict 时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "data/skills" / "list_cfg"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text("---\nname: list_cfg\ndescription: test\n---\n\ntext", encoding="utf-8")
            (d / "config.yaml").write_text("- item1\n- item2", encoding="utf-8")
            loader = SkillLoader(td)
            s = loader.load(str(d))
            assert s.config is None

    def test_effective_intent_keywords_except(self, monkeypatch):
        """keywords_for_categories 抛异常时回退到 intent_keywords。"""
        s = Skill(
            name="weather", description="天气查询", body="",
            intent_categories=["domain.weather"],
            intent_keywords=["搜索", "查询"],
        )
        # 模拟 keywords_for_categories 抛异常
        def _raise(*args, **kwargs):
            raise RuntimeError("intent module not loaded")
        monkeypatch.setattr(
            "src.intent.default_registry.keywords_for_categories", _raise,
        )
        result = s.effective_intent_keywords
        assert result == ["搜索", "查询"]

    def test_allowed_tools_other_type(self):
        """allowed-tools 为其他类型时返回空列表。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "bad_tools",
                            "name: bad_tools\ndescription: test\nallowed-tools: 123")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "bad_tools"))
            assert s.allowed_tools == []

    def test_intent_keywords_other_type(self):
        """intent_keywords 为其他类型时走 else（空列表），之后由 _derive_keywords 自动推导。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "kw_int",
                            "name: kw_int\ndescription: 智能翻译\ntags:\n  - nlp\nintent_keywords: 123")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "kw_int"))
            # intent_keywords: 123（非 list 非 str）→ else → []，然后触发自动推导
            assert len(s.intent_keywords) > 0

    def test_intent_categories_other_type(self):
        """intent_categories 为其他类型时返回空列表。"""
        with tempfile.TemporaryDirectory() as td:
            _write_skill_md(Path(td), "cat_int",
                            "name: cat_int\ndescription: test\nintent_categories: 456")
            loader = SkillLoader(td)
            s = loader.load(str(Path(td) / "data/skills" / "cat_int"))
            assert s.intent_categories == []
