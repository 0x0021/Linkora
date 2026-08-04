"""身份约束注入测试：system prompt 在拿到职位时应包含『你就是该领域执行者』提示，
未拿到职位时应降级为『默认假设用户能亲自动手』。

不写死具体岗位，验证：prompt 内容是变量化的，根据 user_title 动态切换。
"""
from types import SimpleNamespace

from src.llm.system_prompt import build_system_prompt_core


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
    # 兜底硬性规则仍在：分身无自我否定边界
    assert "没有『我不负责/这不归我管』的边界" in prompt


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


def test_identity_constraint_has_no_self_denial_boundary():
    """硬性规则：分身没有『我不负责/这不归我管』的边界，技术问题都给方案。"""
    # 有职位
    prompt = build_system_prompt_core(_fake_agent(user_title="IT运维"))
    assert "没有『我不负责/这不归我管』的边界" in prompt
    assert "所有技术问题都按主人能亲自动手处理的方向给方案" in prompt
    # 无职位兜底
    prompt2 = build_system_prompt_core(_fake_agent(user_title=""))
    assert "没有『我不负责/这不归我管』的边界" in prompt2
    assert "所有技术问题都按主人能亲自动手处理的方向给方案" in prompt2