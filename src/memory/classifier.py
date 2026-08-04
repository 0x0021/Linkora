"""记忆范围（scope）自动分类算法。

将一条记忆内容判定为：
- ``personal``（点对点个人记忆）：绑定到具体对话人，仅在该人上下文召回；
- ``public``（公共记忆）：团队/公司级共享知识，向所有对话人召回。

判定为纯函数，便于单测与前端透明展示。算法规则见模块级常量
``ALGORITHM_SPEC``，可由 Web 端「分类规则」卡片直接渲染。
"""

from __future__ import annotations

import re

# ============ 可调参数 ============
# 公共分达到该阈值且严格高于个人分，才判定为公共；否则保守归为个人。
PUBLIC_THRESHOLD: float = 2.0
# 群聊记忆公共分加权（群讨论更可能是共享知识）。
GROUP_PUBLIC_BOOST: float = 1.0
# 单聊记忆个人分加权（私聊更可能是个人隐私）。
SINGLE_PERSONAL_BOOST: float = 0.5
# 长文本公共偏置：内容 > 200 字符时长文本更可能是制度/规范类公共知识。
LONG_TEXT_LENGTH: int = 200
LONG_TEXT_PUBLIC_BOOST: float = 0.5
# 短文本个人偏置：极短内容更可能是随口一提的个人琐事。
SHORT_TEXT_LENGTH: int = 20
SHORT_TEXT_PERSONAL_BOOST: float = 0.3
# 手动来源个人偏置：手动添加且有 sender_id 时，是对着某人说的事，偏个人。
MANUAL_WITH_SENDER_PERSONAL_BOOST: float = 0.3

# ---- 词边界正则（防子串误匹配）-----------------------------------------------
# 单字符短中文词用词边界匹配，避免 "他" 匹配 "其他"、"我" 匹配 "自我"；
# 多字符中文词和 ASCII 短词保留原子串匹配（CJK 无空格分界，误匹配概率极低）。
_SINGLE_CJK_BOUNDARY_PATTERN = r'(?<![一-龥])({})(?![一-龥])'
_ASCII_BOUNDARY_PATTERN = r'\b{}\b'


def _word_match(word: str, content: str) -> bool:
    """对短词做词边界匹配防止子串误匹配；长词保留原子串匹配。"""
    wlen = len(word)
    escaped = re.escape(word)
    # 单字符中文词：防"他"匹配"其他"、"我"匹配"自我"
    if wlen == 1 and '\u4e00' <= word <= '\u9fff':
        return bool(re.search(_SINGLE_CJK_BOUNDARY_PATTERN.format(escaped), content))
    # 短 ASCII 词（≤3 字符）：用 \b 防 "API" 匹配 "JAPI"
    if wlen <= 3 and word.isascii():
        return bool(re.search(_ASCII_BOUNDARY_PATTERN.format(escaped), content))
    # 多字中文词/长 ASCII 词：子串匹配（误匹配概率极低）
    return word in content


# ============ 公共信号词（组织/制度/产品/流程/通用知识） ============
# 每个元素: (词, 权重)。权重反映该词作为"公共知识"指示的强弱。
PUBLIC_PATTERNS: list[tuple[str, float]] = [
    # 组织 / 结构
    ("公司", 1.0), ("集团", 1.0), ("部门", 1.0), ("团队", 1.0), ("小组", 0.8),
    ("分公司", 1.0), ("子公司", 1.0), ("总部", 1.0), ("组织", 0.8), ("机构", 0.8),
    # 产品 / 业务 / 系统
    ("产品", 0.8), ("业务", 0.6), ("系统", 1.0), ("平台", 1.0), ("应用", 0.8),
    ("软件", 0.8), ("解决方案", 1.2), ("方案", 0.6), ("功能", 0.5), ("模块", 0.6),
    # 制度 / 规范
    ("制度", 1.5), ("规定", 1.5), ("规范", 1.2), ("规则", 1.2), ("政策", 1.5),
    ("流程", 1.2), ("标准", 1.0), ("办法", 1.2), ("条例", 1.2), ("章程", 1.2), ("要求", 0.8),
    # 文档 / 通知
    ("文档", 1.0), ("手册", 1.0), ("指南", 1.0), ("说明书", 1.2), ("公告", 1.5),
    ("通知", 1.2), ("通报", 1.2), ("公示", 1.5), ("周报", 1.0), ("月报", 1.0), ("汇报", 0.8),
    # 会议 / 培训
    ("会议", 0.8), ("培训", 1.0), ("课程", 1.0), ("讲座", 1.0), ("研讨会", 1.0),
    ("评审", 0.8), ("复盘", 0.8),
    # 市场 / 行业
    ("市场", 0.8), ("行业", 0.8), ("竞品", 1.2), ("对手", 0.8), ("趋势", 0.8),
    ("行情", 0.8), ("指标", 0.6), ("KPI", 1.0), ("OKR", 1.0),
    # 技术
    ("技术", 0.8), ("架构", 1.0), ("框架", 1.0), ("接口", 1.0), ("API", 1.2),
    ("数据库", 1.0), ("算法", 0.8), ("模型", 0.6), ("部署", 0.8), ("上线", 0.6),
    ("版本", 0.6), ("协议", 0.8),
    # 通用知识短语
    ("一般而言", 1.5), ("一般来说", 1.5), ("众所周知", 1.5), ("公司规定", 2.0),
    ("公司要求", 2.0), ("我们公司", 1.5), ("公司里", 1.0), ("公开", 1.0), ("公示", 1.5),
    ("通用", 1.0), ("标准做法", 1.5), ("最佳实践", 1.5), ("行业惯例", 1.5), ("统一", 0.6),
]

# ============ 个人信号词（人称/属性/关系/绑定短语） ============
PERSONAL_PATTERNS: list[tuple[str, float]] = [
    # 人称代称
    ("我", 0.5), ("俺", 0.5), ("本人", 1.0), ("自己", 0.8), ("咱", 0.5),
    ("用户", 0.8), ("该用户", 1.2), ("这位用户", 1.2), ("他", 0.5), ("她", 0.5),
    ("对方", 1.0), ("您", 0.5),
    # 个人属性
    ("生日", 2.0), ("年龄", 1.5), ("岁", 0.6), ("喜好", 1.5), ("偏好", 1.5),
    ("习惯", 1.2), ("兴趣", 1.2), ("爱好", 1.2), ("住址", 2.0), ("地址", 1.0),
    ("家庭住址", 2.0), ("电话", 1.5), ("手机", 1.2), ("微信", 1.5), ("邮箱", 1.2),
    ("家人", 2.0), ("配偶", 2.0), ("妻子", 2.0), ("丈夫", 2.0), ("子女", 2.0),
    ("儿子", 2.0), ("女儿", 2.0), ("父母", 2.0), ("父亲", 2.0), ("母亲", 2.0),
    ("工资", 2.0), ("薪资", 2.0), ("薪水", 2.0), ("收入", 1.5), ("奖金", 1.5),
    ("请假", 1.5), ("病假", 1.8), ("年假", 1.8), ("私人", 2.0), ("隐私", 2.0), ("密码", 2.0),
    # 关系 / 称呼
    ("同事", 1.2), ("朋友", 1.2), ("领导", 1.2), ("老板", 1.2), ("上级", 1.2),
    ("上司", 1.2), ("下属", 1.2), ("徒弟", 1.2), ("亲属", 1.5), ("亲戚", 1.5),
    # 绑定短语（强烈指示个人归属）
    ("我的", 1.5), ("他的", 1.5), ("她的", 1.5), ("你的", 1.0), ("您的", 1.0),
    ("对方的", 1.5), ("用户说", 1.5), ("用户提到", 1.5), ("该用户", 1.2),
    ("他喜欢", 1.5), ("她负责", 1.5), ("对方要求", 1.5), ("本人表示", 1.5), ("本人认为", 1.5),
]

# ============ 算法规则说明（供前端展示） ============
ALGORITHM_SPEC: list[dict] = [
    {
        "order": 0,
        "name": "显式指定优先",
        "rule": "若调用方显式传入 scope（个人 / 公共），直接采用，不再自动判断。",
        "example": "Web 端「新增记忆」手动选择范围；工具显式传入 scope。",
    },
    {
        "order": 1,
        "name": "手动无归属 → 公共",
        "rule": "来源为手动添加(manual)且未绑定任何具体对话人(sender_id 为空)时，视为公共知识。",
        "example": "「公司年假为 10 天」（无指定人）→ 公共。",
    },
    {
        "order": 2,
        "name": "信号词打分",
        "rule": (
            "对记忆文本匹配「公共信号词」与「个人信号词」两套词典，分别累加权重得分。"
            "公共信号示例：公司 / 部门 / 制度 / 系统 / 产品 / 流程 / 公告 / 培训 / 接口 / 政策…；"
            "个人信号示例：我 / 生日 / 工资 / 家人 / 隐私 / 我的 / 他的 / 对方要求…"
        ),
        "example": "「公司规定报销流程」公共分高；「用户提到他下周三请假」个人分高。",
    },
    {
        "order": 3,
        "name": "会话类型加权",
        "rule": "群聊(chat_type=group)记忆公共分 +1.0；单聊(single)个人分 +0.5，作为平局修正。",
        "example": "同一句「下周开会」，群聊更可能判为公共、单聊更可能判为个人。",
    },
    {
        "order": 4,
        "name": "裁决（保守默认个人）",
        "rule": "公共分 ≥ 2.0 且严格大于个人分 → 公共；否则 → 个人。保守默认避免个人隐私误入公共库。",
        "example": "信号均不明确时落入个人，保持原有 1对1 行为不变。",
    },
]


def _score(content: str) -> tuple[float, float, list[dict]]:
    """对文本打分，返回 (public_score, personal_score, signals)。
    使用词边界匹配防止短词子串误匹配（如"API"不会匹配"纸API机"）。
    """
    public_score = 0.0
    personal_score = 0.0
    signals: list[dict] = []
    for word, weight in PUBLIC_PATTERNS:
        if _word_match(word, content):
            public_score += weight
            signals.append({"category": "public", "word": word, "weight": weight})
    for word, weight in PERSONAL_PATTERNS:
        if _word_match(word, content):
            personal_score += weight
            signals.append({"category": "personal", "word": word, "weight": weight})
    return public_score, personal_score, signals


def _confidence(public_score: float, personal_score: float) -> float:
    """根据分差计算置信度 0.0~1.0。"""
    diff = public_score - personal_score
    if abs(diff) >= 4.0:
        return 1.0
    if abs(diff) >= 2.0:
        return 0.8
    if abs(diff) >= 1.0:
        return 0.65
    return 0.5  # 边界 case，置信度低


def classify_memory_scope(
    content: str,
    sender_id: str = "",
    sender_name: str = "",
    chat_type: str = "",
    source: str = "",
    explicit_scope: str | None = None,
) -> tuple[str, str, float]:
    """判定单条记忆的范围（scope）。

    Returns:
        (scope, reason, confidence)。
        scope ∈ {"personal", "public"}；
        reason 为可读的判定依据；
        confidence 为 0.0~1.0 的置信度。
    """
    content = (content or "").strip()

    # 规则 0：显式指定优先
    if explicit_scope in ("public", "personal"):
        return explicit_scope, f"显式指定为{explicit_scope}", 1.0

    # 规则 1：手动无归属 → 公共
    src = (source or "").lower()
    if src.startswith("manual") and not sender_id:
        return "public", "手动添加且未绑定具体对话人，视为公共知识", 1.0

    # 规则 2：信号打分
    public_score, personal_score, signals = _score(content)

    # 规则 2b: 内容长度偏置
    length_notes = []
    if len(content) > LONG_TEXT_LENGTH:
        public_score += LONG_TEXT_PUBLIC_BOOST
        length_notes.append(f"长文本偏置+{LONG_TEXT_PUBLIC_BOOST}")
    elif len(content) < SHORT_TEXT_LENGTH:
        personal_score += SHORT_TEXT_PERSONAL_BOOST
        length_notes.append(f"短文本偏置+{SHORT_TEXT_PERSONAL_BOOST}")

    # 规则 2c: 来源信号加权
    source_notes = []
    if src.startswith("manual") and sender_id:
        personal_score += MANUAL_WITH_SENDER_PERSONAL_BOOST
        source_notes.append(f"手动指定人偏置+{MANUAL_WITH_SENDER_PERSONAL_BOOST}")

    # 规则 3：会话类型加权
    chat_boost_note = ""
    if chat_type == "group":
        public_score += GROUP_PUBLIC_BOOST
        chat_boost_note = f"；群聊加权+{GROUP_PUBLIC_BOOST}"
    elif chat_type == "single":
        personal_score += SINGLE_PERSONAL_BOOST
        chat_boost_note = f"；单聊加权+{SINGLE_PERSONAL_BOOST}"

    # 合并偏置说明
    extra_notes = "；".join(length_notes + source_notes)
    if extra_notes:
        chat_boost_note = f"{chat_boost_note}；{extra_notes}"

    conf = _confidence(public_score, personal_score)

    # 规则 4：裁决
    if public_score >= PUBLIC_THRESHOLD and public_score > personal_score:
        return "public", (
            f"公共分{public_score:.1f} ≥ 阈值{PUBLIC_THRESHOLD} 且高于个人分{personal_score:.1f}{chat_boost_note}"
        ), round(conf, 2)
    return "personal", (
        f"保守归为个人（公共分{public_score:.1f} / 个人分{personal_score:.1f}{chat_boost_note}）"
    ), round(conf, 2)


def explain_classification(
    content: str,
    sender_id: str = "",
    sender_name: str = "",
    chat_type: str = "",
    source: str = "",
    explicit_scope: str | None = None,
) -> dict:
    """返回分类的完整解释，便于调试与前端透明展示。"""
    scope, reason, confidence = classify_memory_scope(
        content, sender_id=sender_id, sender_name=sender_name,
        chat_type=chat_type, source=source, explicit_scope=explicit_scope,
    )
    _, _, signals = _score(content)
    return {
        "scope": scope,
        "reason": reason,
        "confidence": confidence,
        "signals": signals,
        "algorithm": ALGORITHM_SPEC,
    }
