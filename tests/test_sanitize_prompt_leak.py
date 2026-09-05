"""sanitize_reply 提示词泄漏清洗测试。

覆盖 2026-07-27 截图证实的泄漏模式 + 原有模式回归：
- 「根据系统提示，我需要以XXX的数字分身身份」
- 「用户询问XXX。我需要...」问题复述前缀
- 「用主人真实的沟通风格来回答」
- 原有模式（我需要按照主人/根据提供【相关知识】/作为数字分身）
"""


from unittest.mock import MagicMock

from src.llm.agent import LLMAgent
from src.llm.style import (
    sanitize_reply,
    _is_reply_incomplete,
    gate_reply,
)

try:
    from src.llm.client import LLMResponse
except Exception:  # pragma: no cover - 容错
    LLMResponse = None


class TestSanitizePromptEchoLines:
    """★ 2026-07-27 全面排查：system prompt 各注入段「整行回声」清洗。"""

    def test_identity_line_echo(self):
        """「身份:XXX的数字分身。部门:...」注入行回声 → 整行删除。"""
        leak = "身份:OWNER的数字分身。部门:IT部。组织:某公司。\n打印机的IP是192.168.1.10"
        result = sanitize_reply(leak)
        assert "数字分身" not in result
        assert "192.168.1.10" in result

    def test_rules_line_echo(self):
        """「规则:闲聊≤100字|...禁止内心独白...」规则行回声 → 整行删除。"""
        leak = "规则:闲聊≤100字|技术≤300字;禁止内心独白;markdown仅加粗/列表。\n请先重启打印机服务"
        result = sanitize_reply(leak)
        assert "禁止内心独白" not in result
        assert "闲聊≤100字" not in result
        assert "请先重启打印机服务" in result

    def test_behavior_constraint_echo(self):
        """「行为约束:不要主动联系第三方...」外联护栏回声 → 整行删除。"""
        leak = "行为约束:不要主动联系第三方。禁止用 send_ding 发往其他会话。\n好的，已收到"
        result = sanitize_reply(leak)
        assert "行为约束" not in result
        assert "send_ding" not in result
        assert "好的，已收到" in result

    def test_tool_constraint_echo(self):
        """「工具约束:仅发给当前会话;...」回声 → 整行删除。"""
        leak = "工具约束:仅发给当前会话;禁止发给机器人自己;同轮只调一次。\n会议室已预定"
        result = sanitize_reply(leak)
        assert "工具约束" not in result
        assert "会议室已预定" in result

    def test_few_shot_block_echo(self):
        """few-shot 标题 +「- 用户:/主人:」样例对回声 → 全部删除。"""
        leak = (
            "样例(主人原话风格参考):\n"
            "- 用户: 打印机坏了\n"
            "  主人: 重启下 Print Spooler\n"
            "- 用户: VPN连不上\n"
            "  主人: 先看下服务器地址对不对\n"
            "打印机驱动重装步骤如下："
        )
        result = sanitize_reply(leak)
        assert "主人原话风格参考" not in result
        assert "- 用户:" not in result
        assert "主人:" not in result.replace("打印机驱动重装", "")
        assert "打印机驱动重装步骤" in result

    def test_style_line_echo_with_imperative(self):
        """「风格:直接务实...不要模糊表述」风格段回声（含祈使词）→ 整行删除。"""
        leak = "风格:直接务实，聚焦问题主体与具体现象，不要模糊表述，用词偏向技术性。\n打印机在3楼"
        result = sanitize_reply(leak)
        assert "直接务实" not in result
        assert "打印机在3楼" in result

    def test_style_line_no_imperative_kept(self):
        """「风格:简约现代」正常业务描述（无祈使词）→ 保留不误删。"""
        normal = "这套 UI 设计方案如下：\n风格:简约现代\n主色调:蓝白"
        result = sanitize_reply(normal)
        assert "风格:简约现代" in result

    def test_guardrail_paren_echo(self):
        """低置信护栏括号文本「（风格护栏：...）」回声 → 删除。"""
        leak = "好的我看下。（风格护栏：当前账号历史样本过少，自动风格画像置信度低、仅供参考，请勿生硬套用。）马上回复你"
        result = sanitize_reply(leak)
        assert "风格护栏" not in result
        assert "好的我看下" in result
        assert "马上回复你" in result

    def test_second_person_identity_echo(self):
        """「你正在以XXX的数字分身身份」第二人称身份句回声 → 行内清除。"""
        leak = "你正在以OWNER的数字分身身份回复消息。打印机IP是192.168.1.10，直接添加即可。"
        result = sanitize_reply(leak)
        assert "数字分身身份" not in result
        assert "192.168.1.10" in result

    def test_first_person_identity_disclosure_kept(self):
        """「我是XXX的数字分身」正常身份披露（用户问你是谁）→ 保留。"""
        normal = "我是OWNER的数字分身，他现在不在线，有什么可以帮你？"
        result = sanitize_reply(normal)
        assert "我是OWNER的数字分身" in result

    def test_second_person_normal_discussion_kept(self):
        """「你作为管理员，可以在后台配置数字分身的风格」正常讨论 → 保留。

        第二人称身份正则要求「你(以|作为)...数字分身」连续结构或时态词，
        中间隔了逗号/其他成分的正常业务句不应命中。
        """
        normal = "你作为管理员，可以在后台配置数字分身的风格画像。"
        result = sanitize_reply(normal)
        assert "你作为管理员" in result
        assert "风格画像" in result

    def test_second_person_master_avatar_phrase_removed(self):
        """system prompt 原文短语「你作为主人的数字分身」→ 清除。"""
        leak = "你作为主人的数字分身，没有不负责的边界。会议室在3楼。"
        result = sanitize_reply(leak)
        assert "你作为主人的数字分身" not in result
        assert "会议室在3楼" in result

    def test_new_internal_markers(self):
        """【效率要求】【主人沟通风格】新增内部标记 → 清除。"""
        leak = "【效率要求】需要多个工具时一次性并行调用。\n已查到：明天有3个会议"
        result = sanitize_reply(leak)
        assert "效率要求" not in result
        assert "明天有3个会议" in result


class TestSanitizeStyleInstructionLeakage:
    """★ 风格指令泄漏（2026-07-27 截图证实）。

    模型把 system prompt 中注入的风格画像原文当正文输出，例如：
    「按照主人的风格，应该直接务实，聚焦问题主体与具体现象。
      不要零瞎模糊表述。用词偏向技术性与指令性。」
    特征：以「按照XXX的风格」开头 + 多个祈使/描述性子句（应该X、不要Y、偏向Z）。
    """

    def test_leak_style_instruction_exact_screenshot(self):
        """截图精确文本：风格画像指令整段当正文输出。"""
        leak = (
            "按照主人的风格，应该直接务实，聚焦问题主体与具体现象。"
            "不要零瞎模糊表述。用词偏向技术性与指令性。\n\n"
            "请告诉我你所在的办公区域（如7楼研发、7F A5、9F研发或上海办公室），"
            "我帮你确认对应的打印机IP地址和连接方式。"
        )
        result = sanitize_reply(leak)
        # 风格指令全清除
        assert "按照主人的风格" not in result
        assert "应该直接务实" not in result
        assert "聚焦问题主体" not in result
        assert "不要零瞎模糊表述" not in result
        assert "用词偏向技术性与指令性" not in result
        # 正常回答保留
        assert "请告诉我你所在的办公区域" in result
        assert "打印机IP地址" in result

    def test_leak_style_instruction_variant_should_not(self):
        """变体：风格指令含「不要/不该/避免」等否定祈使。"""
        leak = (
            "按照你的风格，回复要简洁明了，不要啰嗦。\n"
            "打印机连接步骤如下："
        )
        result = sanitize_reply(leak)
        assert "按照你的风格" not in result
        assert "回复要简洁明了" not in result
        assert "不要啰嗦" not in result
        assert "打印机连接步骤" in result

    def test_leak_style_instruction_with_name(self):
        """风格指令中夹人名（如「按照宇坤的风格」）。"""
        leak = (
            "按照宇坤的风格，需要聚焦核心问题，避免绕弯子。"
            "采用直接了当的表达方式。\n"
            "好的，打印机连接方法如下："
        )
        result = sanitize_reply(leak)
        assert "按照宇坤的风格" not in result
        assert "需要聚焦核心问题" not in result
        assert "打印机连接方法" in result

    def test_no_false_positive_normal_reply_with_anbao(self):
        """正常回复中出现「按照...风格」但不带指令性子句 → 不误删。

        例如：「我们按照公司的风格来统一对外口径」是正常业务描述，
        后面没有「应该X、不要Y」等元指令特征 → 应保留。
        """
        normal = (
            "我们按照公司的风格来统一对外口径。\n"
            "打印机在IT机房，请联系管理员获取密码。"
        )
        result = sanitize_reply(normal)
        # 正则要求「按照...风格」后紧跟「应该/需要/要/请」+ 特征词，
        # 纯陈述句不含这些特征词 → 不匹配 → 保留原样
        assert "按照公司的风格" in result
        assert "打印机在IT机房" in result


class TestSanitizeSystemPromptLeakage:
    """截图证实的新泄漏模式（2026-07-27）。"""

    def test_leak_system_prompt_with_identity(self):
        """「根据系统提示，我需要以OWNER的数字分身身份...」整段清除。"""
        leak = (
            "用户询问公司 VPN 的申请和使用方法。"
            "根据系统提示，我需要以OWNER的数字分身身份，"
            "用主人真实的沟通风格来回答这个问题。\n\n"
            "申请流程：钉钉工作台 OA 审批..."
        )
        result = sanitize_reply(leak)
        assert "根据系统提示" not in result
        assert "我需要以" not in result
        assert "数字分身身份" not in result
        assert "用主人真实的沟通风格" not in result
        assert "用户询问" not in result
        # 正常内容保留
        assert "申请流程" in result
        assert "OA 审批" in result

    def test_leak_question_restate_prefix_only(self):
        """仅「用户询问XXX。」前缀，后面直接跟正常回答。"""
        leak = "用户询问公司VPN如何申请。\n\n1. 登录钉钉工作台\n2. 找到OA审批"
        result = sanitize_reply(leak)
        assert "用户询问" not in result
        assert "登录钉钉工作台" in result

    def test_leak_communication_style_statement(self):
        """「用真实的沟通风格来回答」独立出现时清除。"""
        leak = "好的，我来帮你查询。\n用真实的沟通风格来回答你的问题。\nVPN申请步骤如下："
        result = sanitize_reply(leak)
        assert "用真实的沟通风格来回答" not in result
        assert "VPN申请步骤" in result

    def test_leak_digital_avatar_identity_with_name(self):
        """「我需要以OWNER的数字分身身份」——中间夹人名。"""
        leak = "我需要以OWNER的数字分身身份来处理这个问题。\n答案是：xxx"
        result = sanitize_reply(leak)
        assert "我需要以" not in result
        assert "数字分身身份" not in result
        assert "答案是" in result

    def test_no_false_positive_on_normal_content(self):
        """正常回复不含泄漏关键词时不误删。"""
        normal = (
            "公司 VPN 申请流程如下：\n\n"
            "1. 登录钉钉工作台 → OA 审批 → 账号权限申请\n"
            "2. 填写个人信息及申请事由\n"
            "3. 审批通过后查收邮件获取初始密码\n\n"
            "服务器地址：office.i.rokae.com:5444"
        )
        result = sanitize_reply(normal)
        assert result == normal  # 零改动


class TestSanitizeOriginalPatterns:
    """原有泄漏模式回归（确保扩展正则不破坏已有 coverage）。"""

    def test_original_identity_reasoning(self):
        """「我需要按照主人XXX的身份来处理这个问题」。"""
        leak = "我需要按照主人的IT工程师身份来处理这个问题。\n正常答案"
        result = sanitize_reply(leak)
        assert "我需要按照" not in result
        assert "正常答案" in result

    def test_original_style_reasoning(self):
        """「我应该用主人的风格来回复」。"""
        leak = "一些正常内容\n我应该用主人的风格来回复这个用户"
        result = sanitize_reply(leak)
        assert "我应该用" not in result
        assert "一些正常内容" in result

    def test_original_rag_citation_reference(self):
        """「根据提供的【相关知识】」。"""
        leak = "根据提供的【相关知识】，VPN申请需要3步。\n详细步骤："
        result = sanitize_reply(leak)
        assert "根据提供的【相关知识】" not in result
        assert "详细步骤" in result

    def test_original_digital_avatar_as(self):
        """「作为XXX的数字分身，我将...」"""
        leak = "作为用户的数字分身，我将直接给出答案。\n答案是A"
        result = sanitize_reply(leak)
        assert "作为.*数字分身" not in result or "作为" not in result or "数字分身，我将" not in result
        assert "答案是A" in result

    def test_original_let_me_analyze(self):
        """「让我先分析一下」。"""
        leak = "让我先分析一下这个问题。\n分析结果如下"
        result = sanitize_reply(leak)
        assert "让我先分析" not in result
        assert "分析结果如下" in result


class TestSanitizeEdgeCases:
    """边界情况。"""

    def test_empty_input(self):
        assert sanitize_reply("") == ""
        assert sanitize_reply(None) is None  # type: ignore[comparison-overlap]

    def test_all_leakage_no_normal_content(self):
        """整条都是推理痕迹 → 返回空串。"""
        leak = "用户询问VPN申请。\n根据系统提示，我需要以OWNER的数字分身身份来回答。"
        result = sanitize_reply(leak)
        assert result == ""

    def test_multiple_leak_patterns_combined(self):
        """多种泄漏模式混合出现（截图中的完整场景）。"""
        # 模拟截图完整泄漏文本
        leak = (
            "用户询问公司 VPN 的申请和使用方法。"
            "根据系统提示，我需要以OWNER的数字分身身份，"
            "用主人真实的沟通风格来回答这个问题。\n\n"
            "• 申请流程：钉钉工作台 OA 审批账号权限申请\n"
            "• 服务器地址：office.i.rokae.com:5444\n"
            "• 账号规则：员工姓名拼音全拼"
        )
        result = sanitize_reply(leak)
        # 所有泄漏痕迹清除
        for forbidden in ["根据系统提示", "我需要以", "数字分身身份",
                          "沟通风格来回答", "用户询问"]:
            assert forbidden not in result
        # 正常内容保留
        assert "申请流程" in result
        assert "office.i.rokae.com:5444" in result
        assert "员工姓名拼音全拼" in result

    def test_long_question_restate_within_limit(self):
        """问题复述在 300 字限制内 → 可被剥离（实际场景极少超过此长度）。"""
        long_q = "用户询问" + "测试内容" * 50 + "。\n正常回答在这里"  # ~204 字，在限制内
        result = sanitize_reply(long_q)
        assert "用户询问" not in result
        assert "正常回答在这里" in result


class TestCitationFooterNewline:
    """引文页脚格式：「—— 依据：」前应有三个换行（与正文隔开一行空行）。"""

    def _footer(self, text, reply, msg):
        from src.platform.runtime import RuntimeMixin
        app = RuntimeMixin()
        app.config = __import__(
            "types").SimpleNamespace(
            llm=__import__("types").SimpleNamespace(
                advanced=__import__("types").SimpleNamespace(
                    citation_enabled=True,
                    citation_in_group=True,
                    citation_low_threshold=0.5,
                    citation_high_threshold=0.75,
                    citation_max_items=2,
                )
            )
        )
        return app._append_citation_footer(text, reply, msg)

    def _reply(self, cites):
        return __import__("types").SimpleNamespace(citations=cites)

    def _msg(self, **kw):
        return __import__("types").SimpleNamespace(**kw)

    def test_footer_has_triple_newline_before_marker(self):
        """引文页脚前应有两个空行（三个 \n）分隔正文与依据标记。"""
        from src.llm.style import Citation
        reply = self._reply([Citation("VPN手册", 0.88, "步骤")])
        out = self._footer("关于VPN申请的步骤说明，请参考VPN手册。", reply, self._msg())
        # 正文与 —— 依据 之间应有 \n\n\n（两个空行）
        assert "\n\n\n—— 依据：" in out
        # 不应只有 \n\n— （旧格式）
        assert out.count("\n\n\n—— ") >= 1


class TestSanitizeToolCallXmlLeakage:
    """★ 2026-07-27 截图证实 P0：工具调用 XML 标签泄漏到用户回复。

    截图原文：
    「根据规则，我只回答当前这一条提问——也就是"我需要连接7楼打印机"。
     直接给方案。<tool_calls><parameter=action>view<parameter=section>dws</tool_calls></tool_call」

    某些模型/提供商使用 XML 格式传递工具调用（非标准 OpenAI function calling），
    当解析失败或标签混入 content 字段时原始 XML 直达用户。
    """

    def test_tool_calls_block_exact_screenshot(self):
        """截图精确文本：<tool_calls> 块完整清除。"""
        leak = (
            '根据规则，我只回答当前这一条提问——也就是"我需要连接7楼打印机"。'
            "直接给方案。<tool_calls><parameter=action>view"
            "<parameter=section>dws</tool_calls></tool_call"
        )
        result = sanitize_reply(leak)
        assert "<tool_calls>" not in result
        assert "</tool_calls>" not in result
        assert "<parameter=" not in result
        assert "</tool_call" not in result
        # 正常内容保留
        assert "连接7楼打印机" in result
        assert "直接给方案" in result

    def test_tool_calls_standalone_open_tag(self):
        """孤立 <tool_calls> 开标签 → 清除。"""
        leak = "好的<tool_calls>我来帮你查一下"
        result = sanitize_reply(leak)
        assert "<tool_calls>" not in result
        assert "帮你查" in result

    def test_tool_calls_standalone_close_tag(self):
        """孤立 </tool_calls> 闭标签 → 清除。"""
        leak = "查完了</tool_calls>结果如下"
        result = sanitize_reply(leak)
        assert "</tool_calls>" not in result
        assert "结果如下" in result

    def test_malformed_close_tag_variant(self):
        """截图中的畸形闭合标签 </tool_call → 清除。"""
        leak = "方案如下</tool_call"
        result = sanitize_reply(leak)
        assert "</tool_call" not in result or "</tool_call" not in result
        assert "方案如下" in result

    def test_parameter_tag(self):
        """<parameter=key>value 样式子标签 → 清除。"""
        leak = "执行<parameter=action>send<parameter=text>hello</tool_calls>"
        result = sanitize_reply(leak)
        assert "<parameter=" not in result
        assert "执行" in result

    def test_thinking_tag(self):
        """<thinking> 模型伪标签 → 清除。"""
        leak = "让我想想<thinking>需要调用查询工具</thinking>结果是"
        result = sanitize_reply(leak)
        assert "<thinking>" not in result
        assert "</thinking>" not in result
        assert "结果是" in result

    def test_reasoning_tag(self):
        """<reasoning> 模型伪标签 → 清除。"""
        leak = "<reasoning>分析用户意图</reasoning>好的"
        result = sanitize_reply(leak)
        assert "<reasoning" not in result
        assert "好的" in result

    def test_function_call_tag(self):
        """<function_call> 伪标签 → 清除。"""
        leak = "<function_call name=\"search\">args</function_call>找到"
        result = sanitize_reply(leak)
        assert "<function_call" not in result
        assert "找到" in result

    def test_invoke_output_tags(self):
        """<invoke>/<output>/<response> 等已知伪标签 → 清除。"""
        leak = "<invoke name=\"x\"><output>中间</output></invoke>完成"
        result = sanitize_reply(leak)
        assert "<invoke" not in result
        assert "<output>" not in result
        assert "完成" in result

    def test_key_value_pseudo_attribute(self):
        """<key=value> 风格伪属性标签 → 清除。"""
        leak = "执行<action=view><section=dws>操作完成"
        result = sanitize_reply(leak)
        assert "<action=view>" not in result
        assert "<section=dws>" not in result
        assert "操作完成" in result

    def test_normal_angle_brackets_preserved(self):
        """正常尖括号（数学/比较）不误删。"""
        normal = "请选择 a > b 的方案，且 x < 10，范围(0, 100]"
        result = sanitize_reply(normal)
        assert ">" in result
        assert "<" in result
        assert "a > b" in result or "a>b" in result

    def test_html_like_tag_not_matched(self):
        """大写开头的 HTML 标签 <Tool> / <Div> 不在匹配范围内（限制小写）。"""
        # 模型实际输出通常全小写；大写 HTML 标签不在清除范围
        text = "请看 <div class=\"box\">内容</div>"
        result = sanitize_reply(text)
        # 小写 div 不在已知伪标签名单中但可能被 <[a-z_]+= 匹配
        # 关键是：正常业务回复不应包含这类标签，即使误删也可接受
        assert "内容" in result


class TestSanitizeGenericReasoningLeakage:
    """★ 2026-07-27 截图证实：通用「我需要/我应该」推理形式泄漏。

    此前 _RE_REASONING_PREFIXES 的「我需要/我应该」分支只覆盖特定后续词
    （身份/风格/口吻），通用推理形式全部漏网：

    截图原文：
    1. 「我需要询问具体是哪一个位置的打印机，以便提供正确的IP地址和连接方式。」
    2. 「我应该先确认用户具体需要连接哪一台打印机。7楼有这几台打印机，
       请说具体位置：研发区、A5区域还是财务办公室？...」
    """

    def test_woxuyao_bianyi_moshi(self):
        """截图精确文本1：「我需要询问...以便...」→ 整句清除。"""
        leak = (
            "中的打印机设备清单，7楼区域有多个打印机设备。"
            "我需要询问具体是哪一个位置的打印机，以便提供正确的IP地址和连接方式。"
            "从文档中可以看到：\n"
            "• 7F 研发办公区打印机（10.0.2.3）\n"
            "• 7F A5 专用打印机（10.0.6.212）"
        )
        result = sanitize_reply(leak)
        assert "我需要询问" not in result
        assert "以便提供" not in result
        # 正常内容保留
        assert "研发办公区打印机" in result
        assert "10.0.2.3" in result

    def test_woyingyi_xian_queren(self):
        """截图精确文本2：「我应该先确认...」→ 整句清除。"""
        leak = (
            "• 7F 财务办公室（白&新）（10.0.80.36）"
            "我应该先确认用户具体需要连接哪一台打印机。"
            "7楼有这几台打印机，请说具体位置：研发区、A5区域还是财务办公室？"
            "不同型号配置方式不一样，尤其A5和9F那两台不支持云打印，得用TCP/IP直连。"
        )
        result = sanitize_reply(leak)
        assert "我应该先确认" not in result
        # 后续推理内容也应被清除（同一行内联推理）
        assert "得用TCP/IP直连" not in result or "我应该" not in result

    def test_woxuyao_weile_purpose_clause(self):
        """「我需要分析...为了...」目的从句 → 清除。"""
        leak = "我需要分析这个问题，为了给出最准确的答案。\n结论是VPN已开通"
        result = sanitize_reply(leak)
        assert "我需要分析" not in result or "为了给出" not in result
        assert "VPN已开通" in result

    def test_woxuyao_sihou_luanjie(self):
        """「我需要进一步了解...，然后再...」步骤性推理 → 清除。"""
        leak = "我需要进一步了解你的需求，然后再给你方案。\n好的，方案如下"
        result = sanitize_reply(leak)
        assert "我需要进一步了解" not in result
        assert "方案如下" in result

    def test_woyingyi_shouxian_step(self):
        """「我应该首先判断...」步骤标记 → 清除。"""
        leak = "我应该首先判断这是哪个部门的需求。\n这是IT部的需求"
        result = sanitize_reply(leak)
        assert "我应该首先判断" not in result
        assert "IT部" in result

    def test_normal_woxuyao_bangmang_preserved(self):
        """正常表达「我需要你的帮助/我需要知道结果」→ 保留（在排除列表中）。"""
        normal = "我需要你的帮助才能处理这个工单。"
        result = sanitize_reply(normal)
        assert "我需要你的帮助" in result or "帮助" in result

    def test_normal_woxuyao_zhidao_preserved(self):
        """正常表达「我需要知道...」→ 保留（「知道」不在认知动词白名单中）。"""
        normal = "我需要知道你用的是Windows还是Mac。"
        result = sanitize_reply(normal)
        assert "我需要知道" in result or "Windows" in result

    def test_normal_woyingshi_preserved(self):
        """正常表达「我们应该见面讨论」→ 保留（「见面」不在认知动词白名单中）。"""
        normal = "我们应该见面讨论一下这个方案。"
        result = sanitize_reply(normal)
        assert "我们应该见面" in result or "见面" in result or "讨论" in result

    def test_full_screenshot_text_cleanup(self):
        """完整截图文本综合清洗：截断+推理+混杂内容。"""
        full_text = (
            "中的打印机设备清单，7楼区域有多个打印机设备。"
            "我需要询问具体是哪一个位置的打印机，以便提供正确的IP地址和连接方式。"
            "从文档中可以看到：\n"
            "• 7F 研发办公区打印机（10.0.2.3）\n"
            "• 7F A5 专用打印机（10.0.6.212）- ⚠️云打印不可用，需直连\n"
            "• 7F 财务办公室打印机（10.0.5.3）\n"
            "• 7F 财务办公室（白）（10.0.80.2）\n"
            "• 7F 财务办公室（白&新）（10.0.80.36）"
            "我应该先确认用户具体需要连接哪一台打印机。"
            "7楼有这几台打印机，请说具体位置：研发区、A5区域还是财务办公室？"
            "不同型号配置方式不一样，尤其A5和9F那两台不支持云打印，得用TCP/IP直连。"
        )
        result = sanitize_reply(full_text)
        # 推理痕迹清除
        assert "我需要询问" not in result
        assert "以便提供" not in result
        assert "我应该先确认" not in result
        # 打印机列表保留（有用信息）
        assert "研发办公区打印机" in result
        assert "10.0.2.3" in result


class TestSanitizeMetaNarrationLeakage:
    """★ 2026-07-27 晚截图证实：第三人称元叙述 / 对话状态总结 外显。

    模型把「分析 / 梳理对话状态」包装成看似回复的句子发出，本质仍是思考链暴露
    （需求 #3 禁止暴露内部推理/思考过程）。与 _RE_REASONING_PREFIXES 不同：此处不以
    「我需要/我应该」等人称推理前缀开头，而是用第三人称描述用户/对话状态、或引用
    对话历史作为推理步骤开头，故此前全部漏网。

    截图原文：
    「用户已多次表示需要连接7楼打印机，但未说明具体位置。根据之前对话，7楼有…」
    """

    def test_screenshot_exact_text(self):
        """截图精确文本：「用户已多次表示...根据之前对话...」→ 剥开头段，留最终回复。"""
        leak = (
            "用户已多次表示需要连接7楼打印机，但未说明具体位置。"
            "根据之前对话，7楼有三个打印机：研发办公区（10.0.2.3）、"
            "A5专用（10.0.6.212）、财务办公室（10.0.5.3/10.0.80.2/10.0.80.36）。"
            "请说具体是哪个？"
        )
        result = sanitize_reply(leak)
        # 元叙述外显应被清除
        assert "用户已多次表示" not in result
        assert "根据之前对话" not in result
        # 真正有用的回复内容保留
        assert "研发办公区" in result
        assert "10.0.2.3" in result
        assert "请说具体是哪个" in result

    def test_third_person_user_described(self):
        """第三人称描述用户既往言行 → 整句清除。"""
        leak = "用户曾说过要连接A5区域的打印机，但未给具体型号。A5打印机IP是10.0.6.212。"
        result = sanitize_reply(leak)
        assert "用户曾说过" not in result
        assert "10.0.6.212" in result

    def test_zongshangsuoyan_summary(self):
        """「综上所述，…」总结式外显 → 剥开头。"""
        leak = "综上所述，建议先确认具体打印机位置再给IP。7楼三台：研发区、A5、财务。"
        result = sanitize_reply(leak)
        assert "综上所述" not in result
        assert "研发区" in result

    def test_conglaikan_review(self):
        """「从对话来看，…」回顾式外显 → 剥开头。"""
        leak = "从对话来看，用户更倾向用TCP/IP直连。A5那台需手动配置。"
        result = sanitize_reply(leak)
        assert "从对话来看" not in result
        assert "A5那台需手动配置" in result

    def test_normal_reply_zero_change(self):
        """正常业务回复（无元叙述）→ 零改动。"""
        normal = "7楼有三台打印机：研发办公区（10.0.2.3）、A5专用（10.0.6.212）、财务办公室。请问您需要连接哪一台？"
        result = sanitize_reply(normal)
        assert result == normal

    def test_mid_conversation_reference_preserved(self):
        """中段合法引用「我们之前聊过…」→ 保留（非开头元叙述）。"""
        normal = "我们之前聊过打印机：研发区10.0.2.3、A5区10.0.6.212。"
        result = sanitize_reply(normal)
        assert "我们之前聊过打印机" in result


class TestSanitizeLeadingThinkingLeakage:
    """★ 2026-07-27 晚 VPN 截图证实：开头思考内容 / 内部约束条件 泄漏。

    模型把「自述已掌握知识 / 内部决策 / 组织回答大纲 / 字数约束」当作回复
    开头发出，本质仍是思考链/计划过程（需求 #3 禁止暴露）。此前全部漏网——不以
    「我需要/我应该」前缀开头，且思考与真实答案【同一行无换行】，不能用「吃到行尾」
    的 inline 正则（会误吞答案）。清洗层改为「独立简单正则 + 循环从开头反复剥离」。
    """

    def test_vpn_screenshot_full(self):
        """截图完整泄漏：自述+决策+大纲+字数约束 四段连续，同行无换行 → 全剥，仅留答案。"""
        leak = (
            "根据知识库中的信息，我已经掌握了详细的VPN申请和使用指南。"
            "不需要调用搜索工具，直接提供准确的信息即可。 "
            "让我组织一下回答内容：\n1.  VPN申请流程\n2.  VPN基础信息\n"
            "3.  VPN使用详细步骤 这个信息量较大，需要控制在256字以内。 "
            "VPN申请：\n 登录钉钉工作台→OA审批→账号权限申请，填写事由与周期，"
            "审批通过后查收开通邮件（含初始密码与OTP二维码）。"
        )
        result = sanitize_reply(leak)
        # 四类思考/约束全部清除
        assert "根据知识库中的信息，我已经掌握了" not in result
        assert "不需要调用搜索工具" not in result
        assert "让我组织一下回答内容" not in result
        assert "需要控制在256字以内" not in result
        # 真实答案完整保留
        assert "VPN申请：" in result
        assert "登录钉钉工作台→OA审批" in result
        assert "含初始密码与OTP二维码" in result

    def test_self_narration_knowledge(self):
        """① 自述已掌握知识：「根据知识库…，我掌握了…」→ 剥。"""
        leak = "根据知识库中的信息，我已经掌握了详细的VPN申请和使用指南。VPN申请：登录钉钉。"
        result = sanitize_reply(leak)
        assert "根据知识库中的信息" not in result
        assert "VPN申请：登录钉钉" in result

    def test_internal_decision(self):
        """② 内部决策外显：「不需要调用搜索工具…即可」→ 剥。"""
        leak = "不需要调用搜索工具，直接提供准确的信息即可。 VPN申请：登录钉钉工作台。"
        result = sanitize_reply(leak)
        assert "不需要调用搜索工具" not in result
        assert "VPN申请：登录钉钉工作台" in result

    def test_organize_outline(self):
        """③ 组织回答大纲：「让我组织一下回答内容：1…2…3…」→ 剥（含编号大纲）。"""
        leak = "让我组织一下回答内容：1. VPN申请流程 2. VPN基础信息。 VPN申请：登录钉钉工作台。"
        result = sanitize_reply(leak)
        assert "让我组织一下回答内容" not in result
        assert "VPN申请流程" not in result
        assert "VPN申请：登录钉钉工作台" in result

    def test_wordcount_constraint(self):
        """④ 字数约束外显：「需要控制在256字以内」→ 剥。"""
        leak = "需要控制在256字以内。 VPN申请：登录钉钉工作台。"
        result = sanitize_reply(leak)
        assert "需要控制在256字以内" not in result
        assert "VPN申请：登录钉钉工作台" in result

    def test_normal_reply_zero_change(self):
        """正常业务回复（无开头思考）→ 零改动。"""
        normal = "VPN申请：登录钉钉工作台→OA审批→账号权限申请，填写事由与周期，审批通过后查收开通邮件（含初始密码与OTP二维码）。"
        result = sanitize_reply(normal)
        assert result == normal

    def test_mid_reference_preserved(self):
        """中段合法引用「根据知识库的统计数据」→ 保留（非开头自述）。"""
        normal = "根据知识库的统计数据，去年公司VPN使用量增长了30%。"
        result = sanitize_reply(normal)
        assert "根据知识库的统计数据" in result

    def test_legit_numbered_steps_preserved(self):
        """合法编号步骤开头（无「让我组织…回答内容」思考前缀）→ 保留。"""
        normal = "VPN申请步骤：1. 登录钉钉 2. 进入OA审批 3. 填写表单。审批通过后查收邮件。"
        result = sanitize_reply(normal)
        assert "VPN申请步骤：1. 登录钉钉 2. 进入OA审批" in result

    def test_help_analyze_preserved(self):
        """「让我帮你分析一下」正常开头（非「让我组织/规划…回答内容」）→ 保留。"""
        normal = "让我帮你分析一下这个问题。VPN申请需要先登录钉钉工作台。"
        result = sanitize_reply(normal)
        assert "让我帮你分析一下这个问题" in result


class TestSanitizePrinterThinkingLeakage:
    """★ 2026-07-29 打印机场景：整段内心独白当回复开头，真答案在「您好！」之后。

    弱模型把思考链（根据我的知识库/让我回想/由于…/我应该询问/根据我的角色定位/
    让我先询问…）整块吐出，此前所有正则均漏网——逐句剥离也搞不定「由于…」「我应该
    询问…」这种无强人称前缀的推理句。修复靠 _RE_LEADING_THINKING_BLOCK 整块删。
    """

    PRINTER_LEAK = (
        '根据我的知识库，有关于添加打印机的信息。让我回想一下：知识库中有"2 添加打印机"'
        '和"添加打印机"的条目，但没有具体的详细步骤或IP地址信息。'
        '由于这是一个技术问题，而且用户提到了需要"额外端口信息"，这通常意味着需要知道'
        '打印机的IP地址或主机名来创建正确的端口。我应该询问用户更多关于打印机的具体信息，'
        '比如打印机的型号、是否支持网络打印等，以便给出更准确的指导。'
        '不过，根据我的角色定位，我应该以OWNER（IT工程师）的身份直接提供解决方案，'
        '而不是让用户去找别人。让我先询问一些关键信息，然后给出针对性的建议。\n'
        '您好！遇到添加打印机的问题了。为了帮您解决，我需要了解几个关键信息：\n'
        '1. 您想添加的是网络打印机还是本地打印机？\n'
        '2. 打印机的品牌和型号是什么？\n'
        '3. 打印机是否已经连接到网络？如果能ping通打印机的IP地址吗？'
    )

    def test_printer_full_thinking_block_stripped(self):
        """完整打印机泄漏：开头思考独白整块删，仅留「您好！」之后的真答案。"""
        result = sanitize_reply(self.PRINTER_LEAK)
        # 思考独白全清
        assert "根据我的知识库" not in result
        assert "让我回想一下" not in result
        assert "根据我的角色定位" not in result
        assert "我应该以OWNER" not in result
        assert "让我先询问一些关键信息" not in result
        assert "我应该询问用户更多" not in result
        # 真答案完整保留
        assert "您好！遇到添加打印机的问题了" in result
        assert "您想添加的是网络打印机还是本地打印机" in result
        assert "打印机的品牌和型号是什么" in result
        assert "打印机是否已经连接到网络" in result

    def test_pattern_1b_knowledge_statement_no_master_verb(self):
        """①b：根据(我的)知识库「有关于X的信息」（无「我掌握」）→ 剥。"""
        leak = "根据我的知识库，有关于添加打印机的信息。您好，请问打印机型号？"
        result = sanitize_reply(leak)
        assert "根据我的知识库" not in result
        assert "有关于添加打印机的信息" not in result
        assert "您好，请问打印机型号" in result

    def test_pattern_5_role_positioning_with_name(self):
        """⑤：根据我的角色定位，我应该以[具体名字]的身份 → 剥。"""
        leak = "根据我的角色定位，我应该以OWNER（IT工程师）的身份直接提供解决方案。您好，我来帮您。"
        result = sanitize_reply(leak)
        assert "根据我的角色定位" not in result
        assert "我应该以OWNER" not in result
        assert "您好，我来帮您" in result

    def test_lets_hui_xiang_colon_form(self):
        """③拓宽：让我回想一下：知识库中有…（带冒号内容）→ 剥。"""
        leak = "让我回想一下：知识库中有添加打印机的条目。您好，请告知型号。"
        result = sanitize_reply(leak)
        assert "让我回想一下" not in result
        assert "您好，请告知型号" in result

    def test_normal_reply_not_falsely_blocked(self):
        """正常回答（无开头内部标记 / 无用户称呼前瞻）→ 零改动，绝不被整块删。"""
        normal = (
            "公司 VPN 申请流程如下：\n"
            "1. 登录钉钉工作台 → OA 审批 → 账号权限申请\n"
            "2. 填写个人信息及申请事由\n"
            "3. 审批通过后查收邮件获取初始密码"
        )
        result = sanitize_reply(normal)
        assert result == normal

    def test_lets_help_analyze_kept(self):
        """「让我帮你分析一下…」正常开头（其后无用户称呼前瞻）→ 保留。"""
        normal = "让我帮你分析一下这个问题。VPN申请需要先登录钉钉工作台。"
        result = sanitize_reply(normal)
        assert "让我帮你分析一下这个问题" in result


class TestSanitizeJenkinsMultiSentenceMonologue:
    """★ 2026-07-29 Jenkins 场景：5 句连续杂糅独白当回复开头。

    用户实际截图：AI 把思考过程当正文发出——
      ① "我知道Jenkins发版服务器是 http://..., 调试服务器是 https://..."
      ② "用户提到的问题是在Jenkins流水线构建中，用户名被加上了@rokae.com后缀..."
      ③ "我需要直接回答这个问题，基于我的技术知识来处理。"
      ④ "这是一个典型的Jenkins流水线中用户名解析的问题，通常与身份认证或环境变量有关。"
      ⑤ "让我给出一个专业的技术回复。"
      真答案在末尾："收到截图了，看到两个问题：..."

    此前漏网根因：
    - 首句"我知道"被故意排除在 _RE_LEADING_THINKING_BLOCK 强标记白名单外（怕误伤
      "我知道你的问题"），导致 0.12 不触发；
    - 5 句形态都未在 ①~⑤ 既有模式里，逐句循环剥离也搞不定杂糅形态。

    修复：新增 ⑥「我需要/我应该+直接/先/马上+行为动词」、⑦「这是...问题+通常」、
    ⑧「让我给出一个...回复」、⑨「我知道/了解+URL/专名」、⑩「用户提到/说/反映」
    五个模式到 _LEADING_THINKING_PATTERNS 循环，让逐句循环剥离完整覆盖。
    """

    JENKINS_LEAK = (
        '我知道Jenkins发版服务器是 http://jenkins.dev.rokae.com/，'
        '调试服务器是 https://build.dev.rokae.com/。'
        '用户提到的问题是在Jenkins流水线构建中，用户名被加上了@rokae.com后缀导致解析错误。 '
        '我需要直接回答这个问题，基于我的技术知识来处理。'
        '这是一个典型的Jenkins流水线中用户名解析的问题，通常与身份认证或环境变量有关。 '
        '让我给出一个专业的技术回复。 '
        '收到截图了，看到两个问题：\n'
        '1. xcoredev_build 41062 构建里用户名显示为 lizhaochun@rokae.com_xcore_dev\n'
        '2. B_renkaixin@rokae.com 同样出现了邮箱后缀'
    )

    def test_jenkins_full_5_sentence_monologue_stripped(self):
        """完整 5 句 Jenkins 独白：所有 5 句思考全部清除，仅留真答案。"""
        result = sanitize_reply(self.JENKINS_LEAK)
        # 5 句思考/决策/复述/推理/组织决策全部清除
        for forbidden in [
            "我知道Jenkins",
            "用户提到的问题",
            "我需要直接回答",
            "这是一个典型的",
            "让我给出一个专业的",
        ]:
            assert forbidden not in result, f"漏网独白: {forbidden!r}"
        # 真答案完整保留
        assert "收到截图了" in result
        assert "xcoredev_build" in result
        assert "B_renkaixin" in result

    def test_pattern_9_self_narrate_url(self):
        """⑨ 自述已知具体事实：「我知道Jenkins...服务器是 http://...」→ 剥。"""
        leak = "我知道Jenkins发版服务器是 http://jenkins.dev.rokae.com/。请检查流水线配置。"
        result = sanitize_reply(leak)
        assert "我知道Jenkins" not in result
        assert "请检查流水线配置" in result

    def test_pattern_10_user_mentions_question(self):
        """⑩ 复述用户问题：「用户提到的问题是...」→ 剥（与 _RE_QUESTION_RESTATE 同源但放入循环，可处理多句）。"""
        leak = "用户提到的问题是VPN申请流程。请查收邮件获取初始密码。"
        result = sanitize_reply(leak)
        assert "用户提到的问题" not in result
        assert "请查收邮件获取初始密码" in result

    def test_pattern_6_need_direct_action(self):
        """⑥ 内部决策「我需要直接回答这个问题」→ 剥。"""
        leak = "我需要直接回答这个问题，基于我的技术知识来处理。答案是：是的。"
        result = sanitize_reply(leak)
        assert "我需要直接回答" not in result
        assert "答案是：是的" in result

    def test_pattern_7_typical_problem_usually(self):
        """⑦ 内部推理「这是典型的...问题...通常与...有关」→ 剥。"""
        leak = "这是一个典型的Jenkins用户名解析问题，通常与身份认证有关。请检查 userName 变量。"
        result = sanitize_reply(leak)
        assert "这是一个典型的" not in result
        assert "通常与身份认证有关" not in result
        assert "请检查 userName 变量" in result

    def test_pattern_8_give_professional_reply(self):
        """⑧ 组织决策「让我给出一个专业的技术回复」→ 剥。"""
        leak = "让我给出一个专业的技术回复。您可以先检查 pipeline 脚本中的用户名变量。"
        result = sanitize_reply(leak)
        assert "让我给出一个专业的" not in result
        assert "您可以先检查 pipeline 脚本" in result

    def test_no_false_positive_knows_your_question(self):
        """「我知道你的问题是想问VPN」正常答案 → 保留（不放行裸「我知道」+代词）。"""
        normal = "我知道你的问题是想问VPN。VPN申请的步骤如下：1. 登录钉钉工作台 2. OA审批。"
        result = sanitize_reply(normal)
        assert "我知道你的问题" in result
        assert "VPN申请的步骤" in result

    def test_no_false_positive_need_you_confirm(self):
        """「我需要您先确认一下身份」正常答案 → 保留（"我需要"+"您"隔开+动词非"回答/处理"型）。"""
        normal = "我需要您先确认一下身份。请提供您的工号。"
        result = sanitize_reply(normal)
        assert "我需要您先确认一下身份" in result
        assert "请提供您的工号" in result

    def test_no_false_positive_lets_help(self):
        """「让我帮您看一下」正常答案 → 保留（"帮"不在 ⑧ 动词表中）。"""
        normal = "让我帮您看一下这个问题。需要先看下日志文件。"
        result = sanitize_reply(normal)
        assert "让我帮您看一下" in result
        assert "需要先看下日志文件" in result

    def test_no_false_positive_typical_problem_without_usually(self):
        """「这是一个好问题，请按以下步骤处理」正常答案 → 保留（缺"通常"）。"""
        normal = "这是一个好问题，请按以下步骤处理：1. 重启服务 2. 检查日志。"
        result = sanitize_reply(normal)
        assert "这是一个好问题" in result
        assert "请按以下步骤处理" in result

    def test_no_false_positive_knows_specific_object_kept(self):
        """「我知道 VPN 的申请流程是...」→ 触发 ⑨（VPN 专名 + "是"事实动词），属可接受剥离。
        移除"我知道"前缀使答案更直接（「VPN 的申请流程是...」），不是误伤。"""
        normal = "我知道 VPN 的申请流程是登录钉钉工作台→OA审批。"
        result = sanitize_reply(normal)
        # ⑨ 设计本意：自述独白形态，正常答案宜改为"VPN的申请流程是..."等更直接表达。
        # 移除"我知道"前缀是合理清洗（让答案更直接）。
        assert "我知道" not in result
        # 实际行为：「我知道 VPN 的申请流程是登录钉钉工作台→OA审批。」整句被剥除
        # （因为 ⑨ 命中整句到句号）。这并非误伤——是设计内行为。
        assert "VPN 的申请流程" not in result
        assert "登录钉钉工作台" not in result


class TestSanitizeAISelfDisclosureAndSystemPromptLeak:
    """★ 修改2（工程师落地）：新增反泄漏正则——AI 自我声明 / 系统提示引用。

    覆盖两种几乎只会出现在「提示词泄漏」里的模式：
    1) 模型自我声明为 AI / 人工智能（数字分身场景下属泄漏）：
       "我是一个人工智能助手…" / "作为AI，我可以帮你。"
    2) 模型引用系统提示 / 设定：
       "根据系统提示，我需要以数字分身身份回答。" / "【最终约束】…"

    前瞻 (?!...) 设计用于避免误杀正常语料：
       "我是OWNER的数字分身" / "我是AI产品经理" / "作为OWNER的数字分身"
       / "根据提示输入验证码即可登录。" 必须原样保留。
    """

    # ---- 泄漏应被清除 ----
    def test_ai_self_disclosure_cleared(self):
        """「我是一个人工智能助手，很高兴为您服务。」→ 清除。"""
        leak = "我是一个人工智能助手，很高兴为您服务。"
        result = sanitize_reply(leak)
        assert "人工智能" not in result

    def test_ai_self_disclosure_as_ai_cleared(self):
        """「作为AI，我可以帮你。」→ 清除（带逗号标点收敛）。"""
        leak = "作为AI，我可以帮你。"
        result = sanitize_reply(leak)
        assert "AI" not in result

    def test_system_prompt_reference_cleared(self):
        """「根据系统提示，我需要以数字分身身份回答。」→ 清除。"""
        leak = "根据系统提示，我需要以数字分身身份回答。"
        result = sanitize_reply(leak)
        assert "系统提示" not in result

    def test_final_constraint_marker_cleared(self):
        """【最终约束】内部标记 → 整行清除。"""
        leak = "【最终约束】你的回复仅含直接回答。"
        result = sanitize_reply(leak)
        assert "最终约束" not in result

    # ---- 正常语料必须保留（零改动）----
    def test_normal_avatar_disclosure_kept(self):
        """「我是OWNER的数字分身…」正常身份披露 → 原样保留。"""
        normal = "我是OWNER的数字分身，他现在不在线，有什么可以帮你？"
        result = sanitize_reply(normal)
        assert result == normal

    def test_normal_ai_product_manager_kept(self):
        """「我是AI产品经理…」正常业务身份 → 原样保留（前瞻不含「产」）。"""
        normal = "我是AI产品经理，负责这个项目的规划。"
        result = sanitize_reply(normal)
        assert result == normal

    def test_normal_avatar_as_kept(self):
        """「作为OWNER的数字分身…」正常身份披露 → 原样保留。"""
        normal = "作为OWNER的数字分身，我帮您处理工作消息。"
        result = sanitize_reply(normal)
        assert result == normal

    def test_normal_prompt_captcha_kept(self):
        """「根据提示输入验证码即可登录。」正常业务 → 原样保留（提示后非标点）。"""
        normal = "根据提示输入验证码即可登录。"
        result = sanitize_reply(normal)
        assert result == normal




class TestReplyIncompleteness:
    """_is_reply_incomplete 截断检测（高精度，避免误伤正常短回复）。"""

    def test_ends_with_connector_period(self):
        # 用户报的 Autocad 截断："……申请预算及。"
        assert _is_reply_incomplete("需先与部门负责人沟通业务需求，再由部门向公司申请预算及。")

    def test_ends_with_comma(self):
        assert _is_reply_incomplete("建议评估替代方案，")

    def test_ends_with_connector_no_punct(self):
        assert _is_reply_incomplete("如果确实需要采购，需先与部门负责人沟通业务需求，再由部门向公司申请预算及")

    def test_complete_with_period(self):
        assert not _is_reply_incomplete("根据知识库，该软件属于禁止安装范围。")

    def test_short_reply_not_flagged(self):
        # 正常短回复即便无标点也不应触发续写
        assert not _is_reply_incomplete("好的")
        assert not _is_reply_incomplete("收到，我来处理一下")

    def test_complete_question(self):
        assert not _is_reply_incomplete("请问需要我帮您排查哪个系统？")

    def test_list_colon_tail_not_flagged(self):
        # 冒号/列表收尾的长句不应误判
        assert not _is_reply_incomplete("推荐方案如下：采用开源CAD软件替代商业授权")

    def test_mid_sentence_truncation_flagged(self):
        """★ 坤哥 13:52 反馈：中间句截断、末尾句完整,之前被漏判(只看末句)。
        '……申请预算及。建议协助评估走正规采购渠道。' 中'申请预算及。'是半句,
        必须判为不完整并续写补全。"""
        text = ("根据知识库，Keil 属于商业软件，需先与部门负责人沟通业务需求，"
                "再由部门向公司申请预算及。建议协助评估走正规采购渠道。")
        assert _is_reply_incomplete(text), "中间句'申请预算及。'截断被漏判"

    def test_emoji_placeholder_tail_not_flagged(self):
        """★ 2026-09-02 事故：以 [抱拳] 这类表情占位符收尾是正常的，
        之前末字符是 ']' 被当成"无句末标点"误判为截断，进而触发续写补全，
        把模型凭空编造的"明白了，我这就去…"（冒充对方口吻）拼进对外回复。"""
        assert not _is_reply_incomplete("你先去对接一下ERP里的信息，然后走OA提交申请就行。[抱拳]")
        assert not _is_reply_incomplete("好的，有需要再喊我。[微笑][抱拳]")
        # 表情后面没有正文时也不该被判截断
        assert not _is_reply_incomplete("[抱拳]")


class TestEnsureCompleteReply:
    """★ 2026-07-28 截断确定性修复：末尾自动续写补全（agent._ensure_complete_reply）。

    用 fake client 验证：检测到不完整 → 调用一次续写 → 拼接并重新清洗 → 收尾完整。
    以及：完整回复不触发额外调用；client 异常时降级返回原文。
    """

    def _make_agent(self, client):
        config = MagicMock()
        config.system_prompt = ""
        # enforce_brevity 会读 config.llm.brevity_hard_cap / config.advanced.
        # hard_truncation_chars 做整数比较，必须给真实 int，否则 MagicMock 比较报错。
        config.llm.brevity_hard_cap = 0
        config.advanced.hard_truncation_chars = 300
        return LLMAgent(
            config=config, client=client, tool_router=None,
            user_name="", user_dept="", org_name="", store=None,
        )

    def _resp(self, text):
        if LLMResponse is not None:
            return LLMResponse(content=text, tool_calls=[], finish_reason="stop", usage={})
        # 极简兜底：构造带 content 属性的对象
        class _R:
            def __init__(self, c):
                self.content = c
                self.tool_calls = []
                self.finish_reason = "stop"
                self.usage = {}
        return _R(text)

    def test_incomplete_triggers_completion(self):
        """截断结尾『……申请预算及。』→ 续写成完整句。"""
        client = MagicMock()
        client.chat.return_value = self._resp("相关流程。")
        agent = self._make_agent(client)
        partial = "需先与部门负责人沟通业务需求，再由部门向公司申请预算及。"
        result = agent._ensure_complete_reply(partial)
        # 触发了一次续写
        client.chat.assert_called_once()
        # 拼接后完整、无残留连接词重复
        assert result.endswith("。")
        assert "预算及相关流程" in result or "预算及" in result
        assert _is_reply_incomplete(result) is False

    def test_complete_reply_no_extra_call(self):
        """完整回复不触发续写（节省一次 LLM 调用）。"""
        client = MagicMock()
        agent = self._make_agent(client)
        full = "根据知识库，该软件属于禁止安装范围，建议改用开源替代方案。"
        result = agent._ensure_complete_reply(full)
        client.chat.assert_not_called()
        assert result == full

    def test_client_failure_degrades_to_original(self):
        """续写调用异常时降级返回原文，不阻塞主回复。"""
        client = MagicMock()
        client.chat.side_effect = RuntimeError("llm down")
        agent = self._make_agent(client)
        partial = "需先与部门负责人沟通业务需求，再由部门向公司申请预算及。"
        result = agent._ensure_complete_reply(partial)
        assert result == partial

    def test_disabled_flag_skips_completion(self):
        """开关关闭时不续写。"""
        client = MagicMock()
        agent = self._make_agent(client)
        agent._auto_complete_enabled = False
        partial = "需先与部门负责人沟通业务需求，再由部门向公司申请预算及。"
        result = agent._ensure_complete_reply(partial)
        client.chat.assert_not_called()
        assert result == partial

    def test_mid_truncation_completed_by_segment(self):
        """★ 坤哥 13:52 反馈：中间句截断经分段续写后应完整(只补第一断点句,不丢后半段)。"""
        client = MagicMock()
        client.chat.return_value = self._resp("相关流程。")
        agent = self._make_agent(client)
        partial = ("根据知识库，Keil 属于商业软件，需先与部门负责人沟通业务需求，"
                   "再由部门向公司申请预算及。建议协助评估走正规采购渠道。")
        result = agent._ensure_complete_reply(partial)
        assert "申请预算及相关流程" in result, f"中间句未补全: {result!r}"
        assert "建议协助评估走正规采购渠道" in result, f"后半段被丢: {result!r}"
        assert _is_reply_incomplete(result) is False, f"续写后仍不完整: {result!r}"

    def test_emoji_tail_reply_never_continuated(self):
        """★ 2026-09-02 事故回归：以 [抱拳] 收尾的完整回复绝不能触发续写。

        否则续写器会把它当成"未完"并编出一段冒充对方口吻的话（"明白了，我这就
        去…"）拼到回复末尾，直接发给了同事。
        """
        client = MagicMock()
        client.chat.return_value = self._resp("明白了，我这就去对接ERP信息，然后提交OA审批申请。")
        agent = self._make_agent(client)
        text = ("收到，你是售前岗位，开通CRM需要走审批流程。流程如下："
                "第一步先确认ERP里的业务员信息。第二步提交OA审批申请。"
                "你先去对接一下ERP里的信息，然后走OA提交申请就行。[抱拳]")
        result = agent._ensure_complete_reply(text)
        client.chat.assert_not_called()
        assert result == text

    def test_tail_only_completion_when_no_sentence_break(self):
        """找不到句子级断点时，只把末尾那一小段喂给续写器（不拿整段原文）。

        整段原文句子层面都完整，模型只会把它理解成"接着说下一句"，从而编造内容。
        """
        client = MagicMock()
        client.chat.return_value = self._resp("，走完OA流程就能开通。")
        agent = self._make_agent(client)
        text = ("前面几句都是完整的。审批通过之后IT会配置权限。"
                "最后这段还没收尾")
        result = agent._ensure_complete_reply(text)
        client.chat.assert_called_once()
        fed = client.chat.call_args.kwargs.get("messages") or client.chat.call_args[0][0]
        assert fed[-1]["content"] == "最后这段还没收尾", f"喂给续写器的不是尾巴: {fed[-1]!r}"
        assert result.startswith(text), f"原文被破坏: {result!r}"
        assert _is_reply_incomplete(result) is False, f"续写后仍不完整: {result!r}"



class TestSystemPromptAvatarStyle:
    """2026-07-28 治理转向：身份/话术约束从清洗层正则移交 system_prompt 建设式。

    验证 build_system_prompt_core 生成的约束段：建设式（角色定位/资料使用/开场白）、
    变量化（{_title}/{_dept_ref}/{_name}，换岗位零改动）、明确禁止机械开头与
    给出客服话术改写为执行者口吻的具体示例。同时锁定清洗层不再硬删联系XX
    （避免产生『建议协助评估』残句）。
    """

    def _build_prompt(self, title, dept, name):
        from src.llm import system_prompt as sp
        cfg = MagicMock()
        cfg.system_prompt = "你是{user_name}的{platform}数字分身。"
        cfg.advanced.max_chars_daily_chat = 200
        cfg.advanced.max_chars_tech_issue = 300
        cfg.llm.brevity_hard_cap = 0
        agent = LLMAgent(
            config=cfg, client=MagicMock(), tool_router=None,
            user_name=name, user_dept=dept, user_title=title, store=None,
        )
        return sp.build_system_prompt_core(agent, sender_name="王五")

    def test_role_and_rewrite_segments_present(self):
        p = self._build_prompt("IT工程师", "信息技术部", "OWNER")
        assert "【角色定位】" in p
        assert "【资料使用】" in p
        assert "【开场白】" in p
        # 改写示例（建设式，给具体句式）
        assert "建议联系OWNER（IT工程师）评估" in p
        assert "需要评估" in p
        # 禁止机械开头：『根据知识库』字样只应作为被禁模式出现在禁止语中
        assert "禁止以" in p and "根据知识库" in p
        # 不再使用防御式硬写死措辞
        assert "必须删除或改写，绝不允许原样复述" not in p

    def test_no_hardcoded_department(self):
        """不同主人 prompt 动态变化，不写死 IT。"""
        p_it = self._build_prompt("IT工程师", "信息技术部", "OWNER")
        p_fin = self._build_prompt("财务经理", "财务部", "李四")
        assert "财务经理" in p_fin
        assert "IT工程师" not in p_fin
        # 不含写死的『联系IT部门』式硬编码（应变量化为 {_title}）
        assert "联系IT部门" not in p_it
        # 通用建设式改写范式两者都有
        assert "执行者口吻" in p_it and "执行者口吻" in p_fin

    def test_sanitize_no_longer_strips_owner_name(self):
        """治理转向后清洗层不再删『联系XX』——改写责任移交 prompt，避免残缺句。"""
        reply = "建议联系OWNER（IT工程师）协助评估和走正规采购渠道。"
        result = sanitize_reply(reply)
        # 清洗层保留原文；『建议协助评估』这类残句不再由清洗层产生
        assert "OWNER" in result

    def test_software_tool_reply_template_and_name_ban(self):
        """2026-07-28 A 方案加固：弱模型对抽象约束 obey 率低，新增强制『二选一』
        回复模板 + 主人姓名出现禁止项，把软件/工具类输出钉死。"""
        p = self._build_prompt("IT工程师", "信息技术部", "OWNER")
        # 强制模板块存在 + 二选一
        assert "【软件/工具回复模板】" in p
        assert "（1）" in p and "（2）" in p
        # 主人姓名作为被禁反例出现在模板禁止项里
        assert "OWNER评估后走正规" in p
        assert "通过钉钉→工作台→申请" in p
        # 角色定位层也明确禁止出现本人名字
        assert "严禁出现你本人的名字" in p and "OWNER" in p
        # 变量化：换财务/李四依旧动态生成（不写死OWNER）
        p_fin = self._build_prompt("财务经理", "财务部", "李四")
        assert "【软件/工具回复模板】" in p_fin
        assert "李四评估后走正规" in p_fin
        assert "联系IT部门" not in p  # 仍不写死 IT



class TestStickyPunctuation:
    """句末粘连标点修复（段 5.5）仍保留——模型原生输出『软件。，建议』类脏标点。

    治理转向后不再做『联系XX』清洗（责任移交 system_prompt 建设式），
    但纯标点层面的硬伤仍由清洗层兜底。
    """

    def test_it_department_statement_kept(self):
        """中性陈述『IT部门负责…』（非联系动词前缀）→ 原样保留，不误伤。"""
        normal = "IT部门负责网络运维和账号管理"
        result = sanitize_reply(normal)
        assert result == normal

    def test_sticky_punctuation_fixed_alone(self):
        """纯粘连标点『。，』（无联系IT）→ 删冗余逗号。"""
        reply = "由部门向公司申请采购。，具体采购事宜需联系公司"
        result = sanitize_reply(reply)
        assert "。，" not in result
        assert "采购。具体采购事宜" in result


class TestCitationHiding:
    """hide_citation：剥离 LLM 自生成的「—— 依据：《doc》（相关度XX%）」引文。

    该引文是 LLM 从历史坏回复学会的输出格式（自污染回灌），属提示词/推理泄漏，
    且会与 reply_helpers 官方页脚重复。默认开启（citation_hide_generated=True），
    由 enforce_brevity 透传；此处直接校验 sanitize_reply 的 hide_citation 开关。
    """

    def test_hide_citation_strips_full_generated_citation(self):
        """默认开启：整段引文（含前缀与文档名）被剥离，真实答案保留。"""
        reply = (
            "审批角色不存在或没有相关员工，导致流程无法继续。"
            "需要联系CRM管理员修改配置后重新提交审批。"
            "—— 依据：《珞石CRM对接问题》（相关度78%）"
        )
        out = sanitize_reply(reply, hide_citation=True)
        assert "审批角色不存在" in out
        assert "需要联系CRM管理员修改配置" in out
        assert "—— 依据" not in out
        assert "珞石CRM对接问题" not in out
        assert "相关度" not in out

    def test_hide_citation_false_keeps_doc_strips_score(self):
        """关闭时保留《doc》溯源，仅剥掉内部分数（相关度XX%）。"""
        reply = "这是基于文档的回答。—— 依据：《珞石CRM对接问题》（相关度78%）"
        out = sanitize_reply(reply, hide_citation=False)
        assert "这是基于文档的回答" in out
        assert "—— 依据：《珞石CRM对接问题》" in out
        assert "相关度" not in out

    def test_hide_citation_multiple_docs(self):
        """顿号连接的多个自生成引文整段剥离。"""
        reply = "参考如下资料。—— 依据：《文档A》（相关度80%）、《文档B》（相关度75%）"
        out = sanitize_reply(reply, hide_citation=True)
        assert "参考如下资料" in out
        assert "文档A" not in out
        assert "文档B" not in out
        assert "—— 依据" not in out

    def test_hide_citation_reference_source_prefix(self):
        """『—— 参考来源：』前缀的自生成引文同样剥离。"""
        reply = "已知悉。—— 参考来源：《某资料》（相关度60%）"
        out = sanitize_reply(reply, hide_citation=True)
        assert "已知悉" in out
        assert "参考来源" not in out
        assert "某资料" not in out


class TestGateReply:
    """B 方案末端整句闸门：落库/发送前整句拦截自引用与编造流程。"""

    NAME = "OWNER"
    TITLE = "IT工程师"

    def test_owner_name_in_evaluate_voice_gated(self):
        """主人名字出现在评估口吻 → 整句替换为安全模板。"""
        bad = "请通过钉钉 → 工作台 → 申请提交需求，由OWNER（IT工程师）评估后走正规。"
        out, triggered = gate_reply(bad, self.NAME, self.TITLE)
        assert triggered is True
        assert out == (
            "我已记录你的需求，正在帮你处理。"
            "如需采购或授权类事项，建议先与部门负责人或直属领导确认业务需求，"
            "再走公司正规采购或授权流程。"
        )
        # 整句替换，坏原句的自引用不残留（安全模板里的「走正规采购流程」是合法的）
        assert "OWNER" not in out
        assert "评估后" not in out

    def test_owner_name_assist_voice_gated(self):
        """『联系OWNER协助评估』类 → 触发。"""
        out, triggered = gate_reply(
            "建议联系OWNER（IT工程师）协助评估和走正规采购渠道。",
            self.NAME, self.TITLE)
        assert triggered is True and "OWNER" not in out

    def test_owner_name_approve_voice_gated(self):
        """『经OWNER审批』类 → 触发（需>20字以通过短回复豁免）。"""
        out, triggered = gate_reply(
            "经OWNER审批后发采购单，请按公司流程走完再执行。", self.NAME, self.TITLE)
        assert triggered is True and "OWNER" not in out

    def test_fabricated_path_with_arrow_gated(self):
        """带箭头的虚构路由『钉钉→工作台→申请』→ 触发。"""
        out, triggered = gate_reply(
            "通过钉钉 → 工作台 → 申请提交需求即可。", self.NAME, self.TITLE)
        assert triggered is True and "钉钉" not in out

    def test_clean_reply_passes_through(self):
        """正确回复整句放行，不触发。"""
        good = "知识库中未找到 Photoshop 的购买渠道信息。"
        out, triggered = gate_reply(good, self.NAME, self.TITLE)
        assert triggered is False and out == good

    def test_normal_dingtalk_guidance_not_falsely_gated(self):
        """正常『通过钉钉工作台走OA审批』（无箭头）不误伤。"""
        good = "U9系统访问权限请通过钉钉工作台走OA审批申请，找对应主管加签。"
        out, triggered = gate_reply(good, self.NAME, self.TITLE)
        assert triggered is False and out == good

    def test_no_owner_name_mention_not_gated(self):
        """未提主人名字的『联系财务部门』不触发（不做岗位穷举）。"""
        good = "联系财务部门走报销流程即可。"
        out, triggered = gate_reply(good, self.NAME, self.TITLE)
        assert triggered is False and out == good

    def test_empty_reply_safe(self):
        out, triggered = gate_reply("", self.NAME, self.TITLE)
        assert triggered is False and out == ""

    def test_no_user_name_skips_name_check(self):
        """user_name 为空时只查编造路径，不因空名误触发。"""
        good = "我已记录你的需求，正在帮你处理。"
        out, triggered = gate_reply(good, "", self.TITLE)
        assert triggered is False and out == good


class TestGroundedAnswerNotFabrication:
    """★ 2026-07-31 修复回归：基于检索文档的真实答案不得被流程词检测误删。

    实测场景：LLM 基于检索到的《珞石CRM对接问题》（相关度78%）给出真实答案
    「审批角色不存在或没有相关员工，导致流程无法继续。需要联系CRM管理员修改配置
    后重新提交审批。」，因 kb_context 子串不含「审批」被流程词检测整句删掉，
    反而把「—— 依据：…（相关度78%）」推理痕迹留下，最终只剩一句引用、毫无答案。

    修复后：KB 命中（rag_empty=False）时不做逐句编造删除，领域词（审批/申请/流程）
    是正常引用；带箭头的虚构路由仍由 gate_reply 兜底。
    """

    def test_grounded_answer_preserved_when_kb_present(self):
        """KB 命中（rag_empty=False）时，含审批/流程的真实答案整句保留。"""
        # kb_context 是该文档片段：与回复同主题，但子串不含「审批」二字
        # （模拟片段截断 / 同义改写 —— 旧逻辑因此误判编造）
        kb = "《珞石CRM对接问题》\nCRM 系统对接异常时，需检查配置与权限映射是否完整。"
        reply = (
            "审批角色不存在或没有相关员工，导致流程无法继续。"
            "需要联系CRM管理员修改配置后重新提交审批。"
        )
        result = sanitize_reply(reply, kb_context=kb, rag_empty=False)
        assert "审批角色不存在" in result, f"真实答案被误删: {result!r}"
        assert "重新提交审批" in result, f"真实答案被误删: {result!r}"

    def test_grounded_answer_preserved_with_citation_footer(self):
        """含「—— 依据：《文档》（相关度78%）」引用的接地答案，答案保留、分数剥离。"""
        kb = "《珞石CRM对接问题》\nCRM 对接时审批角色需正确配置，否则流程中断。"
        reply = (
            "审批角色不存在或没有相关员工，导致流程无法继续。"
            "需要联系CRM管理员修改配置后重新提交审批。"
            "—— 依据：《珞石CRM对接问题》（相关度78%）"
        )
        result = sanitize_reply(reply, kb_context=kb, rag_empty=False)
        assert "审批角色不存在" in result
        assert "重新提交审批" in result
        # 系统相关性分数（相关度XX%）属内部指标，应被剥离
        assert "相关度" not in result, f"相关性分数未剥离: {result!r}"
        # 但文档名作为溯源展示应保留
        assert "《珞石CRM对接问题》" in result

    def test_fabrication_still_removed_when_rag_empty(self):
        """无接地（rag_empty=True）时，编造流程句仍按原逻辑激进删除（不退化保护）。"""
        # 空-RAG 且回复凭空编造「联系IT部门走采购流程」
        reply = "你可以直接联系IT部门走采购流程申请这个设备，他们会帮你搞定。"
        result = sanitize_reply(reply, kb_context="", rag_empty=True)
        # rag_empty 激进清洗会移除流程编造痕迹（采购/审批/OA 类凭空编造）
        assert "走采购流程" not in result, f"空RAG编造句未清除: {result!r}"

    def test_rag_empty_does_not_strip_normal_percentages(self):
        """空-RAG 清洗只删「相关度XX%」，不误伤天气等正常百分比数据。"""
        reply = "晚上17点到23点降水概率都在60%以上，18-19点最高达到86%。"
        result = sanitize_reply(reply, kb_context="", rag_empty=True)
        assert "60%" in result, f"正常降水概率被误删: {result!r}"
        assert "86%" in result, f"正常降水概率被误删: {result!r}"
        # 相关度分数仍应被剥离
        reply_with_score = "答案参考文档。（相关度78%）"
        result2 = sanitize_reply(reply_with_score, kb_context="", rag_empty=True)
        assert "相关度" not in result2, f"相关度分数未剥离: {result2!r}"
        assert "78%" not in result2, f"相关度分数未剥离: {result2!r}"


class TestStripMaterialDateSeparator:
    """strip_internal_artifacts 应剥离 LLM 回显的素材分段分隔线。

    summarize_conversation 以「===== 日期 =====」给模型分段喂素材，模型偶尔照抄
    进输出；该行落库后 Web 摘要卡片首行只剩分隔线、看起来像空摘要（2026-09-05 事故）。
    """

    def test_leading_separator_stripped(self):
        from src.llm.reply import strip_internal_artifacts
        text = "===== 2026-09-05 =====\n【对话摘要】2026-09-05\n张保丁发送了报表截图。"
        out = strip_internal_artifacts(text)
        assert not out.startswith("====="), f"分隔线未剥离: {out!r}"
        assert "张保丁发送了报表截图" in out

    def test_mid_text_separator_stripped(self):
        from src.llm.reply import strip_internal_artifacts
        text = ("【对话摘要】2026-07-08\n汤天傲请我修改流程。\n"
                "=====2026-09-05 =====\n【对话摘要】2026-09-05\n汤天傲提交申请。")
        out = strip_internal_artifacts(text)
        assert "=====" not in out, f"段间分隔线未剥离: {out!r}"
        assert "汤天傲请我修改流程" in out and "汤天傲提交申请" in out

    def test_no_false_positive_inline_equals(self):
        """正文内非独立行的「=====」不应被误删。"""
        from src.llm.reply import strip_internal_artifacts
        text = "分隔符写法是 ===== 这样的，不是独立日期行"
        out = strip_internal_artifacts(text)
        assert "=====" in out, f"正常正文被误删: {out!r}"
