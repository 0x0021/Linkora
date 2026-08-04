"""技能路由器：根据用户消息匹配并激活技能。

路由流程（三层智能调度）：
1. 显式激活：用户说「用 {技能名}」则强制激活
2. 意图关键词匹配：消息命中技能的 intent_keywords → 按权重评分排序
3. 泛化关键词兜底：技能名/描述命中消息（低置信度回退）

激活后技能指令注入对话上下文，同时向智能引擎报告关联工具名，
参与工具暴露决策（_select_tools）。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass

from src.skills.loader import Skill
from src.skills.manager import SkillManager
from src import semantic as semantic_index

logger = logging.getLogger(__name__)


@dataclass
class SkillMatch:
    """一次技能匹配结果，包含技能名、得分和激活 prompt。"""
    name: str
    score: float
    prompt: str | None = None
    source: str = ""  # "explicit" | "intent" | "keyword"
    weight: float = 0.0      # 技能权重，用于确定性平局裁决（Phase 3）
    order: int = 0           # 声明/注册顺序，用于平局裁决确定性（Phase 3）


class SkillRouter:
    """将用户意图映射到已加载的技能。

    支持三种激活方式（优先级从高到低）：
    - 显式激活：用户消息含「用 XX 技能」「激活 XX」等
    - 意图关键词匹配：消息命中技能的 intent_keywords → 按权重评分
    - 关键词兜底匹配：技能名/描述词命中消息（仅低分回退时使用）

    激活阈值：
    - 显式激活：无条件
    - 意图匹配：score >= 0.3（即至少命中 30% 权重分）
    - 关键词兜底：score >= 4（旧行为保持）
    """

    # 意图匹配激活阈值（绝对分数，hits × weight 的下限）
    INTENT_THRESHOLD = 0.4
    # 关键词兜底激活阈值（旧行为的 4 分门槛）
    KEYWORD_THRESHOLD = 4

    def __init__(self, manager: SkillManager, skills_config=None, platform_id: str = ""):
        self._manager = manager
        self._skills_config = skills_config
        self._platform_id = (platform_id or "").lower()
        # 线程级状态隔离：SkillRouter 是 agent 的单实例，但 process_message
        # 可被多线程并发调用（reply_semaphore 允许并发）；若把最近路由结果存在
        # 普通实例属性上，并发请求会互相覆盖（请求 B 的 route_combo 覆盖 A 的
        # last_match，导致 A 注入 B 的技能 / 记录错技能名）。改用 threading.local
        # 确保每线程独立（与 agent._tl 同思路）。
        self._tl = threading.local()
        self._tl.last_match: SkillMatch | None = None
        self._tl.last_matches: list[SkillMatch] = []
        # Phase 4 路由质量数据（给 agent 记录用）
        self._tl.last_routing_detail: dict = {}

    def _is_skill_for_platform(self, skill: Skill) -> bool:
        """检查技能是否适用于当前平台。

        platforms 为空列表时表示通用技能（全平台可用）；
        否则只有当当前平台在 platforms 列表中时才可用。
        """
        if not self._platform_id:
            return True
        platforms = getattr(skill, "platforms", None) or []
        if not platforms:
            return True
        return self._platform_id in [p.lower() for p in platforms]

    def _semantic_enabled(self) -> bool:
        """语义路由是否启用（默认开启，可经 config.skills.semantic_routing 关闭）。"""
        if self._skills_config is not None:
            return bool(getattr(self._skills_config, "semantic_routing", True))
        return True

    def _combo_enabled(self) -> bool:
        """组合激活是否启用（默认开启，可经 config.skills.combo_enabled 关闭）。"""
        if self._skills_config is not None:
            return bool(getattr(self._skills_config, "combo_enabled", True))
        return True

    def _combo_gap(self) -> float:
        """组合激活的 score 差距阈值（主 - 副 <= gap 才组合）。"""
        if self._skills_config is not None:
            return float(getattr(self._skills_config, "combo_gap", 0.12))
        return 0.12

    # ── 动作动词集合（Phase 4 目标感知收敛）────────────────────

    # 中文动作动词列表，按抽象领域分组。
    # 用于：① 从消息中检测用户的「可操作目标」；② 对同分技能按动词-领域
    # 重叠度做收敛裁决（替代纯 order 平局）。
    _ACTION_VERBS: dict[str, list[str]] = {
        "retrieve": ["查询", "搜索", "查", "找", "搜"],
        "create":   ["创建", "开", "开通", "申请", "注册", "新建", "增加"],
        "update":   ["修改", "更新", "编辑", "变更", "改"],
        "delete":   ["删除", "取消", "关", "停用", "移除"],
        "send":     ["发送", "通知", "提醒", "发"],
        "view":     ["查看", "显示", "展示", "浏览", "看"],
        "configure":["设置", "配置", "设定", "调整"],
        "analyze":  ["统计", "分析", "汇总", "报告", "报表"],
        "approve":  ["审批", "审核", "同意", "拒绝", "通过"],
    }

    @classmethod
    def _detect_action_domains(cls, message: str) -> set[str]:
        """从消息中检测动作动词所属的抽象领域。"""
        domains: set[str] = set()
        for domain, verbs in cls._ACTION_VERBS.items():
            for v in verbs:
                if v in message:
                    domains.add(domain)
                    break
        return domains

    def _compute_goal_fit(self, message: str, skill) -> float:
        """计算技能与消息中动作意图的匹配度 (0~1)。

        收集消息中的所有动作动词，统计其中有多少个的词根也出现在
        技能的 name / description / intent_categories / semantic_text 中。
        """
        # collect action verbs from message
        msg_verbs: set[str] = set()
        for verbs in self._ACTION_VERBS.values():
            for v in verbs:
                if v in message:
                    msg_verbs.add(v)
        if not msg_verbs:
            return 0.0

        # build skill text
        parts = [
            getattr(skill, "name", "") or "",
            getattr(skill, "description", "") or "",
            getattr(skill, "semantic_text", "") or "",
        ]
        categories = list(getattr(skill, "intent_categories", []) or [])
        parts.extend(categories)
        skill_text = " ".join(parts).lower()

        # count how many action verbs appear in skill text
        matches = sum(1 for v in msg_verbs if v in skill_text)
        return round(matches / len(msg_verbs), 3)

    @property
    def last_match(self) -> SkillMatch | None:
        """最近一次路由的主匹配结果（组合激活时为 score 最高的那个）。"""
        return self._tl.last_match

    @property
    def last_matches(self) -> list[SkillMatch]:
        """最近一次路由激活的全部技能（组合激活时为多个，单激活时为 1 个）。"""
        return self._tl.last_matches

    # ── 显式激活检测 ──────────────────────────────────────────

    _EXPLICIT_PATTERNS = [
        re.compile(r"用\s*(\S+)\s*(?:技能|skill)", re.IGNORECASE),
        re.compile(r"激活\s*(\S+)(?:\s*(?:技能|skill))?", re.IGNORECASE),
        re.compile(r"use[_ ]*(\S+)(?:\s*skill)?", re.IGNORECASE),
        re.compile(r"启动\s*(\S+)(?:\s*(?:技能|skill))?", re.IGNORECASE),
    ]

    def detect_explicit(self, message: str) -> str | None:
        """检测用户是否显式要求激活某个技能。返回技能名。"""
        for pattern in self._EXPLICIT_PATTERNS:
            m = pattern.search(message)
            if m:
                name = m.group(1).strip().lower().replace(" ", "-").replace("_", "-")
                skill = self._manager.get(name)
                if skill and skill.enabled and self._is_skill_for_platform(skill):
                    logger.info("[SkillRouter] 显式激活: %s", skill.name)
                    return skill.name
        return None

    # ── 意图关键词匹配（智能引擎核心）─────────────────────────

    def match_by_intent(self, message: str, query_embedding: list[float] | None = None) -> list[SkillMatch]:
        """根据技能声明的 intent_keywords 匹配用户消息，按加权得分排序。

        Phase 2 语义路由：得分 = max(关键词命中, 语义相似度) × weight。
        - 关键词命中：hits × weight（每命中一个关键词叠加权重分，与旧行为一致）；
        - 语义相似度：query 与技能 semantic_text 的余弦相似度 × weight，
          覆盖同义改写/错别字/口语化表达（子串未命中时的兜底）。
        embedding 不可用时 sim=None，评分自动回退为纯关键词（行为不变）。

        返回按 score 降序排列的匹配列表。
        """
        text_lower = message.lower()
        results: list[SkillMatch] = []

        semantic_on = self._semantic_enabled()
        use_semantic = semantic_on and query_embedding is not None \
            and semantic_index.get_embedding_client() is not None

        for order, skill in enumerate(self._manager.list_all()):
            if not skill.enabled or not self._is_skill_for_platform(skill):
                continue
            # 优先用 effective_intent_keywords（声明 intent_categories 时经注册表解析，
            # 否则回退字面/自动推导词），保证单一真源与向后兼容。

            hits = 0
            for kw in skill.effective_intent_keywords:
                kw_lower = kw.lower()
                if len(kw) <= 3 and kw.isascii() and kw.isalpha():
                    # 短英文词边界匹配
                    if re.search(r"\b" + re.escape(kw_lower) + r"\b", text_lower):
                        hits += 1
                elif kw_lower in text_lower:
                    hits += 1

            # 语义相似度（embedding 不可用 / 未启用时 sim=None，回退纯关键词）
            sim = None
            if use_semantic:
                try:
                    sim = semantic_index.score_skill(
                        query_embedding, skill.name, skill.semantic_text)
                except Exception as e:   # 防御：语义层异常不影响关键词主路径
                    logger.warning("[SkillRouter] 语义评分失败 %s: %s", skill.name, e)
                    sim = None

            # 评分：max(关键词命中, 语义相似度) × weight
            # 关键词权重按命中数线性累加（与旧行为一致），语义按 weight 缩放对齐 stickiness。
            kw_score = hits * skill.weight
            sem_score = (sim * skill.weight) if sim is not None else 0.0
            score = round(max(kw_score, sem_score), 3)

            if score >= self.INTENT_THRESHOLD:
                prompt = self._manager.activate_prompt(skill.name)
                results.append(SkillMatch(
                    name=skill.name,
                    score=score,
                    prompt=prompt,
                    source="intent",
                    weight=skill.weight,
                    order=order,
                ))

        # 确定性排序：score 降序；平局时 weight 高者优先；再平局按声明/注册顺序（order）。
        results.sort(key=lambda m: (-m.score, -m.weight, m.order))
        return results

    # ── 关键词兜底匹配 ────────────────────────────────────────

    def match_by_keywords(self, message: str) -> list[SkillMatch]:
        """关键词兜底匹配（技能名/描述命中消息，旧行为保留）。

        仅在技能无 intent_keywords 或意图匹配无结果时使用。
        """
        text_lower = message.lower()
        results: list[SkillMatch] = []

        for order, skill in enumerate(self._manager.list_all()):
            if not skill.enabled or not self._is_skill_for_platform(skill):
                continue
            score = self._score_skill_legacy(skill, text_lower)
            if score >= self.KEYWORD_THRESHOLD:
                prompt = self._manager.activate_prompt(skill.name)
                results.append(SkillMatch(
                    name=skill.name,
                    score=float(score),
                    prompt=prompt,
                    source="keyword",
                    weight=skill.weight,
                    order=order,
                ))

        # 确定性排序：score 降序；平局时 weight 高者优先；再平局按声明/注册顺序（order）。
        results.sort(key=lambda m: (-m.score, -m.weight, m.order))
        return results

    def _score_skill_legacy(self, skill: Skill, text_lower: str) -> int:
        """旧版关键词评分（保持向后兼容）。"""
        score = 0
        name_lower = skill.name.lower()
        if name_lower in text_lower:
            score += 5

        name_words = set(name_lower.replace("-", " ").split())
        for w in name_words:
            if len(w) >= 3 and w in text_lower:
                score += 2

        desc_lower = skill.description.lower()
        desc_words = set(
            w for w in re.split(r"[，,、\s]+", desc_lower)
            if len(w) >= 2 and not w.isdigit()
        )
        for w in desc_words:
            if w in text_lower:
                score += 1

        return score

    # ── 综合路由 ──────────────────────────────────────────────

    # 目标收敛窗口：与最高分差距 <= 此阈值即进入收敛裁决（避免纯 order 决定平局）
    _CONVERGENCE_EPSILON = 0.005

    def route_combo(self, message: str, query_embedding: list[float] | None = None) -> list[SkillMatch]:
        """Phase 3 组合路由 + Phase 4 目标感知收敛：返回确定性排序的激活技能列表。

        裁决顺序（完全确定性，已写入单测）：
          1. 显式激活 → 单一 [explicit]，不组合；
          2. 意图/关键词匹配 → 按 (-score, -weight, order) 确定性排序的全部达阈值候选；
          3. Phase 4 收敛：与最高分差距 <= _CONVERGENCE_EPSILON 的候选，
             按 (-score, -weight, -goal_fit, order) 重排序，让同分技能收敛于
             用户消息中的动作动词所指示的领域；
          4. 组合激活：在主激活之外，挑选满足「可组合」条件的副技能。
        """
        self._tl.last_match = None
        self._tl.last_matches = []

        # 第 1 层：显式激活（单一，不组合）
        name = self.detect_explicit(message)
        if name:
            skill = self._manager.get(name)
            prompt = self._manager.activate_prompt(name)
            m = SkillMatch(name=name, score=1.0, prompt=prompt, source="explicit",
                           weight=skill.weight if skill else 0.0, order=0)
            self._tl.last_match = m
            self._tl.last_matches = [m]
            self._tl.last_routing_detail = {}
            return self._tl.last_matches

        # 第 2 层：意图匹配（含语义兜底）
        matches = self.match_by_intent(message, query_embedding=query_embedding)
        if not matches:
            # 第 3 层：关键词兜底
            matches = self.match_by_keywords(message)
        if not matches:
            self._tl.last_routing_detail = {}
            return []

        # ----- Phase 4: 目标感知收敛（平局区间内按 goal_fit 重排序） -----
        top_score = matches[0].score
        conv_eps = self._CONVERGENCE_EPSILON
        # 收集收敛区间的候选索引及 goal_fit
        conv_info: list[tuple[int, SkillMatch, float]] = []
        non_conv_indices: list[int] = []
        for i, m in enumerate(matches):
            if m.score >= top_score - conv_eps:
                skill_obj = self._manager.get(m.name)
                goal_fit = self._compute_goal_fit(message, skill_obj) if skill_obj else 0.0
                conv_info.append((i, m, goal_fit))
            else:
                non_conv_indices.append(i)

        if len(conv_info) > 1:
            # 收敛区间内的候选按 (-score, -weight, -goal_fit, order) 重排序
            conv_info.sort(key=lambda x: (-x[1].score, -x[1].weight, -x[2], x[1].order))
            conv_str = "; ".join(f"{x[1].name}={x[2]:.2f}" for x in conv_info)
            logger.info("[SkillRouter] Phase 4 收敛: %s", conv_str)
            # 重建 matches
            matches = [x[1] for x in conv_info] + [matches[i] for i in non_conv_indices]

        # 记录路由质量（供 agent 持久化用）
        goal_fit_dict = {x[1].name: round(x[2], 3) for x in conv_info}
        self._tl.last_routing_detail = {
            "candidates_count": len(matches),
            "convergence_zone_size": len(conv_info),
            "convergence_applied": 1 if len(conv_info) > 1 else 0,
            "goal_fit_details": goal_fit_dict,
        }
        # ------------------------------------------------------------------

        # 各 match_* 已完成确定排序。
        activated: list[SkillMatch] = [matches[0]]

        if self._combo_enabled():
            gap = self._combo_gap()
            for m in matches[1:]:
                skill = self._manager.get(m.name)
                if not skill or not getattr(skill, "composable", False):
                    continue
                if m.score >= top_score - gap:
                    # 组合副技能也记录收敛源
                    activated.append(m)

        self._tl.last_match = activated[0]
        self._tl.last_matches = activated
        return activated

    def route(self, message: str, query_embedding: list[float] | None = None) -> tuple[str | None, str | None]:
        """综合路由（向后兼容的单激活包装）。

        返回 (activated_skill_name, prompt_injection) 或 (None, None)。
        需组合激活语义时请改用 route_combo() + combine_prompts()。
        query_embedding：消息向量（Phase 2 语义路由用），为 None 时退化为纯关键词。
        """
        matches = self.route_combo(message, query_embedding=query_embedding)
        if matches:
            m = matches[0]
            tag = f" [组合 +{len(matches) - 1}]" if len(matches) > 1 else ""
            logger.info("[SkillRouter] 激活技能: %s (source=%s, score=%.2f)%s",
                        m.name, m.source, m.score, tag)
            return m.name, m.prompt
        return None, None

    def combine_prompts(self, matches: list[SkillMatch]) -> str:
        """将多个激活技能的 prompt 合并为单个注入字符串（Phase 3 组合激活）。"""
        parts = [m.prompt for m in matches if m.prompt]
        return "\n\n".join(parts)

    # ── 关联工具查询（供智能引擎暴露工具）─────────────────────

    def get_activated_tools(self) -> list[str]:
        """返回当前激活技能关联的工具名列表。

        工具名来自 SKILL.md frontmatter 的 allowed-tools 字段。
        智能引擎可据此决定本轮是否暴露这些工具给 LLM。
        """
        if not self._tl.last_matches:
            return []
        out: list[str] = []
        for m in self._tl.last_matches:
            skill = self._manager.get(m.name)
            if skill:
                out.extend(skill.allowed_tools)
        # 去重保序（未注册工具由上游 _select_tools 做可用集交集过滤）
        seen: set[str] = set()
        return [t for t in out if not (t in seen or seen.add(t))]

    def get_activated_skill_name(self) -> str | None:
        """返回主激活技能名（组合激活时为 score 最高的那个），未激活返回 None。"""
        return self._tl.last_matches[0].name if self._tl.last_matches else None

    def get_activated_skill_names(self) -> list[str]:
        """返回全部激活技能名（组合激活时为多个）。"""
        return [m.name for m in self._tl.last_matches]

    def get_activated_fallback_tools(self) -> list[str]:
        """返回所有激活技能声明的 fallback_tools 并集（故障时回退的内置工具名）。"""
        if not self._tl.last_matches:
            return []
        out: list[str] = []
        for m in self._tl.last_matches:
            skill = self._manager.get(m.name)
            if skill:
                out.extend(skill.fallback_tools)
        seen: set[str] = set()
        return [t for t in out if not (t in seen or seen.add(t))]
