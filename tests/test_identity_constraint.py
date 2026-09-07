"""身份约束注入测试：system prompt 在拿到职位时应包含『你就是该领域执行者』提示，
未拿到职位时应降级为『默认假设用户能亲自动手』。

不写死具体岗位，验证：prompt 内容是变量化的，根据 user_title 动态切换。
"""
from types import SimpleNamespace

from src.llm.system_prompt import build_system_prompt, build_system_prompt_core


def _fake_agent(user_title: str, user_name: str = "主人", user_dept: str = "研发部",
                org_name: str = "公司", platform_id: str = "dingtalk") -> SimpleNamespace:
    cfg = SimpleNamespace(
        system_prompt="你是助手。",
        advanced=SimpleNamespace(max_chars_daily_chat=200, max_chars_tech_issue=600),
    )
    return SimpleNamespace(
        config=cfg, user_name=user_name, user_dept=user_dept, org_name=org_name,
        user_title=user_title, platform_id=platform_id,
    )


def test_identity_constraint_includes_job_title_when_set():
    """拿到职位时，prompt 应包含『该领域执行者』+ 建设式禁止绕弯话（甩锅式回复）。"""
    prompt = build_system_prompt_core(_fake_agent(user_title="运维工程师"))
    assert "职位:运维工程师" in prompt
    assert "执行者" in prompt
    # 关键：建设式给出绕弯话示例（建议联系XX/找XX部门/提工单），而不是写死岗位
    assert "提工单" in prompt
    # 应有条件性提示：超出职责才走外部路径
    assert "超出" in prompt and "职责范围" in prompt


def test_identity_constraint_falls_back_when_no_title():
    """没拿到职位（个人模式 / 未配 CLI）时，降级为『按主人能亲自动手的方向给方案』，
    避免 AI 走『找人/提工单』弯路。"""
    prompt = build_system_prompt_core(_fake_agent(user_title=""))
    assert "职位:" not in prompt, "无职位时不应注入『职位:』字段"
    assert "按主人能亲自动手的方向给方案" in prompt
    assert "找人/找部门/提工单" in prompt
    # 兜底仍是「回答层面不推诿」；但旧的绝对规则（分身无边界）必须已移除——
    # 它会让分身替主人接下归属未定的活（2026-08-28 事故）
    assert "回答边界" in prompt
    assert "没有『我不负责/这不归我管』的边界" not in prompt


def test_identity_constraint_works_for_non_it_roles():
    """不写死岗位：『财务』『HR』等任何职位都触发同样逻辑。"""
    for title in ("运维工程师", "前端开发", "财务经理", "HRBP", "采购专员"):
        prompt = build_system_prompt_core(_fake_agent(user_title=title))
        assert f"职位:{title}" in prompt, f"{title} 应被注入"
        assert "执行者" in prompt, f"{title} 应触发执行者约束"


def test_identity_constraint_does_not_override_base_identity():
    """新增约束不应破坏原有身份段（部门/组织/对话者）。"""
    prompt = build_system_prompt_core(
        _fake_agent(user_title="运维工程师", user_name="张三", user_dept="研发部", org_name="X公司")
    )
    # 原有身份字段都在
    assert "部门:研发部" in prompt
    assert "组织:X公司" in prompt
    assert "职位:运维工程师" in prompt
    # 新增的身份约束标签（治理转向后改名【角色定位】）
    assert "【角色定位】" in prompt


def test_identity_constraint_with_external_speaker():
    """外部对话者场景下，身份约束同样生效（不应因对话者不同而跳过）。"""
    agent = _fake_agent(user_title="运维工程师")
    prompt = build_system_prompt_core(agent, sender_name="李四")
    assert "对话者:李四(外部)" in prompt
    assert "【角色定位】" in prompt
    assert "执行者" in prompt


def test_identity_constraint_no_self_denial_but_no_taking_on_work():
    """回答层面「不推诿」（不甩锅别的部门），但绝不替主人接活——两者必须拆开且同时成立。

    回归 2026-08-28 事故：旧规则「没有『我不负责/这不归我管』的边界」把
    「回答不推诿」与「替主人承接任务」混为一谈，导致分身对「你能操作不？」
    直接回「我这边按文档去操作」。
    """
    for label, prompt in (
        ("有职位", build_system_prompt_core(_fake_agent(user_title="IT运维"))),
        ("无职位兜底", build_system_prompt_core(_fake_agent(user_title=""))),
    ):
        # 旧的绝对规则必须已移除，否则与【承诺边界】自相矛盾、模型会择一执行
        assert "没有『我不负责/这不归我管』的边界" not in prompt, label
        # 回答层面仍不推诿（不能治好接活又退回甩锅）
        assert "回答边界" in prompt, label
        assert "按主人能亲自动手" in prompt, label
        assert "不推诿" in prompt, label
        # 且明确不许替主人接活
        assert "【承诺边界" in prompt, label


def _fake_full_agent() -> SimpleNamespace:
    """补齐 build_system_prompt 所需属性的 fake（few-shot / 技能 / 风格段留空）。"""
    cfg = SimpleNamespace(
        system_prompt="你是助手。",
        advanced=SimpleNamespace(max_chars_daily_chat=200, max_chars_tech_issue=600),
        dynamic_few_shot=False,
        few_shot_examples=[],
    )
    return SimpleNamespace(
        config=cfg, user_name="张三", user_dept="研发部", org_name="X公司",
        user_title="IT运维", platform_id="dingtalk",
        tool_router=None, skill_manager=None, skills_config=None,
        few_shot_examples=[], _get_style_prompt=lambda: "",
    )


def test_commitment_boundary_does_not_imply_owner_is_present():
    """承诺边界不得再假设「主人就在对话对面」。

    回归 2026-08-29：旧措辞「把决定权交回本人」「先跟本人对一下再答复你」
    全程第三人称称呼主人，隐含主人在场。但分身是与外部同事对话，
    主人根本不在这个会话里——模型为满足「交回本人」便虚构主人在场，
    把当前对话者错认成主人/转述者，反问「这条消息是某某发来的吗」。
    """
    prompt = build_system_prompt_core(_fake_agent(user_title="IT运维"))
    # 旧的歧义动作指令必须消失
    assert "把决定权交回本人" not in prompt
    # 改为「向对方说明」+ 显式禁止反问发件人
    assert "★谁在和你说话" in prompt
    assert "主人本人并不在这个会话里" in prompt
    assert "严禁反问对方" in prompt
    # 承诺边界本身不能被削弱，也不能因此退化成甩锅病
    assert "一律不得替主人答应" in prompt
    assert "不要因此变得畏缩" in prompt


def test_speaker_anchor_sits_at_recency_position():
    """身份锚定必须落在完整 prompt 末尾近因位，光靠开头那一份不够。

    实测 core 里的「对话者:xxx(外部)」位于全文第 55 字符（占比 1.3%），
    身后压着 4318 字符、30 个指令段，末尾是带「最高优先级」的承诺边界。
    按本项目已验证的近因效应，开头身份信息几乎不参与最终决策。
    """
    prompt = build_system_prompt(_fake_full_agent(), sender_name="魏欣悦")
    anchor_at = prompt.rfind("【当前对话者】")
    assert anchor_at != -1, "完整 prompt 末尾必须有身份锚定"
    # 必须压在承诺边界提醒之后，否则纠正不了它「交回本人」的副作用
    assert anchor_at > prompt.rfind("【承诺边界提醒】")
    # 距末尾足够近，确保不被后续内容稀释
    assert len(prompt) - anchor_at < 400
    tail = prompt[anchor_at:]
    assert "魏欣悦" in tail
    assert "不要反问这条消息是谁发的" in tail


def test_sender_missing_does_not_fall_silent():
    """sender 缺失时不得静默跳过整段身份——否则模型默认对面是主人本人。"""
    prompt = build_system_prompt_core(_fake_agent(user_title="IT运维"))
    assert "对话者:未能识别(外部)" in prompt
    assert "不要向对方追问姓名" in prompt


class TestSelfReferencePronoun:
    """★ 2026-09-07 事故回归：分身用第三人称/主人姓名称呼自己。

    截图原文（对方问 wifi 密码，分身引用上文那条 LDAP 账号信息）：
        「输入用户名 sunxueyao，密码 zuK4Ma!f（徐宇坤刚才发你的）」
    而「徐宇坤」正是分身所代表的主人本人——分身用第三人称称呼了自己，
    对面的人会以为还有第三个人在场。

    漏网原因（两点，缺一不可）：
      1. 【角色定位】里那条「严禁出现你本人的名字」只论证「不存在联系自己」，
         覆盖不到「归属说明」这一场景，且埋在 core 中段被后续指令段稀释；
      2. 【承诺边界】通篇用「主人」这个第三人称词，等于在教模型「主人=第三方」。

    修复策略：不扩 gate_reply 正则（它命中后整句替换为安全模板，会把
    「账号/密码」这类有用正文一起删掉），改为在近因位显式定义人称映射，
    并把「归属说明」点名，同时显式放行真正的第三方（防止过度禁止）。
    """

    def test_self_reference_directive_present(self):
        """core 必须含【自称与人称】，且压在承诺边界之后（近因位）。"""
        prompt = build_system_prompt_core(_fake_agent(user_title="IT运维", user_name="徐宇坤"))
        assert "【自称与人称" in prompt
        # 近因位：承诺边界通篇用「主人」第三人称，自称指令必须在其后纠偏
        assert prompt.rindex("【自称与人称") > prompt.rindex("【承诺边界 - 最高优先级】")

    def test_positive_example_first_person(self):
        """必须给出正例「我刚才发你的」——只禁止不示范，弱模型无从模仿。"""
        prompt = build_system_prompt_core(_fake_agent(user_title="IT运维"))
        assert "我刚才发你的" in prompt

    def test_attribution_scenario_covered(self):
        """★ 核心：归属说明场景（某条消息/物料是谁发的）必须被点名禁止。

        旧的「严禁出现你本人的名字」只管「联系自己」，管不到这里，
        正是本次事故的直接漏网点。
        """
        prompt = build_system_prompt_core(_fake_agent(user_title="IT运维", user_name="徐宇坤"))
        # 事故原句形态被显式列为反例
        assert "徐宇坤刚才发你的" in prompt
        # 归属说明这一用法单独点名
        assert "归属说明" in prompt
        assert "徐宇坤刚才发的" in prompt

    def test_owner_name_variable_not_hardcoded(self):
        """变量化：换主人姓名后禁止示例随之变化，不得残留旧名字。"""
        p_a = build_system_prompt_core(_fake_agent(user_title="IT运维", user_name="徐宇坤"))
        p_b = build_system_prompt_core(_fake_agent(user_title="财务经理", user_name="李四"))
        assert "徐宇坤刚才发你的" in p_a
        assert "李四刚才发你的" in p_b
        # 不得写死某个具体人名
        assert "徐宇坤" not in p_b
        assert "李四" not in p_a

    def test_third_party_explicitly_allowed(self):
        """不得过度禁止：真正的第三方照常用姓名，否则模型连「张三说过…」都不敢写。"""
        prompt = build_system_prompt_core(_fake_agent(user_title="IT运维", user_name="徐宇坤"))
        assert "真正的第三方" in prompt
        assert "本条不限制" in prompt

    def test_no_owner_name_falls_back_but_keeps_pronoun_rule(self):
        """拿不到主人姓名时降级为不点名版本，但人称映射照样锁死。"""
        prompt = build_system_prompt_core(_fake_agent(user_title="IT运维", user_name=""))
        assert "【自称与人称" in prompt
        assert "自称一律用「我」" in prompt
        assert "我刚才发你的" in prompt
        # 无名字可点名，不应出现空的「」占位
        assert "「」" not in prompt

    def test_speaker_anchor_carries_self_anchor(self):
        """最末尾的身份锚定必须同时锚定「我是谁」，不能只锚定「对面是谁」。"""
        prompt = build_system_prompt(_fake_full_agent(), sender_name="孙雪瑶")
        tail = prompt[prompt.rfind("【当前对话者】"):]
        assert "自称用「我」" in tail
        assert "我刚才发你的" in tail
        # 近因位长度预算由 test_speaker_anchor_sits_at_recency_position 守着（<400）
        assert len(prompt) - prompt.rfind("【当前对话者】") < 400

    def test_self_reference_reminder_at_tail(self):
        """完整 prompt 尾部须有自称提醒：few-shot 主人原话里的第三人称会往回拽。"""
        prompt = build_system_prompt(_fake_full_agent(), sender_name="孙雪瑶")
        assert "【自称提醒】" in prompt
        # 与排版/承诺/故障提醒同处末尾提醒区
        assert prompt.rindex("【自称提醒】") > prompt.rindex("【排版提醒】")
