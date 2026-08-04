"""意图注册表核心实现。

包含 IntentRegistry 类、TOOL_ACTION_MAP 和 default_registry 实例。
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Optional

from src.intent.types import IntentCategory, DispositionResult, LAYER_DISPOSITION, LAYER_ACTION
from src.intent.matching import match_keyword, _SOCIAL_PRIORITY
from src.intent.categories_disposition import DEFAULT_INTENTS
from src.intent.categories_action import ACTION_INTENTS
from src.intent.categories_domain import DOMAIN_INTENTS

logger = logging.getLogger(__name__)

# 合并所有意图类别
_ALL_INTENTS: list[IntentCategory] = DEFAULT_INTENTS + ACTION_INTENTS + DOMAIN_INTENTS

# 工具 → 抽象行动意图 的中心化映射
TOOL_ACTION_MAP: dict[str, list[str]] = {
    # 基础工具
    "send_message": ["action.execute", "action.communicate"],
    "save_memory": ["action.analyze", "action.subscribe"],
    "recall_memory": ["action.query", "action.analyze"],
    # 查询类
    "web_search": ["action.query"],
    "get_weather": ["action.query"],
    "kb_search": ["action.query"],
    "search_doc": ["action.query"],
    "get_doc_content": ["action.query"],
    "search_contact": ["action.query"],
    "get_calendar_events": ["action.query"],
    "get_attendance": ["action.query"],
    "get_my_profile": ["action.query"],
    "list_orgs": ["action.query"],
    "get_current_org": ["action.query"],
    "system_status": ["action.query"],
    "message_stats": ["action.query"],
    "keyword_rules": ["action.query"],
    "config_manage": ["action.query", "action.execute"],
    "get_unread": ["action.query", "action.communicate"],
    "get_conversation_info": ["action.query", "action.communicate"],
    "search_messages": ["action.query", "action.communicate"],
    # 执行/通讯/媒体类
    "create_todo": ["action.execute", "action.monitor", "action.subscribe"],
    "send_ding": ["action.execute", "action.communicate", "action.monitor", "action.subscribe"],
    "transfer_approval": ["action.execute", "action.communicate"],
    "upload_image": ["action.execute", "action.media"],
    # AI 听记 / 会议纪要：只读查询 + 内容提取
    "list_minutes": ["action.query", "action.analyze"],
    "get_minutes": ["action.query", "action.analyze"],
    # 钉钉知识库（wiki）只读查询
    "wiki_space_list": ["action.query"],
    "wiki_space_search": ["action.query"],
    "wiki_node_list": ["action.query"],
    "wiki_node_search": ["action.query"],
    # 钉钉 OA 审批只读查询
    "approval_list_forms": ["action.query"],
    "approval_search_forms": ["action.query"],
    "approval_get_detail": ["action.query"],
    "approval_list_pending": ["action.query"],
    "approval_list_tasks": ["action.query"],
    "approval_list_initiated": ["action.query"],
    "approval_list_executed": ["action.query"],
    # 钉钉 OA 审批：我的审批 / 审批详情（与 approval_* 同类只读查询）
    "get_my_approvals": ["action.query"],
    "get_approval_detail": ["action.query"],
}


class IntentRegistry:
    """抽象意图注册表：分类体系 + 匹配逻辑，声明式、可扩展。"""

    def __init__(self, categories: list[IntentCategory] | None = None):
        self._cats: dict[str, IntentCategory] = {}
        for c in (categories or _ALL_INTENTS):
            self._cats[c.id] = copy.deepcopy(c)
        self._business_ratio_threshold: float = 0.3

    # ---- 注册 / 查询 ----------------------------------------------------
    def register(self, category: IntentCategory) -> None:
        """注册/覆盖一个意图类别（扩展点）。"""
        self._cats[category.id] = category

    def get(self, category_id: str) -> Optional[IntentCategory]:
        return self._cats.get(category_id)

    def all(self) -> list[IntentCategory]:
        return list(self._cats.values())

    def tool_action_categories(self, tool_name: str) -> list[str]:
        return TOOL_ACTION_MAP.get(tool_name, [])

    def tools_for_action_category(self, cat_id: str) -> list[str]:
        """反向查 TOOL_ACTION_MAP：返回声明了该行动意图的工具名列表。"""
        return [t for t, cs in TOOL_ACTION_MAP.items() if cat_id in cs]

    # ---- 域类别（路由单一真源）解析 ----------------------------------
    def keywords_for_categories(self, category_ids: list[str] | None) -> list[str]:
        """把一个工具/技能声明的 intent_categories 解析为关键词列表（单一真源）。"""
        if not category_ids:
            return []
        result: list[str] = []
        for cid in category_ids:
            cat = self._cats.get(cid)
            if cat is None:
                logger.warning("[意图] 工具/技能引用了未注册的意图类别: %s（已跳过）", cid)
                continue
            for kw in cat.evidence_keywords:
                if kw not in result:
                    result.append(kw)
        return result

    def validate_tool_intent_categories(self, tool_name: str, category_ids: list[str] | None) -> None:
        """校验工具声明的 intent_categories 是否都已在注册表注册（防漂移）。"""
        if not category_ids:
            return
        for cid in category_ids:
            if cid not in self._cats:
                logger.warning(
                    "[意图校验] 工具 %s 声明的意图类别未注册（路由盲区）: %s",
                    tool_name, cid,
                )

    # ---- 关键词覆盖（向后兼容 config.intent_filter） --------------------
    def apply_intent_filter(self, intent_filter: dict) -> None:
        """用 config.intent_filter 中的关键词/阈值合并追加到默认证据词。"""
        if not intent_filter:
            return

        key_map = {
            "business_keywords": "business",
            "thank_you": "social.gratitude",
            "acknowledge": "social.acknowledge",
            "closing": "social.closing",
            "polite": "social.polite",
            "compliment": "social.compliment",
            "smalltalk": "social.smalltalk",
            "emotion": "social.emotion",
        }
        stats = {}
        for cfg_key, cat_id in key_map.items():
            kws = intent_filter.get(cfg_key)
            if isinstance(kws, list) and kws:
                cat = self._cats.get(cat_id)
                if cat is not None:
                    existing = set(cat.evidence_keywords)
                    added = [k for k in kws if k not in existing]
                    cat.evidence_keywords = existing | set(kws)
                    cat.evidence_keywords = list(existing) + added
                    stats[cfg_key] = len(added)

        if "pure_thank_max_length" in intent_filter:
            try:
                self._cats["social.gratitude"].max_length = int(intent_filter["pure_thank_max_length"])
            except (TypeError, ValueError) as e:
                logger.debug("social.gratitude max_length 解析失败: %s", e)
        if "pure_ack_max_length" in intent_filter:
            try:
                self._cats["social.acknowledge"].max_length = int(intent_filter["pure_ack_max_length"])
            except (TypeError, ValueError) as e:
                logger.debug("social.acknowledge max_length 解析失败: %s", e)

        brt = intent_filter.get("business_ratio_threshold")
        if brt is not None:
            try:
                self._business_ratio_threshold = float(brt)
            except (TypeError, ValueError) as e:
                logger.debug("business_ratio_threshold 解析失败: %s", e)

        domain_overrides = intent_filter.get("domain_overrides")
        if isinstance(domain_overrides, dict):
            for cat_id, kws in domain_overrides.items():
                if not (isinstance(kws, list) and kws):
                    continue
                cat = self._cats.get(cat_id)
                if cat is None:
                    logger.warning("[意图过滤] domain_overrides 引用了未注册类别: %s（跳过）", cat_id)
                    continue
                existing = set(cat.evidence_keywords)
                added = [k for k in kws if k not in existing]
                cat.evidence_keywords = list(existing) + added
                stats[f"domain:{cat_id}"] = len(added)

        if any(v > 0 for v in stats.values()):
            logger.info(
                "[意图过滤] config 关键词合并追加: %s",
                {k: v for k, v in stats.items() if v > 0},
            )

    # ---- 匹配原语 ------------------------------------------------------
    def category_matches(self, category_id: str, content: str, content_lower: str) -> bool:
        """某类别的证据词是否出现在消息中（用词边界防护）。"""
        cat = self._cats.get(category_id)
        if not cat or not cat.evidence_keywords:
            return False
        return any(match_keyword(kw, content, content_lower) for kw in cat.evidence_keywords)

    def match_action_categories(self, content: str) -> list[str]:
        """返回消息命中的全部行动意图类别 id（可共存，按 DEFAULT 顺序）。"""
        content_lower = content.lower()
        matched = []
        for cat in self._cats.values():
            if cat.layer != LAYER_ACTION:
                continue
            if self.category_matches(cat.id, content, content_lower):
                matched.append(cat.id)
        return matched

    # ---- 文本内容检测辅助 -------

    @staticmethod
    def _has_text_content(content: str) -> bool:
        """判断文本是否包含实质性文字内容（中文/英文/数字）。"""
        _TEXT_RE = re.compile(
            r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f'
            r'a-zA-Z0-9\u0400-\u04ff\uac00-\ud7af\u3040-\u309f'
            r'\u30a0-\u30ff]'
        )
        return bool(_TEXT_RE.search(content or ""))

    @staticmethod
    def _keyword_hit_count(cat: IntentCategory, content: str, content_lower: str) -> int:
        """返回某类别证据词命中次数（用于多子型裁决时选证据最强者）。"""
        return sum(1 for kw in cat.evidence_keywords if match_keyword(kw, content, content_lower))

    # ---- 处置层判定 ----------------------------------------------------

    def classify_disposition(
        self, content: str, enabled: bool = True,
        pure_thank_max_length: int | None = 20,
        pure_ack_max_length: int | None = 10,
        pure_closing_max_length: int | None = 20,
    ) -> DispositionResult:
        """判定消息处置意图（business / social+子型），行为与旧 _detect_intent 等价。"""
        # 边缘 Case 1: 空消息
        if not content:
            return DispositionResult(
                "business", None, "business", "空消息", confidence=0.1)

        if not enabled:
            return DispositionResult(
                "business", None, "business", "意图过滤未启用", confidence=1.0)

        content = content.strip()
        content_lower = content.lower()

        # 边缘 Case 2: 无文字内容（纯表情/附件/空白）
        if not self._has_text_content(content):
            return DispositionResult(
                "business", None, "business",
                f"无文字内容：{content[:20]}", confidence=0.1)

        business = self._cats["business"]
        business_hits = [kw for kw in business.evidence_keywords
                         if match_keyword(kw, content, content_lower)]
        has_business = bool(business_hits)

        business_hit_count = len(business_hits)

        if has_business:
            conf = 1.0 if business_hit_count >= 5 else max(0.5, business_hit_count * 0.15)
            return DispositionResult(
                "business", None, "business",
                f"业务消息：{content[:30]}...", confidence=round(conf, 2))

        # 社交子型判定
        best_sub: Optional[str] = None
        best_hit_count: int = 0
        best_cat: Optional[IntentCategory] = None

        for sub_id in _SOCIAL_PRIORITY:
            cat = self._cats[sub_id]
            hit_count = self._keyword_hit_count(cat, content, content_lower)
            if hit_count == 0:
                continue

            if sub_id in ("social.gratitude", "social.polite", "social.compliment",
                          "social.smalltalk", "social.emotion"):
                max_len = pure_thank_max_length
            elif sub_id == "social.acknowledge":
                max_len = pure_ack_max_length
            else:  # social.closing
                max_len = pure_closing_max_length

            if sub_id == "social.acknowledge" and max_len is not None and len(content) > max_len:
                logger.debug(
                    "[意图识别] 消息长度 %d > 阈值(%d)，跳过纯确认判断: %s",
                    len(content), max_len, content[:30],
                )
                return DispositionResult(
                    "business", None, "business",
                    f"业务消息：{content[:30]}...", confidence=0.3)

            if max_len is not None and len(content) > max_len:
                continue

            if hit_count > best_hit_count:
                best_hit_count = hit_count
                best_sub = sub_id
                best_cat = cat

        if best_sub and best_cat:
            label = best_cat.short_label or best_sub
            conf = 1.0 if best_hit_count >= 2 else 0.7
            return DispositionResult(
                "social", label, best_sub,
                f"{best_cat.name}（权重:{best_hit_count}）：{content}",
                confidence=round(conf, 2))

        # 边缘 Case 3: 无任何关键词命中
        return DispositionResult(
            "business", None, "business",
            f"业务消息（无关键词命中）：{content[:30]}...", confidence=0.2)

    # ---- 对外序列化 ------------------------------------------

    def self_check(self) -> dict[str, Any]:
        """启动自检：返回意图分类体系健康报告。"""
        report: dict[str, Any] = {
            "categories": {},
            "business_ratio_threshold": self._business_ratio_threshold,
        }
        for cat_id, cat in self._cats.items():
            report["categories"][cat_id] = {
                "keyword_count": len(cat.evidence_keywords),
                "max_length": cat.max_length,
            }
        mapped_tools = set(TOOL_ACTION_MAP.keys())
        report["tool_action_map_size"] = len(mapped_tools)
        return report

    def as_definitions(self, allowed_tools: set[str] | None = None) -> dict:
        """把分类体系序列化为可对外展示的结构。"""
        def _filter_tool(name: str) -> bool:
            return allowed_tools is None or name in allowed_tools

        layers: dict[str, list[dict]] = {LAYER_DISPOSITION: [], LAYER_ACTION: []}
        for cat in self._cats.values():
            layers.setdefault(cat.layer, []).append({
                "id": cat.id,
                "name": cat.name,
                "parent": cat.parent,
                "definition": cat.definition,
                "trigger": cat.trigger,
                "evidence_keywords": list(cat.evidence_keywords)[:30],
                "evidence_keyword_count": len(cat.evidence_keywords),
                "max_length": cat.max_length,
                "tools": [t for t, cs in TOOL_ACTION_MAP.items() if cat.id in cs and _filter_tool(t)],
            })
        tool_action_map = {
            t: cs for t, cs in TOOL_ACTION_MAP.items() if _filter_tool(t)
        } if allowed_tools is not None else dict(TOOL_ACTION_MAP)
        return {
            "layers": layers,
            "tool_action_map": tool_action_map,
        }


# 进程内默认注册表单例
default_registry = IntentRegistry()


def validate_tool_action_coverage(available_tools: list[str]) -> None:
    """校验工具清单两源一致性（防漂移）。"""
    mapped = set(TOOL_ACTION_MAP.keys())
    configured = set(available_tools)
    only_configured = configured - mapped
    if only_configured:
        raise ValueError(
            "config.tools.available 含无意图映射的工具（smart 路由将成盲区）: "
            + ", ".join(sorted(only_configured))
        )
    only_mapped = mapped - configured
    if only_mapped:
        logger.info(
            "[意图校验] 以下工具在 TOOL_ACTION_MAP 有映射但未在 config.tools.available 启用（合法裁剪）: %s",
            sorted(only_mapped),
        )
