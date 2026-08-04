"""SkillRouter 单元测试：覆盖显式激活/意图匹配/关键词兜底/组合路由/目标收敛等全路径。"""
from __future__ import annotations

import tempfile
from pathlib import Path


from src.skills.manager import SkillManager
from src.skills.router import SkillRouter, SkillMatch


def _patch_skill_dirs(monkeypatch, root: str):
    """临时覆盖模块级 _SKILL_DIRS 仅指向测试目录。"""
    import src.skills.loader as loader_mod
    monkeypatch.setattr(loader_mod, "_SKILL_DIRS", [root + "/data/skills"])


def _write_skill(root: Path, name: str, frontmatter: str, body: str = "# Body\n") -> Path:
    d = root / "data" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}---\n\n{body}", encoding="utf-8")
    return d


# ── 配置/属性 ─────────────────────────────────────────────────

class TestConfigToggles:
    def test_semantic_enabled_defaults_true(self, monkeypatch):
        """_skills_config=None → semantic 默认开启。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr)  # skills_config=None
            assert router._semantic_enabled() is True

    def test_combo_enabled_defaults_true(self, monkeypatch):
        """_skills_config=None → combo 默认开启。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr)
            assert router._combo_enabled() is True

    def test_semantic_disabled_via_config(self, monkeypatch):
        class Config:
            semantic_routing = False
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr, skills_config=Config())
            assert router._semantic_enabled() is False

    def test_combo_disabled_via_config(self, monkeypatch):
        class Config:
            combo_enabled = False
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr, skills_config=Config())
            assert router._combo_enabled() is False

    def test_combo_gap_default(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr)
            assert router._combo_gap() == 0.12

    def test_combo_gap_from_config(self, monkeypatch):
        class Config:
            combo_gap = 0.25
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr, skills_config=Config())
            assert router._combo_gap() == 0.25


# ── 显式激活 ──────────────────────────────────────────────────

class TestExplicit:
    def test_detect_explicit_use(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "web-search", "name: web-search\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            assert router.detect_explicit("用 web-search 技能 搜一下") == "web-search"

    def test_detect_explicit_activate(self, monkeypatch):
        """「激活 XX」模式：贪婪匹配取完整技能名。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # 正则「激活\s*(\S+)(?:\s*(?:技能|skill))?」为贪婪匹配，
            # "激活 weather" → 捕获完整 "weather"，命中已启用技能 → 返回 "weather"
            result = router.detect_explicit("激活 weather")
            assert result == "weather"

    def test_detect_explicit_use_skill_keyword(self, monkeypatch):
        """「use XX skill」模式：贪婪匹配取完整技能名。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "calculator", "name: calculator\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # 正则「use[_ ]*(\S+)(?:\s*skill)?」贪婪匹配，
            # "use calculator skill now" → 捕获 "calculator"（遇空白停），命中已启用技能
            assert router.detect_explicit("use calculator skill now") == "calculator"

    def test_detect_explicit_activate_short_name(self, monkeypatch):
        """显式激活单字符技能名（懒匹配恰好命中）。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "w", "name: w\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            assert router.detect_explicit("激活 w") == "w"

    def test_detect_explicit_no_match_returns_none(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            assert router.detect_explicit("今天天气怎么样") is None

    def test_detect_explicit_disabled_skill_returns_none(self, monkeypatch):
        """显式激活匹配到了技能名但技能 disabled → 返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\nenabled: false\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            assert router.detect_explicit("用 weather 技能查询") is None

    def test_detect_explicit_unknown_skill_returns_none(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # "foobar" not in manager
            assert router.detect_explicit("用 foobar 技能帮忙") is None


# ── match_by_intent ───────────────────────────────────────────

class TestMatchByIntent:
    def test_basic_intent_match(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\nintent_keywords:\n- 天气\nweight: 1.0\n"
            _write_skill(Path(td), "weather", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.match_by_intent("今天天气怎么样")
            assert len(matches) >= 1
            assert matches[0].name == "weather"

    def test_below_threshold_excluded(self, monkeypatch):
        """score < INTENT_THRESHOLD 时不被激活。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weak\nintent_keywords:\n- 微\nweight: 0.1\n"
            _write_skill(Path(td), "weak", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.match_by_intent("一条微弱信号")
            assert len(matches) == 0

    def test_semantic_disabled_skips_embedding(self, monkeypatch):
        """skills_config.semantic_routing=False 时不调用语义评分。"""
        class Config:
            semantic_routing = False
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: search\nintent_keywords:\n- 搜索\nweight: 1.0\n"
            _write_skill(Path(td), "search", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr, skills_config=Config())
            # 传入假 embedding，但语义路由被禁用
            fake_emb = [0.1] * 128
            matches = router.match_by_intent("帮我搜索一下", query_embedding=fake_emb)
            assert len(matches) >= 1
            assert matches[0].name == "search"

    def test_semantic_exception_is_caught(self, monkeypatch):
        """语义评分抛异常时不影响关键词主路径。"""
        monkeypatch.setattr("src.semantic.score_skill", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\nintent_keywords:\n- 天气\nweight: 1.0\n"
            _write_skill(Path(td), "weather", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # 即使语义评分失败，关键词匹配仍能命中
            matches = router.match_by_intent("今天天气不错", query_embedding=[0.1] * 128)
            assert len(matches) >= 1
            assert matches[0].name == "weather"


# ── match_by_keywords ────────────────────────────────────────

class TestMatchByKeywords:
    def test_legacy_name_match(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\nweight: 1.0\n"
            _write_skill(Path(td), "weather", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # 消息中直接包含技能名
            matches = router.match_by_keywords("用 weather 来帮我")
            assert len(matches) >= 1
            assert matches[0].name == "weather"
            # score = 5 (name match) + 可能 desc 匹配
            assert matches[0].score >= 5

    def test_legacy_name_words_match(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: web-search-tool\nweight: 1.0\n"
            _write_skill(Path(td), "web-search-tool", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # name_words: web(+2), search(+2), tool(+2) → 6 >= 4
            matches = router.match_by_keywords("web search tool")
            assert len(matches) >= 1
            assert matches[0].name == "web-search-tool"

    def test_legacy_desc_match(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: zzz\ndescription: 天气, 查询, 预报, 气象, 服务\nweight: 1.0\n"
            _write_skill(Path(td), "zzz", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            # desc_words: 天气(+1), 查询(+1), 预报(+1), 气象(+1), 服务(+1) → 5 >= 4
            matches = router.match_by_keywords("天气 查询 预报 气象 服务")
            assert len(matches) >= 1

    def test_below_threshold_excluded(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: zzz\nweight: 1.0\n"
            _write_skill(Path(td), "zzz", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.match_by_keywords("完全无关的消息内容")
            assert len(matches) == 0

    def test_disabled_skill_skipped(self, monkeypatch):
        """match_by_keywords 跳过 disabled 技能。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: offline\nenabled: false\nweight: 1.0\n"
            _write_skill(Path(td), "offline", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.match_by_keywords("offline")
            assert len(matches) == 0  # disabled，不会匹配


# ── _compute_goal_fit ────────────────────────────────────────

class TestGoalFit:
    def test_no_verbs_returns_zero(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            skill = mgr.get("weather")
            fit = router._compute_goal_fit("一条没有动作动词的消息", skill)
            assert fit == 0.0

    def test_verbs_in_skill_text(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: search\ndescription: 搜索与查询工具\n"
            _write_skill(Path(td), "search", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            skill = mgr.get("search")
            fit = router._compute_goal_fit("帮我搜索一下最新的论文", skill)
            assert fit > 0.0

    def test_verbs_not_in_skill(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\ndescription: 天气查询\n"
            _write_skill(Path(td), "weather", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            skill = mgr.get("weather")
            # "删除" 不在 weather 技能的文本中
            fit = router._compute_goal_fit("帮我删除这个文件", skill)
            assert fit == 0.0


# ── 综合路由 ─────────────────────────────────────────────────

class TestRouteCombo:
    def test_explicit_route(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.route_combo("用 weather 技能查天气")
            assert len(matches) == 1
            assert matches[0].source == "explicit"
            assert matches[0].name == "weather"

    def test_combo_routing(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\nintent_keywords:\n- 天气\nweight: 1.0\ncomposable: true\n"
            _write_skill(Path(td), "weather", fm)
            fm2 = "name: search\nintent_keywords:\n- 搜索\nweight: 1.0\ncomposable: true\n"
            _write_skill(Path(td), "search", fm2)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.route_combo("今天天气怎么样要不要搜索一下")
            assert len(matches) >= 1

    def test_combo_disabled_no_combination(self, monkeypatch):
        class Config:
            combo_enabled = False
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\nintent_keywords:\n- 天气\nweight: 1.0\ncomposable: true\n"
            _write_skill(Path(td), "weather", fm)
            fm2 = "name: search\nintent_keywords:\n- 搜索\nweight: 0.9\ncomposable: true\n"
            _write_skill(Path(td), "search", fm2)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr, skills_config=Config())
            matches = router.route_combo("今天天气怎么样要不要搜索一下")
            # combo 禁用，只保留一个
            assert len(matches) == 1

    def test_no_matches_returns_empty(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\nintent_keywords:\n- 天气\nweight: 0.1\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.route_combo("完全不相关的信息")
            assert matches == []

    def test_keyword_fallback_when_intent_empty(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            # 无 intent_keywords，auto-derive 从 description "工具" 产生 {工具} 关键词
            # 但消息不含「工具」，所以 intent 命中为 0 → 回退到 keyword 匹配
            fm = "name: zzz-tool\ndescription: 工具\nweight: 1.0\n"
            _write_skill(Path(td), "zzz-tool", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            matches = router.route_combo("zzz tool")
            assert len(matches) >= 1
            assert matches[0].source == "keyword"


# ── 单激活 route() 包装 ──────────────────────────────────────

class TestRoute:
    def test_route_returns_name_prompt(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\nbody: 天气技能正文\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            name, prompt = router.route("用 weather 技能查天气")
            assert name == "weather"
            assert prompt is not None

    def test_route_no_matches_returns_none(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\nintent_keywords:\n- 天气\nweight: 0.1\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            name, prompt = router.route("完全不相关")
            assert name is None
            assert prompt is None


# ── 后激活工具查询 ────────────────────────────────────────────

class PostActivationQueries:
    def test_combine_prompts(self, monkeypatch):
        m1 = SkillMatch(name="a", score=1.0, prompt="Prompt A", source="intent")
        m2 = SkillMatch(name="b", score=0.9, prompt="Prompt B", source="intent")
        router = SkillRouter.__new__(SkillRouter)
        router._last_matches = [m1, m2]
        combined = router.combine_prompts([m1, m2])
        assert "Prompt A" in combined
        assert "Prompt B" in combined

    def test_get_activated_tools(self):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\nallowed-tools: Bash, Python\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            router.route_combo("用 weather 技能")
            tools = router.get_activated_tools()
            assert "Bash" in tools
            assert "Python" in tools

    def test_get_activated_skill_name(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            router.route_combo("用 weather 技能")
            assert router.get_activated_skill_name() == "weather"

    def test_get_activated_skill_name_no_activation(self, monkeypatch):
        """未激活时返回 None。"""
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr)
            assert router.get_activated_skill_name() is None

    def test_get_activated_skill_names(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            _write_skill(Path(td), "weather", "name: weather\n")
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            router.route_combo("用 weather 技能")
            assert "weather" in router.get_activated_skill_names()

    def test_get_activated_fallback_tools(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            fm = "name: weather\nfallback_tools: web_search, send_message\n"
            _write_skill(Path(td), "weather", fm)
            mgr = SkillManager(td); mgr.reload()
            router = SkillRouter(mgr)
            router.route_combo("用 weather 技能")
            tools = router.get_activated_fallback_tools()
            assert "web_search" in tools
            assert "send_message" in tools

    def test_get_activated_fallback_tools_empty(self, monkeypatch):
        with tempfile.TemporaryDirectory() as td:
            _patch_skill_dirs(monkeypatch, td)
            mgr = SkillManager(td)
            router = SkillRouter(mgr)
            assert router.get_activated_fallback_tools() == []
