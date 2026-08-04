"""
H5/H6 历史分级注入（_apply_history_tiering）降级回归测试。

背景：原 _apply_history_tiering 把 max_recent 硬编码为 6，且因 history_window(5/6/6)
<= 6 导致「摘要分支」是死代码（agent.py:575-597 的 else 永不触发）。H5/H6 把
max_recent 经配置暴露（history_tiering_recent，默认 6 向后兼容），并接入 H2-A 缓存
读路径。本测试保证：

1. max_recent 改自配置（默认 6 = 向后兼容），降级逻辑正确：
   - len(history) <= max_recent → 原样返回（不触发摘要）；
   - len(history) > max_recent 且无缓存 → 降级为仅 recent（older 被丢弃，安全不失忆）；
   - len(history) > max_recent 且 older 不足 summary_min_older → 降级为仅 recent。
2. 注入条数与阈值严格一致（防止 H5/H6 把「近期完整条数」改错导致失忆/越界）。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import LlmConfig
from src.llm.agent import LLMAgent
from src.models import Message


def _msg(content: str, idx: int) -> Message:
    return Message(
        msg_id=f"m{idx}",
        chat_id="chat_tiering",
        chat_type="single",
        chat_name=None,
        sender_id="u1",
        sender_name="小明",
        content=content,
        msg_type="text",
        timestamp=datetime(2024, 1, 1, 10, 0, 0) + timedelta(minutes=idx),
        role="user",
    )


def _make_agent(**adv_overrides):
    adv = LlmConfig().advanced
    for k, v in adv_overrides.items():
        setattr(adv, k, v)
    agent = LLMAgent(
        config=LlmConfig(advanced=adv),
        client=None,
        tool_router=None,
        store=None,  # 无 store：_read_cached_summary 返回 None → 走降级路径
    )
    return agent


class TestHistoryTieringDegradation:
    """降级路径（无缓存）：max_recent 控制注入条数，older 被安全丢弃。"""

    def test_default_max_recent_is_6_backward_compat(self):
        """未配置 history_tiering_recent 时默认 6（向后兼容原硬编码行为）。"""
        a = _make_agent()
        assert a._history_tiering_recent == 6

    def test_no_tiering_when_under_max_recent(self):
        """len(history) <= max_recent → 原样返回所有消息，不触发摘要/降级。"""
        a = _make_agent(history_tiering_recent=4)
        history = [_msg(f"消息{i}", i) for i in range(4)]  # 4 条，等于 max_recent
        out = a._apply_history_tiering(history)
        assert out == history, "未超阈值时应原样返回，不丢历史"

    def test_exact_boundary_included(self):
        """刚好 max_recent 条时仍原样返回（边界不触发摘要）。"""
        a = _make_agent(history_tiering_recent=4)
        history = [_msg(f"消息{i}", i) for i in range(4)]
        out = a._apply_history_tiering(history)
        assert len(out) == 4

    def test_degrade_to_recent_only_when_over_max_recent(self):
        """len(history) > max_recent 且无缓存 → 仅返回最近 max_recent 条（recent）。"""
        a = _make_agent(history_tiering_recent=4)
        history = [_msg(f"消息{i}", i) for i in range(10)]  # 10 条 > 4
        out = a._apply_history_tiering(history)
        # 降级：只注入最近 4 条（older 的 6 条被安全丢弃，不调 LLM、不崩溃）
        assert len(out) == 4
        assert out == history[-4:]

    def test_degrade_keeps_most_recent_when_older_insufficient(self):
        """len(history) > max_recent，但 older 段不足 summary_min_older(2) → 仍仅 recent。"""
        a = _make_agent(history_tiering_recent=4, summary_min_older=2)
        history = [_msg(f"消息{i}", i) for i in range(5)]  # 5 条：recent=4, older=1 < 2
        out = a._apply_history_tiering(history)
        assert len(out) == 4
        assert out == history[-4:]

    def test_configured_max_recent_respected(self):
        """history_tiering_recent 配置值严格生效（不写死 magic number）。"""
        for recent in (3, 4, 5):
            a = _make_agent(history_tiering_recent=recent)
            history = [_msg(f"消息{i}", i) for i in range(recent + 3)]
            out = a._apply_history_tiering(history)
            assert len(out) == recent, f"max_recent={recent} 应注入 {recent} 条，实际 {len(out)}"

    def test_store_none_safe_degradation(self):
        """store=None 时绝不应抛异常（主回复链路不受摘要模块影响）。"""
        a = _make_agent(history_tiering_recent=4)
        history = [_msg(f"消息{i}", i) for i in range(8)]
        # 不应抛 AttributeError / TypeError（此前死代码里 Message 的 created_at 字段是无效签名）
        out = a._apply_history_tiering(history)
        assert len(out) == 4
