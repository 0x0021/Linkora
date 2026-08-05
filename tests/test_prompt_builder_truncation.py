"""prompt_builder 截断逻辑的回归测试。

核心关注点：历史超长触发 token 截断时，重建的 messages 必须保留
【最终约束】guard（近因约束），否则弱模型失去护栏会泄漏/机械回复。
这是 2026-07-28 修复的 pre-existing bug。
"""


from src.llm.prompt_builder import PromptBuilder


class _FakeRag:
    best_score = None
    injected = False
    intent_ok = False
    citations = []
    relevant_knowledge = ""


class _Msg:
    def __init__(self, sender, role, content):
        self.sender_name = sender
        self.role = role
        self.content = content


class _Agent:
    user_name = "OWNER"
    # 故意设极小，强制触发历史截断 + 砍主 system 两条路径
    _max_input_tokens = 30
    _rag_auto_inject = False
    _rag_intent_only = False

    def _build_system_prompt(self, **kw):
        return "system prompt body"

    def _apply_history_tiering(self, history):
        return history

    def _truncate_long_message(self, c):
        return c


def _patch(monkeypatch):
    import src.llm.prompt_builder as pbmod

    monkeypatch.setattr(
        pbmod, "inject_rag_knowledge",
        lambda **kwargs: (kwargs.get("system_content"), _FakeRag()),
    )
    # 隔离外部消息包装，避免依赖其具体实现
    monkeypatch.setattr(
        pbmod, "wrap_incoming_message",
        lambda message, truncate_fn=None: message.content,
    )


def test_truncation_preserves_final_guard(monkeypatch):
    """超长历史触发截断时，重建 messages 必须保留【最终约束】guard。"""
    _patch(monkeypatch)
    agent = _Agent()
    pb = PromptBuilder(agent)

    # 每条都远超 token 预算，强制触发截断
    history = [_Msg("张三", "user", "x" * 200) for _ in range(15)]
    incoming = _Msg("张三", "user", "hello")

    messages = pb.build_user_message(incoming, history)

    guard_present = any(
        m.get("role") == "system" and "【最终约束】" in (m.get("content") or "")
        for m in messages
    )
    assert guard_present, "截断后 messages 必须保留【最终约束】guard（pre-existing bug 已修复）"

    roles = [m["role"] for m in messages]
    assert "user" in roles and roles[-1] == "user"
    # guard 是 user 之前的独立 system 消息
    assert roles.index("user") > 0


def test_no_truncation_also_has_guard(monkeypatch):
    """正常不触发截断时，guard 本就存在（基线，防止回归引入遗漏）。"""
    _patch(monkeypatch)
    agent = _Agent()
    agent._max_input_tokens = 100000  # 不触发截断
    pb = PromptBuilder(agent)

    history = [_Msg("张三", "user", "短消息") for _ in range(3)]
    incoming = _Msg("张三", "user", "hello")

    messages = pb.build_user_message(incoming, history)

    guard_present = any(
        m.get("role") == "system" and "【最终约束】" in (m.get("content") or "")
        for m in messages
    )
    assert guard_present, "不截断时也应保留【最终约束】guard"


def test_truncation_preserves_system_trims_user(monkeypatch):
    """历史砍光后仍超阈值：必须保主 system 完整，改砍超长的用户消息（风险#2 修复）。

    旧逻辑按比例是砍主 system_content，会腰斩关键约束；新逻辑保护主 system，
    只截断 incoming（用户贴的大段文本），guard 同样保留。
    """
    _patch(monkeypatch)
    agent = _Agent()
    # 设适中预算：system(~7)+guard(~90) 占 97，余 ~103 给 user
    agent._max_input_tokens = 200
    pb = PromptBuilder(agent)

    history = []  # 无历史，直接构造 system+guard+超长user 超阈值场景
    incoming = _Msg("张三", "user", "长" * 500)  # 中文 ~750 token，远超预算

    messages = pb.build_user_message(incoming, history)

    # 1) 主 system 必须完整（核心修复点：不再被比例腰斩）
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "system prompt body", "主 system 不应被截断"

    # 2) guard 完整保留
    guard_present = any(
        m.get("role") == "system" and "【最终约束】" in (m.get("content") or "")
        for m in messages
    )
    assert guard_present, "guard 必须保留"

    # 3) 用户消息被裁剪（不再超出预算），且确实短于原始
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert user_msgs, "应有 user 消息"
    trimmed = user_msgs[-1]["content"]
    assert len(trimmed) < 500, "超长用户消息应被裁剪"
    # 裁剪后 token 应接近预算（留容差，因估算为线性近似）
    assert pb.estimate_tokens(trimmed) <= 103 + 8, f"裁剪后 user 不应超预算，实得 {pb.estimate_tokens(trimmed)}"


def test_truncation_system_only_overflow_warns(monkeypatch):
    """极端：即便砍光 user 仍超（主 system 本身极长），降级砍 system 并告警，guard 仍保留。"""
    _patch(monkeypatch)
    agent = _Agent()
    agent._max_input_tokens = 30  # 极小预算，system+guard 本身就近 97，必触发降级
    pb = PromptBuilder(agent)

    history = []
    incoming = _Msg("张三", "user", "短")  # user 极短，超阈值全因 system+guard

    messages = pb.build_user_message(incoming, history)

    guard_present = any(
        m.get("role") == "system" and "【最终约束】" in (m.get("content") or "")
        for m in messages
    )
    assert guard_present, "降级砍 system 时 guard 仍必须保留"
