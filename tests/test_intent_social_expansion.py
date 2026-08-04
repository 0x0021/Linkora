"""社交意图扩充（social 子型）回归测试。

覆盖：
- 新增 3 个社交子型（compliment / smalltalk / emotion）注册与父子关系
- 原有 4 子型扩充关键词仍正确归类
- 子型优先级裁决（可以啊→compliment，你好→polite 不被确认词抢占）
- business 优先级绝对高于社交（混合消息归 business）
- 新子型长度阈值（> pure_thank_max_length 不归社交）
- config.intent_filter 经 compliment/smalltalk/emotion 键合并追加
- RuleEngine._detect_intent 透传新 short_label
"""
from __future__ import annotations

from src.config import RulesConfig
from src.intent import IntentRegistry, LAYER_DISPOSITION
from src.rule_engine import RuleEngine


# ---------------------------------------------------------------------------
# 1. 新子型注册与结构
# ---------------------------------------------------------------------------

def test_new_social_subtypes_registered():
    reg = IntentRegistry()
    disp = {c.id for c in reg.all() if c.layer == LAYER_DISPOSITION}
    for sid in ("social.compliment", "social.smalltalk", "social.emotion"):
        assert sid in disp
        assert reg.get(sid).parent == "social"
        assert reg.get(sid).short_label is not None


def test_social_priority_order_includes_new():
    # 顺序约束：polite 早于 acknowledge；compliment 早于 acknowledge。
    from src.intent import _SOCIAL_PRIORITY
    assert "social.polite" in _SOCIAL_PRIORITY
    assert _SOCIAL_PRIORITY.index("social.polite") < _SOCIAL_PRIORITY.index("social.acknowledge")
    assert _SOCIAL_PRIORITY.index("social.compliment") < _SOCIAL_PRIORITY.index("social.acknowledge")


# ---------------------------------------------------------------------------
# 2. 新子型分类
# ---------------------------------------------------------------------------

def test_classify_compliment():
    reg = IntentRegistry()
    r = reg.classify_disposition("太棒了，厉害")
    assert r.disposition == "social"
    assert r.subtype == "compliment"
    r2 = reg.classify_disposition("666 牛啊")
    assert r2.disposition == "social" and r2.subtype == "compliment"
    r3 = reg.classify_disposition("yyds 靠谱")
    assert r3.disposition == "social" and r3.subtype == "compliment"


def test_classify_smalltalk():
    reg = IntentRegistry()
    r = reg.classify_disposition("在忙吗")
    assert r.disposition == "social" and r.subtype == "smalltalk"
    r2 = reg.classify_disposition("最近好吗，在干嘛呢")
    assert r2.disposition == "social" and r2.subtype == "smalltalk"
    r3 = reg.classify_disposition("吃饭了吗")
    assert r3.disposition == "social" and r3.subtype == "smalltalk"


def test_classify_emotion():
    reg = IntentRegistry()
    r = reg.classify_disposition("哈哈哈")
    assert r.disposition == "social" and r.subtype == "emotion"
    r2 = reg.classify_disposition("服了，无语")
    assert r2.disposition == "social" and r2.subtype == "emotion"
    r3 = reg.classify_disposition("emo 破防了")
    assert r3.disposition == "social" and r3.subtype == "emotion"


# ---------------------------------------------------------------------------
# 3. 原有 4 子型扩充关键词
# ---------------------------------------------------------------------------

def test_expanded_gratitude_keywords():
    reg = IntentRegistry()
    for msg in ("蟹蟹", "多谢啦", "thank", "thx", "3q", "感激不尽", "比心"):
        r = reg.classify_disposition(msg)
        assert r.disposition == "social" and r.subtype == "thank_you", msg


def test_expanded_acknowledge_keywords():
    reg = IntentRegistry()
    for msg in ("嗯", "哦哦", "晓得", "欧了", "k", "get", "妥了", "已读"):
        r = reg.classify_disposition(msg)
        assert r.disposition == "social" and r.subtype == "acknowledge", msg


def test_expanded_closing_keywords():
    reg = IntentRegistry()
    for msg in ("回头聊", "拜啦", "bye", "88", "溜了", "关机"):
        r = reg.classify_disposition(msg)
        assert r.disposition == "social" and r.subtype == "closing", msg


def test_expanded_polite_keywords():
    reg = IntentRegistry()
    for msg in ("哈喽", "早啊", "晚上好", "在嘛", "有空吗", "劳驾"):
        r = reg.classify_disposition(msg)
        assert r.disposition == "social" and r.subtype == "polite", msg


# ---------------------------------------------------------------------------
# 4. 优先级裁决
# ---------------------------------------------------------------------------

def test_compliment_beats_acknowledge_for_可以啊():
    reg = IntentRegistry()
    r = reg.classify_disposition("可以啊")
    assert r.disposition == "social" and r.subtype == "compliment"


def test_polite_beats_acknowledge_for_你好():
    reg = IntentRegistry()
    r = reg.classify_disposition("你好")
    assert r.disposition == "social" and r.subtype == "polite"


# ---------------------------------------------------------------------------
# 5. business 优先级绝对高于社交
# ---------------------------------------------------------------------------

def test_business_wins_over_social_mixed():
    reg = IntentRegistry()
    # 含业务词「帮我/项目」，即使含社交词也应归 business
    r = reg.classify_disposition("最近项目怎么样了帮我看下")
    assert r.disposition == "business"
    # 含「谢谢」但带明确请求
    r2 = reg.classify_disposition("谢谢，请问这个配置怎么改")
    assert r2.disposition == "business"


# ---------------------------------------------------------------------------
# 6. 新子型长度阈值
# ---------------------------------------------------------------------------

def test_new_subtype_length_threshold():
    reg = IntentRegistry()
    # 超过 pure_thank_max_length(默认20) 的纯笑声，不应归 emotion，兜底 business
    long_laugh = "哈哈" * 11  # 22 字符 > 20
    assert len(long_laugh) > 20
    r = reg.classify_disposition(long_laugh)
    assert r.disposition == "business"
    # 短笑声仍然归 emotion
    assert reg.classify_disposition("哈哈").subtype == "emotion"


# ---------------------------------------------------------------------------
# 7. config.intent_filter 合并追加新键
# ---------------------------------------------------------------------------

def test_config_merge_new_social_keys():
    reg = IntentRegistry()
    reg.apply_intent_filter({
        "compliment": ["真牛", "太顶了"],
        "smalltalk": ["在摸鱼吗"],
        "emotion": ["笑喷了"],
    })
    assert "真牛" in reg.get("social.compliment").evidence_keywords
    assert "在摸鱼吗" in reg.get("social.smalltalk").evidence_keywords
    assert "笑喷了" in reg.get("social.emotion").evidence_keywords
    # 默认种子词未被覆盖
    assert "厉害" in reg.get("social.compliment").evidence_keywords


def test_rulesconfig_has_new_social_keys():
    cfg = RulesConfig()
    assert "compliment" in cfg.intent_filter
    assert "smalltalk" in cfg.intent_filter
    assert "emotion" in cfg.intent_filter
    assert cfg.intent_filter["compliment"]  # 有种子词


# ---------------------------------------------------------------------------
# 8. RuleEngine 透传新 short_label
# ---------------------------------------------------------------------------

def test_rule_engine_detect_new_subtypes():
    engine = RuleEngine(RulesConfig(), db_store=None)
    intent, _desc, _conf = engine._detect_intent("太棒了厉害")
    assert intent == "compliment"
    intent2, _d2, _c2 = engine._detect_intent("在忙吗")
    assert intent2 == "smalltalk"
    intent3, _d3, _c3 = engine._detect_intent("哈哈哈")
    assert intent3 == "emotion"
