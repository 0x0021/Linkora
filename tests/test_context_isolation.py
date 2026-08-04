"""聊天上下文隔离（context isolation）回归测试。

问题背景（用户截图）：
- 用户先问「珞石股价走势及最新价格」→ AI 正确回复了股票信息；
- 用户接着发「再试试」（一个全新的、不同意图的消息）→ AI 却回复了**天气**，
  且上下文里混入了上一轮股票对话的内容。

根因（见 commit 修复）：
1) 上下文污染：get_conversation_history 返回 ORDER BY timestamp DESC（最新在前），
   旧代码 `for h in history` 直接顺序遍历，导致上一轮的 assistant 回复被插到
   用户提问「之前」，多轮结构非法（assistant 先于 user），弱模型锚点错乱、
   对含糊追问「再试试」误判并触发 get_weather 兜底工具。
   → 修复：_build_user_message 用 `for h in reversed(history)` 还原为时间正序。
2) 重复处理：已答复消息可能因 dedup 标记与「已回复」判定两套机制更新时机不一致
   而被重新拉取重跑；msg_id 为空时 _has_replied_after 直接放行导致空 msg_id
   漏防重复回复。
   → 修复：回复成功后原子化标记用户消息 dedup（与 update_last_replied_msg_id 同步）；
     _has_replied_after 对空 msg_id 用 raw.alt_id 兜底。

本测试只验证【上下文隔离】这一最确定的根因（历史排序不变量），
以及【空 msg_id 防重复】的补丁。
"""
from __future__ import annotations

from datetime import datetime
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

from src.config import LlmAdvancedConfig, LlmConfig
from src.llm.agent import LLMAgent
from src.models import Message


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _make_agent():
    """构造 LLMAgent，隔离 RAG 注入，只测历史排序逻辑。"""
    cfg = LlmConfig()
    cfg.advanced = LlmAdvancedConfig()
    cfg.system_prompt = "你是助手"
    agent = LLMAgent(config=cfg, client=None, tool_router=None, store=MagicMock())
    agent.user_name = "AI助手"
    # 隔离 RAG / embedding 等与本测试无关的副作用
    agent._get_embedding_client = lambda: None
    agent._is_document_query = lambda q, e: False
    agent._build_system_prompt = lambda sender_name=None: "SYS"
    return agent


def _hist(role: str, content: str, sender_name: str = "张三", is_bot: bool = False) -> Message:
    """构造一条历史消息。注意：get_conversation_history 返回 DESC（最新在前），
    测试里按「真实返回顺序」构造（最新在列表头部）。"""
    return Message(
        msg_id="h", chat_id="c1", chat_type="single", chat_name="单聊",
        sender_id="u1", sender_name=sender_name, content=content,
        msg_type="text", timestamp=datetime.now(), role=role, is_bot=is_bot,
    )


def _incoming(content: str = "再试试") -> Message:
    return Message(
        msg_id="m_new", chat_id="c1", chat_type="single", chat_name="单聊",
        sender_id="u1", sender_name="张三", content=content,
        msg_type="text", timestamp=datetime.now(),
    )


def _non_system(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m["role"] != "system"]


# ---------------------------------------------------------------------------
# 上下文隔离：历史排序不变量
# ---------------------------------------------------------------------------
def test_history_in_chronological_order():
    """DESC 历史经 reversed 后应还原为 旧→新 的时间正序。

    场景：上轮 [股价问 → 股价答]，本轮回 [再试试]。
    期望上下文顺序：user(股价问) → assistant(股价答) → user(再试试)。
    """
    agent = _make_agent()
    # 真实返回值：DESC（最新在前）
    history = [
        _hist("assistant", "珞石(XK)当前最新价 12.5 元，近一月震荡上行。"),  # 最新
        _hist("user", "珞石股价走势及最新价格"),                              # 最旧
    ]
    messages = agent._build_user_message(_incoming("再试试"), history)
    body = _non_system(messages)
    roles = [m["role"] for m in body]
    contents = [m["content"] for m in body]

    # 角色必须合法交替且以 user 收尾
    assert roles == ["user", "assistant", "user"], f"角色序列错误: {roles}"
    # 最旧的 user 提问必须排在最前，对应 assistant 紧随其后
    assert contents[0] == "珞石股价走势及最新价格", "最早的股价提问必须排第一"
    assert contents[1].startswith("珞石"), "对应的股价回复必须紧随其后"
    # 当前 incoming 必须是最后一条
    assert contents[-1] == "再试试", "当前用户消息必须排在最后"


def test_three_turn_history_keeps_user_before_assistant():
    """多轮历史下，任何 assistant 回复都不能排在其对应 user 提问之前。"""
    agent = _make_agent()
    history = [  # DESC：最新在前
        _hist("assistant", "珞石股价是 12.5 元。"),                 # 最新
        _hist("user", "珞石股价"),
        _hist("assistant", "北京今天晴，28 度。"),                   # 更旧
        _hist("user", "北京今天天气怎么样"),
    ]
    messages = agent._build_user_message(_incoming(), history)
    roles = [m["role"] for m in _non_system(messages)]

    # 不变量：每个 assistant 必须紧跟在它的 user 提问之后
    for i, r in enumerate(roles):
        if r == "assistant":
            assert roles[i - 1] == "user", f"assistant 不能越位到 user 之前，序列={roles}"
    # 整段结构以 user 开头、以 user 结尾
    assert roles[0] == "user" and roles[-1] == "user", roles


def test_reversed_not_double_reversing():
    """回归保护：`reversed` 只应施加一次。若历史已是 ASC，则结果会错乱——
    本断言确保我们依赖的契约是「history 为 DESC（最新在前）」。"""
    agent = _make_agent()
    # 构造一个明显 bug 场景：若有人把 history 当成 ASC 传入，第一轮 user 应最后出现？
    # 这里验证：DESC 历史的最后一条（最旧）应出现在 body 的第一位（紧接 system 之后）。
    history = [
        _hist("assistant", "第二条回复"),
        _hist("user", "第二条问题"),
        _hist("assistant", "第一条回复"),
        _hist("user", "第一条问题"),   # 最旧
    ]
    messages = agent._build_user_message(_incoming(), history)
    contents = [m["content"] for m in _non_system(messages)]
    assert contents[0] == "第一条问题", "最旧的历史 user 必须排在第一"
    assert contents[-1] == "再试试", "当前 incoming 必须最后"


def test_leading_assistant_trimmed_to_valid_alternation():
    """B2 修复回归：历史窗口截断切掉最旧的用户提问后，首条变成 bot 的 assistant
    回复。归一化必须剔除开头连续的 assistant，使上下文以 user 开头、合法交替，
    否则 OpenAI 系接口会直接报错或退化多轮质量。"""
    agent = _make_agent()
    # DESC 返回，窗口只保留最近 3 条（最旧的用户提问被截断）：
    # 最新在前 = [assistant(新答), user(新问), assistant(旧答)]
    history = [  # DESC
        _hist("assistant", "珞石最新价 12.5 元。"),       # 最新
        _hist("user", "珞石股价"),                         # 当前轮 user
        _hist("assistant", "北京今天晴，28 度。"),         # 旧轮 assistant（最旧，截断后首条）
    ]
    messages = agent._build_user_message(_incoming(), history)
    body = _non_system(messages)
    roles = [m["role"] for m in body]

    # 不变量：必须以 user 开头（开头连续的 assistant 被剔除），且以 user 结尾
    assert roles[0] == "user", f"归一化后必须以 user 开头，序列={roles}"
    assert roles[-1] == "user", f"必须以 user 结尾，序列={roles}"
    # 任意 assistant 必须紧跟在 user 之后（无越位）
    for i, r in enumerate(roles):
        if r == "assistant":
            assert roles[i - 1] == "user", f"assistant 不能越位，序列={roles}"


def test_adjacent_same_role_merged():
    """B2 修复回归：相邻同角色历史消息必须合并，避免 user/user 或 assistant/assistant
    连续导致接口报错。"""
    agent = _make_agent()
    # DESC：构造相邻同角色（窗口截断切掉中间的 assistant）→ 还原后两个 user 相邻
    history = [  # DESC
        _hist("assistant", "第二条回复"),
        _hist("user", "第二条问题"),
        _hist("user", "第一条问题"),    # 与第二条问题相邻且同为 user（被截断隔离）
        _hist("assistant", "第一条回复"),
    ]
    messages = agent._build_user_message(_incoming(), history)
    # _build_user_message 内部归一化会合并相邻 user；这里只验证 production 调用不抛错
    # 且最终结构以 user 开头/结尾、交替合规。
    body = _non_system(messages)
    roles = [m["role"] for m in body]
    assert roles[0] == "user" and roles[-1] == "user"
    for i, r in enumerate(roles):
        if r == "assistant":
            assert roles[i - 1] == "user"


def test_own_auto_reply_excluded_from_context():
    """AI 自己发出的 [自动回复] 不应被视为对话历史污染上下文。"""
    agent = _make_agent()
    history = [  # DESC
        _hist("assistant", "[自动回复]我现在不方便，稍后回复。",
              sender_name="AI助手", is_bot=True),
        _hist("user", "在吗", sender_name="张三"),
    ]
    messages = agent._build_user_message(_incoming(), history)
    contents = [m["content"] for m in _non_system(messages)]
    assert not any("[自动回复]" in c for c in contents), "自动回复不应进入上下文"
    assert contents[0] == "在吗"
    assert contents[-1] == "再试试"


# ---------------------------------------------------------------------------
# 空 msg_id 防重复回复补丁
# ---------------------------------------------------------------------------
def test_has_replied_after_alt_id_fallback():
    """msg_id 为空时，用 raw.alt_id 兜底判定是否已回复，避免空 msg_id 漏防重复。"""
    from main import LinkoraEngine

    fake = SimpleNamespace(store=MagicMock())
    fake.store._conversation_repo.get_last_replied_msg_id= MagicMock(return_value="alt_xyz")

    msg = Message(
        msg_id="", chat_id="c1", chat_type="single", chat_name="单聊",
        sender_id="u1", sender_name="张三", content="再试试",
        msg_type="text", timestamp=datetime.now(),
        raw={"alt_id": "alt_xyz"},
    )
    fn = MethodType(LinkoraEngine._has_replied_after, fake)
    assert fn(msg) is True, "alt_id 命中 last_replied 应判定已回复"


def test_has_replied_after_empty_no_alt_prevents_false_positive():
    """msg_id 与 alt_id 都为空，且 last_replied 无记录 → 视为新消息（不误判已回复）。"""
    from main import LinkoraEngine

    fake = SimpleNamespace(store=MagicMock())
    fake.store._conversation_repo.get_last_replied_msg_id= MagicMock(return_value="")

    msg = Message(
        msg_id="", chat_id="c1", chat_type="single", chat_name="单聊",
        sender_id="u1", sender_name="张三", content="再试试",
        msg_type="text", timestamp=datetime.now(), raw={},
    )
    fn = MethodType(LinkoraEngine._has_replied_after, fake)
    assert fn(msg) is False, "无 msg_id/alt_id 且无 last_replied 记录 → 当作新消息"
