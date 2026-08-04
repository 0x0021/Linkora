"""Phase 4 目标感知收敛测试。

验证 SkillRouter 在同分/近平局的技能候选之间，能按消息中的动作动词
收敛于领域最匹配的技能（而非纯 order 裁决）。

不加载真实 embedding，用 FakeManager + 直接构造 Skill。
"""

import pytest

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
               allowed=None, fallback=None, body="", description=None,
               intent_categories=None):
    return Skill(
        name=name,
        description=description or name,
        body=body or f"body-{name}",
        intent_keywords=list(keywords),
        weight=weight,
        composable=composable,
        allowed_tools=allowed or [],
        fallback_tools=fallback or [],
        intent_categories=intent_categories or [],
    )


# ============================================================================
# 动词检测
# ============================================================================

class TestDetectActionDomains:
    def test_simple_retrieve(self):
        assert SkillRouter._detect_action_domains("查天气") == {"retrieve"}

    def test_simple_create(self):
        assert SkillRouter._detect_action_domains("创建账号") == {"create"}

    def test_multiple_domains(self):
        r = SkillRouter._detect_action_domains("查询并分析数据")
        assert "retrieve" in r
        assert "analyze" in r

    def test_no_action_verb(self):
        assert SkillRouter._detect_action_domains("今天天气怎么样") == set()

    def test_send_domain(self):
        assert "send" in SkillRouter._detect_action_domains("发送通知")
        assert "send" in SkillRouter._detect_action_domains("提醒我")


# ============================================================================
# goal_fit 计算
# ============================================================================

class TestComputeGoalFit:
    def test_retrieve_verb_matches_description(self):
        """技能 description 含"查询" → 对"查天气"的 goal_fit > 0。"""
        router = SkillRouter(FakeManager([]))
        s = make_skill("weather", ["天气"], description="查询天气信息")
        fit = router._compute_goal_fit("查一下天气", s)
        assert fit > 0.0

    def test_no_verb_no_match(self):
        """消息中无动作动词 → goal_fit = 0。"""
        router = SkillRouter(FakeManager([]))
        s = make_skill("weather", ["天气"], description="查询天气信息")
        fit = router._compute_goal_fit("今天天气", s)
        assert fit == 0.0

    def test_intent_categories_contain_verb(self):
        """intent_categories + description 含动词 → match。"""
        router = SkillRouter(FakeManager([]))
        s = make_skill("weather", ["天气"], description="查询今日天气",
                       intent_categories=["domain.weather"])
        fit = router._compute_goal_fit("查天气", s)
        assert fit > 0.0  # "查询" in desc → "查" in msg → match

    def test_semantic_text_contains_verb(self):
        """semantic_text 含动词 → match。"""
        router = SkillRouter(FakeManager([]))
        s = make_skill("docs", ["教程"], description="返回历史记录")
        # semantic_text = name + description + keywords
        # "docs 返回历史记录 教程" → "查" 不在其中
        fit = router._compute_goal_fit("查教程", s)
        assert fit == 0.0  # no match because skill doesn't mention "查"/"查询"/"搜索"/"找"/"搜"

    def test_multiple_verbs_partial_match(self):
        """消息中含多个动作动词，部分命中 → 分数按比例。"""
        router = SkillRouter(FakeManager([]))
        s = make_skill("weather", ["天气"], description="分析趋势风向")
        # msg "查询分析" → msg_verbs: 查询, 查, 分析 → 3 verbs
        # skill has "分析" → 1/3 match ≈ 0.333
        fit = router._compute_goal_fit("查询分析未来天气", s)
        assert fit == pytest.approx(0.333, abs=0.001)


# ============================================================================
# 收敛路由：同分技能按 goal_fit 重排序
# ============================================================================

class TestConvergenceRoute:
    def test_convergence_breaks_order_tie(self):
        """两个同分同权技能，有动词匹配的应收敛为第一（而非 order 优先）。"""
        search = make_skill("search", ["天气"], weight=0.5,
                            description="查询天气信息，查温度湿度")
        other = make_skill("other", ["天气"], weight=0.5,
                           description="记录历史天气记录")
        # list_all 顺序 [search, other] → search.order=0, other.order=1
        # Phase 3: search wins by order ✓
        # 验证即使交换顺序，动词收敛也能正确翻转：
        router = SkillRouter(FakeManager([other, search]))  # other.order=0, search.order=1
        activated = router.route_combo("查一下天气")
        assert len(activated) >= 1
        # 尽管 search 在 list_all 中 order 靠后，动词匹配应让它收敛为第一
        assert activated[0].name == "search"

    def test_no_convergence_without_action_verb(self):
        """消息中无动作动词 → 不走收敛，纯 order 裁决。"""
        a = make_skill("a", ["天气"], weight=0.5, description="查询天气")
        b = make_skill("b", ["天气"], weight=0.5, description="记录天气")
        # order: b=0, a=1
        router = SkillRouter(FakeManager([b, a]))
        activated = router.route_combo("今天天气怎么样")
        assert activated[0].name == "b"  # order 0 wins

    def test_divergent_scores_not_affected(self):
        """score 差距 > epsilon 时收敛不生效。"""
        high = make_skill("high", ["天气", "下雨"], weight=0.9)   # score=1.8
        low = make_skill("low", ["天气"], weight=0.5)              # score=0.5
        router = SkillRouter(FakeManager([low, high]))
        activated = router.route_combo("天气下雨")
        assert activated[0].name == "high"  # 1.8 >> 0.5

    def test_explicit_activation_bypasses_convergence(self):
        """显式激活不走收敛。"""
        s = make_skill("echo", ["回复"], description="重复消息")
        router = SkillRouter(FakeManager([s]))
        activated = router.route_combo("用echo技能")
        assert len(activated) == 1
        assert activated[0].name == "echo"
        assert activated[0].source == "explicit"

    def test_convergence_with_combo_preserves_sub_skills(self):
        """收敛后组合激活的副技能仍然保留。"""
        main = make_skill("search", ["天气"], weight=0.5,
                          description="查询天气", composable=False)
        sub = make_skill("remind", ["天气"], weight=0.4,
                         description="提醒天气变化", composable=True)
        # order: search=0, remind=1 → score: search=0.5, remind=0.4
        # combo gap = 0.12 → search(0.5) - remind(0.4) = 0.1 <= 0.12 → composable ✓
        router = SkillRouter(FakeManager([main, sub]))
        activated = router.route_combo("查天气")
        assert len(activated) == 2
        assert activated[0].name == "search"
        assert activated[1].name == "remind"

    def test_goal_fit_with_combo_reorders_ties(self):
        """平局候选在收敛 + 组合同时生效时的顺序。"""
        main = make_skill("main", ["天气"], weight=0.5,
                          description="记录天气", composable=True)  # 可组合副技能
        verb_match = make_skill("verb_match", ["天气"], weight=0.5,
                                description="查询天气", composable=False)
        # order: main=0, verb_match=1 → same score 0.5, same weight 0.5
        # Phase 3: main wins (order 0)
        # Phase 4: verb_match has 查询 in desc, message "查天气" → goal_fit > 0 → converges
        router = SkillRouter(FakeManager([main, verb_match]))
        activated = router.route_combo("查天气")
        assert len(activated) == 2
        assert activated[0].name == "verb_match"  # 收敛翻转为主激活
        # main 是 composable=True 的副技能，score 差距 0.5 - 0.5 = 0 ≤ combo_gap 0.12
        assert activated[1].name == "main"         # composable 副技能保留
