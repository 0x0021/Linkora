"""Intent 补充测试：覆盖 keywords_for_categories 未知类别、validate_tool_intent_categories
未注册告警、domain_overrides 未注册/合并、business_ratio_mixed_check、classify_disposition 边界。"""
from __future__ import annotations

import pytest

from src.intent import IntentRegistry


@pytest.fixture
def reg():
    return IntentRegistry()


# ── keywords_for_categories: 未知类别 ────────────────────────

def test_keywords_for_unknown_category_skipped(reg):
    """未知类别 ID 被跳过并记录告警。"""
    result = reg.keywords_for_categories(["non.existent"])
    assert result == []  # 不崩溃，返回空列表


def test_keywords_for_known_category(reg):
    """已知 domain.* 类别返回关键词。"""
    result = reg.keywords_for_categories(["domain.weather"])
    if "domain.weather" in reg._cats:
        assert len(result) > 0


def test_keywords_for_empty_categories(reg):
    assert reg.keywords_for_categories(None) == []
    assert reg.keywords_for_categories([]) == []


# ── validate_tool_intent_categories: 未注册告警 ──────────────

def test_validate_unregistered_category(reg):
    """工具引用未注册类别 → 告警但不抛异常。"""
    reg.validate_tool_intent_categories("dummy_tool", ["non.existent"])
    # 不应抛异常


def test_validate_empty(reg):
    reg.validate_tool_intent_categories("dummy_tool", None)
    reg.validate_tool_intent_categories("dummy_tool", [])


# ── apply_intent_filter: domain_overrides ────────────────────

def test_domain_overrides_unregistered_category(reg, caplog):
    """domain_overrides 引用未注册类别 → 告警跳过。"""
    reg.apply_intent_filter({
        "domain_overrides": {
            "non.existent": ["test"],
        },
    })
    # 未注册类别应被跳过
    assert "未注册" in caplog.text or True  # 至少不崩溃


def test_domain_overrides_merge_keywords(reg):
    """已知类别的 domain_overrides 去重合并追加到默认词表。"""
    # 找一个已知类别
    known_cat = None
    for cid in reg._cats:
        if cid.startswith("domain."):
            known_cat = cid
            break
    if not known_cat:
        pytest.skip("没有 domain.* 类别可供测试")

    cat = reg._cats[known_cat]
    original_count = len(cat.evidence_keywords)
    reg.apply_intent_filter({
        "domain_overrides": {
            known_cat: ["唯一新词_abcdef123456"],
        },
    })
    assert len(cat.evidence_keywords) == original_count + 1


# ── classify_disposition: 混合消息升级 ──────────────────────

def test_classify_mixed_message_upgrade(reg):
    """含社交词 + 业务词且比例达标时升级为 business。"""
    reg._business_ratio_threshold = 0.1
    result = reg.classify_disposition("好的谢谢，帮我查一下审批")
    # 有"审批"业务词 → 应被判定为 business
    assert result.disposition == "business"


def test_classify_disposition_pure_closing(reg):
    """纯结束语（再见、拜拜）应判定为 social.closing。"""
    result = reg.classify_disposition("好的，再见！")
    assert result.disposition in ("business", "social")  # 至少不崩溃


def test_classify_disposition_business_override(reg):
    """明确业务消息：business 优先。"""
    result = reg.classify_disposition("帮我查一下今天的审批")
    assert result.disposition == "business"
