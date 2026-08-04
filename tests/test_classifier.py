"""记忆范围自动分类算法单测。

覆盖：显式指定优先、手动无归属→公共、公共信号、个人信号、
群聊加权、保守默认个人、置信度输出，以及 explain 返回结构。
"""

from __future__ import annotations


from src.memory.classifier import (
    ALGORITHM_SPEC,
    classify_memory_scope,
    explain_classification,
)


def test_explicit_scope_overrides():
    # 规则 0：显式指定优先，不做判断
    scope, reason, conf = classify_memory_scope("今天天气不错", explicit_scope="public")
    assert scope == "public"
    assert "显式指定" in reason
    assert conf == 1.0
    scope, reason, conf = classify_memory_scope("我的工资", explicit_scope="personal")
    assert scope == "personal"
    assert "显式指定" in reason
    assert conf == 1.0


def test_manual_no_sender_is_public():
    # 规则 1：手动添加且无绑定人 → 公共
    scope, reason, conf = classify_memory_scope("公司年假为 10 天", source="manual")
    assert scope == "public"
    assert "未绑定" in reason
    assert conf == 1.0


def test_public_signals_win():
    # 公司规定报销流程 → 公共分远高于个人分
    scope, reason, conf = classify_memory_scope("公司规定报销流程需要先审批", chat_type="group")
    assert scope == "public"
    assert conf > 0.6


def test_personal_signals_win():
    # 用户提到他下周三请假 → 个人
    scope, reason, conf = classify_memory_scope("用户提到他下周三请假，需安排代班", sender_id="u1")
    assert scope == "personal"
    assert conf > 0.6


def test_group_boost_tips_to_public():
    # 群聊加权使边界内容判为公共、单聊保持个人，验证平局修正生效
    content = "部门下周的安排"
    # 单聊：公共分=部门(1.0) + 个人加权 0.5 = 1.5 < 阈值 2.0 → 个人
    assert classify_memory_scope(content, chat_type="single")[0] == "personal"
    # 群聊：公共分=部门(1.0) + 群加权 1.0 = 2.0 ≥ 阈值 → 公共
    assert classify_memory_scope(content, chat_type="group")[0] == "public"


def test_single_chat_stays_personal_when_ambiguous():
    # 单聊 + 无明确信号 → 保守默认个人，保持 1对1 行为
    scope, reason, conf = classify_memory_scope("好的，收到", chat_type="single")
    assert scope == "personal"


def test_privacy_never_leaks_to_public():
    # 明确的个人隐私内容，即便在群聊也应保持个人
    scope, reason, conf = classify_memory_scope("我的工资是 2 万，别告诉别人", chat_type="group")
    assert scope == "personal"


def test_confidence_boundary_case():
    # 边界 case：公共分与个人分接近时置信度应 < 0.7
    # "部门" 命中 public("部门"=1.0)，"他" 命中 personal("他"=0.5)，
    # 长度 < 20 触发短文本偏置 +0.3 个人，单聊 +0.5 个人，
    # 最终 public=1.0, personal=1.3 → personal，diff=-0.3 < 1.0 → conf=0.5
    scope, reason, conf = classify_memory_scope("部门让他去", chat_type="single")
    assert scope == "personal"
    assert conf == 0.5, f"Expected low confidence for boundary case, got {conf}"


def test_word_boundary_no_false_match():
    # 词边界防误匹配："其他" 不应命中 "他"（短中文词边界检查）
    scope, reason, conf = classify_memory_scope("其他部门", chat_type="group")
    # "部门"=1.0 + 群聊=1.0 = 2.0 ≥ 阈值 → public，"他" 应不被匹配
    assert scope == "public"
    # 不应在 signals 中包含 "他"
    info = explain_classification("其他部门", chat_type="group")
    personal_words = [s["word"] for s in info["signals"] if s["category"] == "personal"]
    assert "他" not in personal_words, f"词边界防误匹配失败：'其他'误命中了'他', signals={personal_words}"


def test_explain_structure():
    info = explain_classification("公司规定报销流程", chat_type="group")
    assert info["scope"] == "public"
    assert "公共" in info["reason"] or "public" in info["reason"]
    assert isinstance(info["signals"], list) and len(info["signals"]) > 0
    assert all(s["category"] in ("public", "personal") for s in info["signals"])
    assert info["algorithm"] == ALGORITHM_SPEC
    assert "confidence" in info
    assert len(ALGORITHM_SPEC) >= 5
