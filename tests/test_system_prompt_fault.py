"""system_prompt 指令回归测试：故障口径 + 长回复排版规范。

故障口径（2026-08-31 事故）：
对方报「VDI 更新后黑屏」，AI 回「收到，问题已解决就好。」
根因在数据层（历史消息重放，已在 poller/message_loop 修复）；
本条测试确保行为层兜底指令确实进了 prompt。

长回复排版规范：
钉钉/企微等 IM 平台 Markdown 渲染能力有限，模型输出若不约束格式，
会出现编号挤同行、加粗随意、无段落间距、代码块缺标注等问题。
本条测试确保排版指令在 core 末尾和完整 prompt 末尾均正确注入。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.system_prompt import (  # noqa: E402
    _FAULT_HANDLING_DIRECTIVE,
    _FAULT_HANDLING_REMINDER,
    _REPLY_FORMAT_DIRECTIVE,
    _REPLY_FORMAT_REMINDER,
    build_system_prompt,
    build_system_prompt_core,
)


class _Agent:
    user_name = "宇坤"
    platform_id = "dingtalk"
    user_dept = "总裁办"
    user_title = "助理"
    org_name = "公司"
    few_shot_examples = []
    skill_manager = None
    skills_config = SimpleNamespace(enabled=False)
    tool_router = None
    store = None
    config = SimpleNamespace(
        dynamic_few_shot=False,
        system_prompt="你是{user_name}的{platform}数字分身。",
        advanced=SimpleNamespace(
            max_chars_daily_chat=200,
            max_chars_tech_issue=100,
        ),
        tools=SimpleNamespace(block_outbound_to_third_party=True),
    )

    def _get_style_prompt(self):
        return ""


class TestFaultHandlingDirective:
    def test_directive_in_core(self):
        prompt = build_system_prompt_core(_Agent(), "郭建辉")
        assert _FAULT_HANDLING_DIRECTIVE in prompt

    def test_reminder_in_full_prompt(self):
        prompt = build_system_prompt(_Agent(), "郭建辉", include_few_shot=False)
        assert _FAULT_HANDLING_REMINDER in prompt

    def test_no_premature_resolution_phrasing_absent_as_blanket_ban(self):
        """指令是正向口径（围绕现象给方向），不是纯否定式「不要说已解决」。

        校验关键正向表述存在，避免有人把指令改成纯禁止式而失去指导意义。
        """
        assert "围绕「现在的现象」作答" in _FAULT_HANDLING_DIRECTIVE
        assert "不要把过去某次的结论当成这一次的结论" in _FAULT_HANDLING_DIRECTIVE

    def test_directive_locks_current_topic_not_past(self):
        """末句明确区分「本轮夹带的旧记录」与「当前结论」，正对事故失效模式。"""
        assert "更早时间的相关记录" in _FAULT_HANDLING_DIRECTIVE
        assert "这一次的结论" in _FAULT_HANDLING_DIRECTIVE


class TestReplyFormatDirective:
    """长回复排版规范指令回归测试。"""

    def test_format_directive_in_core(self):
        prompt = build_system_prompt_core(_Agent(), "张三")
        assert _REPLY_FORMAT_DIRECTIVE in prompt

    def test_format_reminder_in_full_prompt(self):
        prompt = build_system_prompt(_Agent(), "张三", include_few_shot=False)
        assert _REPLY_FORMAT_REMINDER in prompt

    def test_covers_all_six_rules(self):
        """6 条规则全部覆盖：标题层级、列表换行、段落间距、列表分块、代码标记、视觉节奏。"""
        d = _REPLY_FORMAT_DIRECTIVE
        assert "##" in d  # 标题层级
        assert "1. " in d or "逐项独占一行" in d  # 列表换行
        assert "空一行" in d  # 段落间距
        assert "列表分块" in d or "列表" in d  # 长内容用列表
        assert "代码块" in d and "反引号" in d  # 代码与标记
        assert "视觉节奏" in d or "一致" in d  # 视觉节奏统一

    def test_excludes_tables_for_dingtalk(self):
        """明确提到钉钉不支持表格，避免模型输出表格。"""
        assert "不支持表格" in _REPLY_FORMAT_DIRECTIVE or "勿用表格" in _REPLY_FORMAT_DIRECTIVE

    def test_short_reply_exempted(self):
        """短回复不受此约束，保持自然口语。"""
        assert "短回复" in _REPLY_FORMAT_REMINDER or "短回复" in _REPLY_FORMAT_DIRECTIVE
