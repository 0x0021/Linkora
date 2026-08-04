"""Phase 0 意图分类改进测试。

覆盖：
1. apply_intent_filter 合并追加（非整段替换）
2. business_ratio_threshold 接入（混合消息升级）
3. self_check 启动自检
4. business_ratio_mixed_check 边界场景
"""

import pytest

from src.intent import (
    IntentRegistry,
    DEFAULT_INTENTS,
    TOOL_ACTION_MAP,
    match_keyword,
)


# ── 合并追加（原为整段替换） ────────────────────────────────

class TestMergeAppend:
    def test_merge_appends_new_keywords(self):
        reg = IntentRegistry()
        before = len(reg.get("business").evidence_keywords)
        reg.apply_intent_filter({"business_keywords": ["全新词A", "全新词B"]})
        after = len(reg.get("business").evidence_keywords)
        assert after == before + 2  # 追加而非替换
        kw_set = set(reg.get("business").evidence_keywords)
        assert "全新词A" in kw_set
        # 原有关键词仍在
        assert "问题" in kw_set

    def test_merge_dedupes(self):
        reg = IntentRegistry()
        before = len(reg.get("business").evidence_keywords)
        reg.apply_intent_filter({"business_keywords": ["问题", "新词"]})  # "问题" 已存在
        after = len(reg.get("business").evidence_keywords)
        assert after == before + 1  # 只追加 "新词"

    def test_merge_preserves_all_default_keywords(self):
        reg = IntentRegistry()
        default_kws = set(reg.get("social.gratitude").evidence_keywords)
        reg.apply_intent_filter({"thank_you": ["追加感谢词"]})
        merged = set(reg.get("social.gratitude").evidence_keywords)
        # 所有原关键词都在
        assert default_kws.issubset(merged)
        # 追加的也在
        assert "追加感谢词" in merged
        # 总数 = 原始 + 新增（去重后）
        assert len(merged) == len(default_kws) + 1

    def test_empty_filter_noop(self):
        reg = IntentRegistry()
        biz_before = list(reg.get("business").evidence_keywords)
        reg.apply_intent_filter({})
        assert reg.get("business").evidence_keywords == biz_before
        reg.apply_intent_filter(None)  # type: ignore[arg-type]
        assert reg.get("business").evidence_keywords == biz_before

    def test_social_categories_merge(self):
        reg = IntentRegistry()
        ack_before = len(reg.get("social.acknowledge").evidence_keywords)
        polite_before = len(reg.get("social.polite").evidence_keywords)
        reg.apply_intent_filter({
            "acknowledge": ["收到哒"],
            "polite": ["在不在哟"],
        })
        assert len(reg.get("social.acknowledge").evidence_keywords) == ack_before + 1
        assert len(reg.get("social.polite").evidence_keywords) == polite_before + 1


# ── business_ratio_threshold 接入 ────────────────────────────

class TestBusinessRatioThreshold:
    def test_threshold_stored_from_filter(self):
        reg = IntentRegistry()
        assert reg._business_ratio_threshold == 0.3  # 默认值
        reg.apply_intent_filter({"business_ratio_threshold": 0.5})
        assert reg._business_ratio_threshold == 0.5

    def test_invalid_threshold_ignored(self):
        reg = IntentRegistry()
        reg.apply_intent_filter({"business_ratio_threshold": "not_a_number"})
        assert reg._business_ratio_threshold == 0.3  # 保持默认

# ── classify_disposition 混合消息集成 ───────────────────────


class TestMixedCheckRemoved:
    """回归：死代码 _business_ratio_mixed_check 已移除，business 判定由 has_business 短路覆盖。"""

    def test_method_no_longer_exists(self):
        reg = IntentRegistry()
        assert not hasattr(reg, "_business_ratio_mixed_check"), \
            "死代码 _business_ratio_mixed_check 应已移除"

    def test_business_keyword_still_wins(self):
        reg = IntentRegistry()
        result = reg.classify_disposition("好的谢谢，帮我查一下审批流程")
        assert result.disposition == "business"

class TestDispositionMixedIntegration:
    def test_social_with_dense_business_upgrades(self):
        reg = IntentRegistry()
        reg._business_ratio_threshold = 0.1  # 低阈值让测试更容易触发
        # "好的" 是 acknowledge 词，但后面有大量业务词
        result = reg.classify_disposition(
            "好的，帮我查一下审批流程和考勤记录以及日历日程安排",
            pure_ack_max_length=10,
        )
        # acknowledge 先命中（"好的"在 acknowledge 列表），但混合检查应升级为 business
        assert result.disposition == "business"

    def test_short_social_not_upgraded(self):
        reg = IntentRegistry()
        result = reg.classify_disposition(
            "好的",
            pure_ack_max_length=10,
        )
        assert result.disposition == "social"

    def test_business_still_takes_priority(self):
        reg = IntentRegistry()
        result = reg.classify_disposition("帮我查一下错误日志")
        assert result.disposition == "business"


# ── 自检 self_check ──────────────────────────────────────────

class TestSelfCheck:
    def test_returns_all_categories(self):
        reg = IntentRegistry()
        report = reg.self_check()
        for cat in DEFAULT_INTENTS:
            assert cat.id in report["categories"]
            assert report["categories"][cat.id]["keyword_count"] == len(cat.evidence_keywords)

    def test_includes_threshold(self):
        reg = IntentRegistry()
        report = reg.self_check()
        assert report["business_ratio_threshold"] == 0.3
        reg._business_ratio_threshold = 0.7
        report = reg.self_check()
        assert report["business_ratio_threshold"] == 0.7

    def test_includes_tool_map_size(self):
        report = IntentRegistry().self_check()
        assert report["tool_action_map_size"] > 0


# ── match_keyword 边界 ──────────────────────────────────────

class TestMatchKeywordBoundary:
    def test_short_ascii_word_boundary(self):
        # "OK" 不应匹配 "ACME" 或 "COOKBOOK"
        assert not match_keyword("OK", "ACME 系统报错", "acme 系统报错")
        assert match_keyword("OK", "OK 收到", "ok 收到")
        assert not match_keyword("ok", "book", "book")

    def test_long_ascii_no_boundary(self):
        assert match_keyword("search", "I want to search something", "i want to search something")

    def test_chinese_substring(self):
        assert match_keyword("搜索", "请帮我搜索一下", "请帮我搜索一下")


# ── validate_tool_action_coverage ────────────────────────────

class TestToolCoverageValidation:
    def test_valid_subset_passes(self):
        from src.intent import validate_tool_action_coverage
        # 取 TOOL_ACTION_MAP 的子集应通过
        subset = list(TOOL_ACTION_MAP.keys())[:5]
        validate_tool_action_coverage(subset)  # 不抛异常即通过

    def test_unknown_tool_raises(self):
        from src.intent import validate_tool_action_coverage
        with pytest.raises(ValueError, match="无意图映射"):
            validate_tool_action_coverage(["nonexistent_tool_xyz"])
