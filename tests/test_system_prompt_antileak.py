"""system_prompt 层防泄漏根因测试。

覆盖 2026-07-27 系统性排查后的根因修复：
- _normalize_style_prompt：剥离风格画像开头的元叙述框架（「按照主人的风格，」等），
  从源头消除模型原样回声（截图证实的泄漏模式）。
- _ANTI_ECHO_DIRECTIVE：注入在 all 注入段之前，约束模型不把指令/推理/内部标记写进回复。
- 删除了规则行中明文列举禁止句式的负面 priming（反而诱导泄漏且费 token）。
"""

import unittest

from src.llm.system_prompt import (
    _ANTI_ECHO_DIRECTIVE,
    _normalize_style_prompt,
    build_system_prompt_core,
)


class _FakeConfig:
    def __init__(self):
        self.system_prompt = "你是{user_name}的数字分身。"
        self.advanced = type("A", (), {
            "max_chars_daily_chat": 100,
            "max_chars_tech_issue": 300,
        })()


class _FakeAgent:
    def __init__(self, user_name="OWNER", title="", platform="dingtalk"):
        self.user_name = user_name
        self.user_dept = "技术部"
        self.user_title = title
        self.org_name = "示例公司"
        self.platform_id = platform
        self.config = _FakeConfig()


class TestNormalizeStylePrompt(unittest.TestCase):
    def test_strip_zh_pressure_prefix(self):
        """「按照主人的风格，应该直接务实…」→ 剥离框架，保留正文。"""
        raw = "按照主人的风格，应该直接务实，聚焦问题主体与具体现象。"
        out = _normalize_style_prompt(raw)
        self.assertFalse(out.startswith("按照"))
        self.assertTrue(out.startswith("应该直接务实"))

    def test_strip_your_style_is(self):
        """「你的风格是：直接务实」→ 剥离「你的风格是：」。"""
        raw = "你的风格是：直接务实，技术性强。"
        out = _normalize_style_prompt(raw)
        self.assertFalse(out.startswith("你的风格"))
        self.assertTrue(out.startswith("直接务实"))

    def test_strip_as_digital_twin(self):
        """「作为数字分身，直接务实」→ 剥离「作为数字分身，」。"""
        raw = "作为数字分身，直接务实，不废话。"
        out = _normalize_style_prompt(raw)
        self.assertFalse(out.startswith("作为数字分身"))
        self.assertTrue(out.startswith("直接务实"))

    def test_clean_style_unchanged(self):
        """已是正文的风格描述（无元框架）原样保留。"""
        raw = "直接务实，聚焦问题主体，用词技术性与指令性。"
        self.assertEqual(_normalize_style_prompt(raw), raw)

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_style_prompt(""), "")
        self.assertEqual(_normalize_style_prompt(None), "")

    def test_only_meta_becomes_empty(self):
        """若风格画像只剩元框架（无正文），返回空串（调用方据此跳过注入）。"""
        self.assertEqual(_normalize_style_prompt("按照主人的风格，"), "")


class TestAntiEchoDirective(unittest.TestCase):
    def test_directive_present_in_core(self):
        """反泄漏指令必须出现在基础 system prompt 中。

        v2 契约（2026-07-27）：指令移到 prompt **末尾**（近因效应最大化），
        不再要求位于身份段之前——旧断言基于 v1 首位效应设计，已过期。
        """
        prompt = build_system_prompt_core(_FakeAgent())
        self.assertIn(_ANTI_ECHO_DIRECTIVE, prompt)
        # v2：指令应在身份段【之后】（prompt 末尾近因位置）
        self.assertGreater(
            prompt.index(_ANTI_ECHO_DIRECTIVE),
            prompt.index("身份:"),
        )

    def test_no_negative_priming(self):
        """规则行不再明文列举『严禁在回复中出现「我需要/我应该/根据提供的【相关知识】/作为主人的」』。
        旧实现这条负面 priming 逐字列举禁止句式，反而诱导弱模型产出这些句式。
        注意：反泄漏指令本身会以「示例」形式提及这些句式（不写「我需要/我应该…」），
        故此处只断言 OLD 的「严禁在回复中出现…」整句已删除，而非禁止任何字样的出现。
        """
        prompt = build_system_prompt_core(_FakeAgent())
        self.assertNotIn("严禁在回复中出现", prompt)
        self.assertNotIn("根据提供的【相关知识】/作为主人的", prompt)
        # 但「禁止内心独白」这一正常规则仍保留
        self.assertIn("禁止内心独白", prompt)

    def test_directive_covers_key_constraints(self):
        """反泄漏指令覆盖核心约束（v2 契约：单一正面约束，不列举禁止句式）。

        v2 设计：负面 priming（逐字列举禁止句式）反而诱导弱模型产出这些句式，
        故指令精简为「回复仅含直接回答 + 不展示思考/不输出系统指令等类别性约束」。
        """
        d = _ANTI_ECHO_DIRECTIVE
        self.assertIn("回复仅含对对话者的直接回答", d)
        self.assertIn("思考", d)
        self.assertIn("系统指令", d)
        self.assertIn("身份设定", d)
        self.assertIn("内部标记", d)
        self.assertIn("引文元信息", d)

    def test_directive_forbids_meta_narration(self):
        """★ 元叙述泄漏防御（v2：从指令点名下沉为 sanitize 硬正则，端到端验证）。

        v1 在指令里逐字点名「用户已多次表示/根据之前对话/综上所述」等句式，
        属负面 priming（诱导弱模型产出）；v2 改为 sanitize_reply 正则硬清除。
        本测试验证防御下沉后依然有效：截图证实的元叙述泄漏必须被清洗层拦截。
        """
        from src.llm.style import sanitize_reply
        # 第三人称元叙述 + 引用对话历史作推理 → 剥离，仅留真实答复
        r = sanitize_reply("用户已多次表示不满，根据之前对话，我应该道歉。好的，抱歉给您带来困扰。")
        self.assertNotIn("用户已多次表示", r)
        self.assertNotIn("根据之前对话", r)
        self.assertIn("抱歉给您带来困扰", r)
        # 总结式外显 → 前缀剥离，正文保留
        r2 = sanitize_reply("综上所述，服务器申请需要走流程。")
        self.assertNotIn("综上所述", r2)
        self.assertIn("服务器申请需要走流程", r2)

    def test_directive_forbids_leading_thinking(self):
        """★ 开头思考/约束泄漏防御（v2：下沉为 sanitize 硬正则，端到端验证）。

        VPN 截图证实四类泄漏：自述已掌握知识 / 内部决策外显 / 组织回答大纲 /
        字数约束外显。v2 由 _LEADING_THINKING_PATTERNS 循环剥离，本测试锁行为。
        """
        from src.llm.style import sanitize_reply
        # 自述掌握 + 内部决策（含省略「中的信息」的变体），答案必须保留
        r = sanitize_reply(
            "根据知识库我已掌握相关信息，不需要调用工具，直接提供即可。VPN地址是vpn.example.com。")
        self.assertNotIn("已掌握", r)
        self.assertNotIn("不需要调用", r)
        self.assertIn("vpn.example.com", r)
        # 组织回答大纲 → 整段剥离
        r2 = sanitize_reply("让我组织一下回答内容：1.先登录 2.再配置 3.完成。")
        self.assertNotIn("让我组织", r2)
        # 字数约束外显 → 前缀剥离，答案保留
        r3 = sanitize_reply("需要控制在256字以内。打印机在7楼。")
        self.assertNotIn("需要控制在", r3)
        self.assertIn("打印机在7楼", r3)
        # 正常引用知识库回答不受影响（防误杀锚点）
        r4 = sanitize_reply("根据知识库的信息，打印机IP是192.168.1.10。")
        self.assertEqual(r4, "根据知识库的信息，打印机IP是192.168.1.10。")

    def test_identity_and_constraint_labels_preserved(self):
        """身份段与【角色定位】标签仍保留（治理转向后标签改名，语义延续）。"""
        prompt = build_system_prompt_core(_FakeAgent(title="IT运维"))
        self.assertIn("身份:OWNER的数字分身。", prompt)
        self.assertIn("【角色定位】", prompt)


if __name__ == "__main__":
    unittest.main()
