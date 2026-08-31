"""system_prompt 故障口径指令回归测试。

2026-08-31 事故：对方报「VDI 更新后黑屏」，AI 回「收到，问题已解决就好。」
根因在数据层（历史消息重放，已在 poller/message_loop 修复）；本条测试
确保行为层兜底指令确实进了 prompt：
- _FAULT_HANDLING_DIRECTIVE 进 core 末尾（近因位）；
- _FAULT_HANDLING_REMINDER 进完整 prompt 末尾（被 few-shot 原话往回拽后再压）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.system_prompt import (  # noqa: E402
    _FAULT_HANDLING_DIRECTIVE,
    _FAULT_HANDLING_REMINDER,
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
