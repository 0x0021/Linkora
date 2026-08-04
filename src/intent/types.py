"""意图分类体系的数据类型定义。

包含 IntentCategory（意图类别）和 DispositionResult（处置结果）两个核心数据类，
以及层常量定义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# 层常量
# ---------------------------------------------------------------------------

# 处置层：消息是否值得助手处理
LAYER_DISPOSITION = "disposition"
# 行动层：业务消息想要的抽象动作类别
LAYER_ACTION = "action"
# 域层：工具/技能路由用的"具体场景"意图类别（单一真源，关键词只在此维护一次）
# 工具/技能不再各自持有原始关键词列表，改为声明服务哪些 domain.* 类别。
LAYER_DOMAIN = "domain"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class IntentCategory:
    """一个抽象意图类别。

    Attributes:
        id: 机器标识，如 "business" / "social.acknowledge" / "action.query"。
        name: 中文名（展示/文档）。
        layer: LAYER_DISPOSITION | LAYER_ACTION。
        parent: 父类别 id（层次结构用，处置层 social 子型指向 "social"）。
        definition: 语义边界——这个意图"包含什么、不包含什么"。
        trigger: 典型触发条件——什么情况下归到这个意图。
        evidence_keywords: 证据词库——命中即视为该意图出现（具体词集中于此，便于扩展）。
        max_length: 可选。消息长度上限，超过则不再归为此类（用于社交短信号）。
        short_label: 可选。对外/日志用的简短标签（social 子型沿用旧名 thank_you 等）。
    """

    id: str
    name: str
    layer: str
    definition: str
    trigger: str
    parent: Optional[str] = None
    evidence_keywords: list[str] = field(default_factory=list)
    max_length: Optional[int] = None
    short_label: Optional[str] = None


@dataclass
class DispositionResult:
    """处置层判定结果。"""
    disposition: str          # "business" | "social"
    subtype: Optional[str]    # social 子型 short_label（thank_you/acknowledge/...），business 时为 None
    category_id: Optional[str]  # 命中的类别 id（如 "social.acknowledge" / "business"）
    reason: str
    confidence: float = 1.0   # 置信度 0.0~1.0（1.0=高置信匹配，<0.5=低置信/边缘case）
