"""承诺边界测试：分身不得替主人承接任务或作出承诺（2026-08-28 事故回归）。

事故：对方问「供应商只给优化建议、不直接改站，这种方式行不行？你能操作不？」
分身回「我这边按文档去操作」——替主人接下了归属未定的活。

根因不是缺少拦截，而是【角色定位】里那条绝对规则把两件事混为一谈：
  ① 回答事实/给方案 —— 确实不该推诿；
  ② 替主人承接任务/作出承诺 —— 分身无权决定。
旧规则「没有『我不负责/这不归我管』的边界」治好了①，却把②彻底放开。

本文件锁死泛化后的行为：求答案照答，要承诺不接。
"""
from types import SimpleNamespace

from src.llm.system_prompt import (
    build_system_prompt,
    build_system_prompt_core,
)


def _fake_agent(user_title: str = "运维工程师", user_name: str = "主人",
                user_dept: str = "信息技术部", org_name: str = "某公司",
                platform_id: str = "dingtalk") -> SimpleNamespace:
    cfg = SimpleNamespace(
        system_prompt="你是助手。",
        advanced=SimpleNamespace(max_chars_daily_chat=200, max_chars_tech_issue=600),
    )
    return SimpleNamespace(
        config=cfg, user_name=user_name, user_dept=user_dept, org_name=org_name,
        user_title=user_title, platform_id=platform_id,
    )


def test_commitment_boundary_present_in_core():
    """【承诺边界】无条件常驻（与 RAG / 工具开关无关）。"""
    prompt = build_system_prompt_core(_fake_agent())
    assert "【承诺边界" in prompt
    assert "不得替主人答应" in prompt


def test_commitment_boundary_distinguishes_answer_vs_commitment():
    """核心：教会模型区分「求答案」与「要承诺」，两者必须同时出现。"""
    prompt = build_system_prompt_core(_fake_agent())
    assert "求答案" in prompt
    assert "要承诺" in prompt


def test_commitment_boundary_forbids_taking_on_work():
    """明确点名禁止替主人接活的表态（不是泛泛而谈，给反例模型才 obey）。"""
    prompt = build_system_prompt_core(_fake_agent())
    assert "严禁出现「我来操作」" in prompt
    # 必须给出「交回本人」的正确做法，而不只是禁止
    assert "交回本人" in prompt
    assert "跟本人确认" in prompt


def test_commitment_boundary_does_not_cause_over_refusal():
    """防回归：加了限制不能退回「什么都推去确认」的甩锅病。

    【角色定位】整段存在的意义就是治甩锅，承诺边界必须与它共存而非覆盖它。
    """
    prompt = build_system_prompt_core(_fake_agent())
    assert "不削弱你回答问题的能力" in prompt
    assert "直接答，不受本条限制" in prompt


def test_commitment_boundary_has_illegality_floor():
    """任何违法违规请求，不因对方身份或语气而答应。"""
    prompt = build_system_prompt_core(_fake_agent())
    assert "违法违规" in prompt
    assert "不答应、不协助、不出主意" in prompt


def test_absolute_no_boundary_rule_removed():
    """回归：旧的绝对规则必须消失，否则与【承诺边界】自相矛盾，模型会择一执行。"""
    for title in ("运维工程师", "财务经理", ""):
        prompt = build_system_prompt_core(_fake_agent(user_title=title))
        assert "没有『我不负责/这不归我管』的边界" not in prompt, title
        assert "所有技术问题都按主人能亲自动手处理的方向给方案" not in prompt, title
        # 但回答层面的「不推诿」仍在
        assert "回答边界" in prompt, title


def test_commitment_boundary_reminder_survives_few_shot_and_style():
    """近因效应：few-shot 是「主人原话」，本人常是「行，我来做」的接活口吻，
    会把模型往回拽——故完整 prompt 末尾必须在 few-shot 之后再压一次提醒。"""
    agent = _fake_agent()
    agent.few_shot_examples = [
        {"user": "这个你来弄一下？", "assistant": "好的，我来操作。"},
    ]
    agent._get_style_prompt = lambda: "说话直接。"
    prompt = build_system_prompt(agent)

    assert "【承诺边界提醒】" in prompt
    # 提醒必须位于 few-shot 之后（近因位），否则被主人原话盖过去
    assert prompt.index("【承诺边界提醒】") > prompt.index("样例(主人原话风格参考)")


def test_commitment_boundary_present_without_few_shot_and_style():
    """最小路径：即便 few-shot / 风格 / 技能段全关，承诺边界仍在。"""
    prompt = build_system_prompt(
        _fake_agent(), include_few_shot=False, include_style=False, include_skills=False
    )
    assert "【承诺边界 - 最高优先级】" in prompt
    assert "【承诺边界提醒】" in prompt
