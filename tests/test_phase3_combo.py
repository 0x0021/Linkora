"""Phase 3 技能组合激活与确定性平局裁决测试。

不加载真实 embedding（semantic 客户端未 set → 退化为纯关键词），
用 FakeManager + 直接构造 Skill 验证路由逻辑。
"""


from src.skills.loader import Skill
from src.skills.router import SkillRouter


class FakeManager:
    """轻量 SkillManager 替身，仅实现 router 依赖的接口。"""

    def __init__(self, skills):
        self._skills = {s.name: s for s in skills}

    def list_all(self):
        return list(self._skills.values())

    def get(self, name):
        return self._skills.get(name)

    def activate_prompt(self, name):
        s = self._skills.get(name)
        return f"\n\n【已激活技能：{name}】\n{s.body}" if s else None


def make_skill(name, keywords, weight=0.5, composable=False,
               allowed=None, fallback=None, body=""):
    return Skill(
        name=name,
        description=name,
        body=body or f"body-{name}",
        intent_keywords=list(keywords),
        weight=weight,
        composable=composable,
        allowed_tools=allowed or [],
        fallback_tools=fallback or [],
    )


# ── 1. 确定性平局裁决：相同 score 时按声明顺序（order）─────────────

def test_tie_broken_by_declaration_order():
    a = make_skill("a", ["天气"], weight=0.5)
    b = make_skill("b", ["天气"], weight=0.5)
    # list_all 顺序 [a, b] → a.order=0, b.order=1
    router = SkillRouter(FakeManager([a, b]))
    matches = router.match_by_intent("今天天气怎么样")
    assert len(matches) == 2
    assert matches[0].name == "a"          # 平局按声明顺序，先注册优先
    assert matches[0].score == matches[1].score == 0.5
    assert matches[0].order == 0 and matches[1].order == 1


# ── 2. 平局裁决：score 相同时 weight 高者优先 ─────────────────────

def test_tie_broken_by_weight():
    # a: hits=2 * 0.2 = 0.4 ; b: hits=1 * 0.4 = 0.4 → 同 score，b 的 weight 更高
    a = make_skill("a", ["天气", "下雨"], weight=0.2)
    b = make_skill("b", ["天气"], weight=0.4)
    router = SkillRouter(FakeManager([a, b]))
    matches = router.match_by_intent("天气下雨")
    assert matches[0].score == matches[1].score == 0.4
    assert matches[0].name == "b"          # weight 0.4 > 0.2 → b 优先
    assert matches[0].weight == 0.4


# ── 3. 组合激活：composable 副技能接近平局时被组合 ──────────────────

def test_combo_activates_composable_sub():
    main = make_skill("main", ["天气"], weight=0.9,
                      allowed=["weather_tool"], composable=False)
    sub = make_skill("sub", ["天气"], weight=0.8,
                     allowed=["remind_tool"], composable=True)
    router = SkillRouter(FakeManager([main, sub]))
    activated = router.route_combo("今天天气")
    assert len(activated) == 2
    assert activated[0].name == "main"
    assert activated[1].name == "sub"
    # 主激活名仍是 top-1
    assert router.get_activated_skill_name() == "main"
    # 工具暴露为两技能并集
    assert set(router.get_activated_tools()) == {"weather_tool", "remind_tool"}
    assert router.get_activated_skill_names() == ["main", "sub"]
    # 组合 prompt 含两段注入
    assert router.combine_prompts(activated).count("【已激活技能") == 2


# ── 4. 不组合：副技能未声明 composable ─────────────────────────────

def test_no_combo_when_sub_not_composable():
    main = make_skill("main", ["天气"], weight=0.9, composable=False)
    sub = make_skill("sub", ["天气"], weight=0.8, composable=False)
    router = SkillRouter(FakeManager([main, sub]))
    activated = router.route_combo("今天天气")
    assert len(activated) == 1
    assert activated[0].name == "main"


# ── 5. combo_gap 边界：副技能 score 差距过大不组合 ──────────────────

def test_combo_gap_boundary():
    main = make_skill("main", ["天气"], weight=0.9, composable=False)
    sub = make_skill("sub", ["天气"], weight=0.7, composable=True)  # 0.9-0.7=0.2 > 0.12
    router = SkillRouter(FakeManager([main, sub]))
    activated = router.route_combo("今天天气")
    assert len(activated) == 1


# ── 6. combo_enabled=False 退回纯单激活（行为与 Phase 2 一致）──────

def test_combo_disabled_falls_back_to_single():
    main = make_skill("main", ["天气"], weight=0.9, composable=False)
    sub = make_skill("sub", ["天气"], weight=0.8, composable=True)
    config = type("C", (), {"combo_enabled": False, "combo_gap": 0.12})()
    router = SkillRouter(FakeManager([main, sub]), skills_config=config)
    activated = router.route_combo("今天天气")
    assert len(activated) == 1


# ── 7. 自定义 combo_gap 放宽门槛 ───────────────────────────────────

def test_custom_combo_gap():
    main = make_skill("main", ["天气"], weight=0.9, composable=False)
    sub = make_skill("sub", ["天气"], weight=0.7, composable=True)  # 差 0.2
    config = type("C", (), {"combo_enabled": True, "combo_gap": 0.3})()
    router = SkillRouter(FakeManager([main, sub]), skills_config=config)
    activated = router.route_combo("今天天气")
    assert len(activated) == 2


# ── 8. route() 向后兼容：返回 top-1 (name, prompt) ─────────────────

def test_route_backward_compatible_top1():
    main = make_skill("main", ["天气"], weight=0.9, composable=False)
    sub = make_skill("sub", ["天气"], weight=0.8, composable=True)
    router = SkillRouter(FakeManager([main, sub]))
    name, prompt = router.route("今天天气")
    assert name == "main"
    assert prompt is not None and "main" in prompt


# ── 9. 显式激活不组合（单一 explicit）──────────────────────────────

def test_explicit_activation_not_combo():
    main = make_skill("main", ["天气"], weight=0.9, composable=False)
    sub = make_skill("sub", ["天气"], weight=0.8, composable=True)
    router = SkillRouter(FakeManager([main, sub]))
    activated = router.route_combo("用 main 技能帮我查天气")
    assert len(activated) == 1
    assert activated[0].source == "explicit"


# ── 10. 无命中返回空 ───────────────────────────────────────────────

def test_no_match_returns_empty():
    main = make_skill("main", ["天气"], weight=0.9)
    router = SkillRouter(FakeManager([main]))
    assert router.route_combo("今天吃了啥") == []
    assert router.get_activated_tools() == []
