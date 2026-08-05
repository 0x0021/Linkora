"""意图分类体系模块。

抽象意图分类体系（Intent Taxonomy），提供声明式、可扩展的意图注册表。
"""
from __future__ import annotations

# 重新导出公共 API
from src.intent.types import (
    IntentCategory,
    DispositionResult,
    LAYER_DISPOSITION,
    LAYER_ACTION,
    LAYER_DOMAIN,
)
from src.intent.matching import match_keyword, _SOCIAL_PRIORITY
from src.intent.categories_disposition import DEFAULT_INTENTS as _DEFAULT_DISPOSITION
from src.intent.categories_action import ACTION_INTENTS
from src.intent.categories_domain import DOMAIN_INTENTS
from src.intent.registry import IntentRegistry, default_registry, validate_tool_action_coverage, TOOL_ACTION_MAP

# 合并所有意图类别
DEFAULT_INTENTS: list[IntentCategory] = _DEFAULT_DISPOSITION + ACTION_INTENTS + DOMAIN_INTENTS

__all__ = [
    # 类型
    'IntentCategory',
    'DispositionResult',
    # 层常量
    'LAYER_DISPOSITION',
    'LAYER_ACTION',
    'LAYER_DOMAIN',
    # 匹配函数
    'match_keyword',
    # 优先级
    '_SOCIAL_PRIORITY',
    # 数据
    'DEFAULT_INTENTS',
    'TOOL_ACTION_MAP',
    'DOMAIN_INTENTS',
    # 注册表
    'IntentRegistry',
    'default_registry',
    'validate_tool_action_coverage',
]
