"""上下文话题边界测试：时间标注 + 断层提示。

背景（2026-08 线上问题）：
    仅有「时间窗口」（3 天 history_days / 6 小时 session_gap）只能挡住远古消息，
    挡不住「窗口内但属于另一件事」的串味——上午聊 A 事、下午聊 B 事，间隔没超
    6 小时就全糊在一起，模型连每句话是几点说的都看不到，于是把上一件事没办完的
    待办（索要工号手机号）带进新话题。

修复思路（按用户要求，不写话题分类正则）：
    只把**客观时间事实**暴露给 LLM——每条历史带时间标记、断层处插自然语言分隔
    提示、当前消息距上一条太久时再提示一次——话题边界由模型自己判断。

本文件覆盖：
1. ``src.llm.timeline`` 纯函数（时间标签 / 时长口语化 / 断层阈值）
2. prompt_builder 把标签与分隔提示正确织入 messages
3. 断层两侧禁止合并
4. 时间戳缺失/异常时静默降级，不炸主回复
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.prompt_builder import PromptBuilder, _normalize_history_asc, _sanitize_rag_query
from src.llm.system_prompt import build_system_prompt_core
from src.llm.timeline import (
    DEFAULT_TOPIC_GAP_MINUTES,
    format_time_label,
    gap_notice,
    humanize_gap,
    incoming_gap_notice,
)

_NOW = datetime(2026, 8, 10, 15, 0)


class _Msg:
    def __init__(self, sender, role, content, ts=None):
        self.sender_name = sender
        self.role = role
        self.content = content
        self.timestamp = ts


class _Agent:
    user_name = "主人"
    platform_id = "dingtalk"
    user_dept = "研发部"
    user_title = "工程师"
    org_name = "公司"
    _max_input_tokens = 99999
    _rag_auto_inject = False
    _rag_intent_only = False
    config = SimpleNamespace(
        system_prompt="你是{user_name}的{platform}数字分身。",
        advanced=SimpleNamespace(
            max_chars_daily_chat=200,
            max_chars_tech_issue=100,
        ),
    )

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
        lambda **kwargs: (kwargs.get("system_content"), SimpleNamespace(
            best_score=None, injected=False, intent_ok=False,
            citations=[], rag_block=None, relevant_knowledge="")),
    )
    monkeypatch.setattr(
        pbmod, "wrap_incoming_message",
        lambda message, truncate_fn=None: message.content,
    )


def _systems(messages):
    return [m["content"] for m in messages if m["role"] == "system"]


def _users(messages):
    """历史段的 user 消息（排除末尾当前消息）。"""
    return [m["content"] for m in messages[1:-1] if m["role"] == "user"]


# ---------------------------------------------------------------- 纯函数


class TestTimelineHelpers:
    def test_time_label_relative_days(self):
        assert format_time_label(datetime(2026, 8, 10, 9, 12), _NOW) == "今天 09:12"
        assert format_time_label(datetime(2026, 8, 9, 9, 12), _NOW) == "昨天 09:12"
        assert format_time_label(datetime(2026, 8, 8, 18, 5), _NOW) == "前天 18:05"
        assert format_time_label(datetime(2026, 7, 8, 16, 40), _NOW) == "07-08 16:40"

    def test_time_label_bad_input_returns_empty(self):
        """脏时间戳不得抛异常——标注是锦上添花，不能拖垮主回复。"""
        for bad in (None, "2026-08-10", 12345, object()):
            assert format_time_label(bad, _NOW) == ""

    def test_humanize_gap(self):
        assert humanize_gap(12 * 60) == "12 分钟"
        assert humanize_gap(3 * 3600) == "3 小时"
        assert humanize_gap(3 * 3600 + 20 * 60) == "3 小时 20 分钟"
        assert humanize_gap(2 * 86400 + 5 * 3600) == "2 天 5 小时"

    def test_gap_notice_below_threshold_is_none(self):
        """阈值内属同一段连续对话，不插提示（避免噪音）。"""
        later = _NOW + timedelta(minutes=DEFAULT_TOPIC_GAP_MINUTES - 1)
        assert gap_notice(_NOW, later) is None

    def test_gap_notice_above_threshold(self):
        notice = gap_notice(_NOW, _NOW + timedelta(hours=3))
        assert notice is not None
        assert "3 小时" in notice
        assert "换了一件事" in notice

    def test_incoming_gap_notice_wording(self):
        """当前消息断层提示必须点明『别再索要上文信息』——正是事故复现点。"""
        notice = incoming_gap_notice(_NOW, _NOW + timedelta(hours=5))
        assert notice is not None
        assert "5 小时" in notice
        assert "同一件事" in notice
        assert "索要" in notice

    def test_gap_notice_tolerates_bad_timestamps(self):
        assert gap_notice(None, _NOW) is None
        assert gap_notice(_NOW, None) is None
        assert incoming_gap_notice("x", _NOW) is None

    def test_threshold_zero_disables(self):
        """阈值 0 = 关闭标注（向后兼容/应急开关）。"""
        far = _NOW + timedelta(days=3)
        assert gap_notice(_NOW, far, threshold_minutes=0) is None
        assert incoming_gap_notice(_NOW, far, threshold_minutes=0) is None


# ---------------------------------------------------------------- 织入上下文


class TestPromptTimeline:
    def test_history_user_messages_carry_time_label(self, monkeypatch):
        """每条历史 user 消息都带 [时间] 发言人： 前缀。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())

        base = datetime.now().replace(hour=9, minute=12, second=0, microsecond=0)
        history = [
            _Msg("小雅", "user", "VPN 连不上了", base),
            _Msg("小雅", "user", "好了谢谢", base + timedelta(minutes=3)),
        ]
        incoming = _Msg("小雅", "user", "在吗", base + timedelta(minutes=5))

        messages = pb.build_user_message(incoming, history)
        contents = _users(messages)

        assert contents, messages
        joined = "\n".join(contents)
        assert "[今天 09:12] 小雅：" in joined, joined

    def test_gap_inserts_separator_and_blocks_merge(self, monkeypatch):
        """超阈值断层：插 system 分隔提示，且断层两侧不得合并成一条。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())

        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        history = [
            _Msg("小雅", "user", "VPN 连不上了", base),
            # 同一个人、同一 role，但隔了 3 小时 —— 换了一件事
            _Msg("小雅", "user", "打印机怎么加", base + timedelta(hours=3)),
        ]
        incoming = _Msg("小雅", "user", "嗯", base + timedelta(hours=3, minutes=1))

        messages = pb.build_user_message(incoming, history)
        systems = _systems(messages)
        contents = _users(messages)

        assert any("换了一件事" in s for s in systems), systems
        # 关键：同人同 role 也不能跨断层合并，否则两件事又糊到一起
        assert len(contents) == 2, contents
        assert not any("VPN" in c and "打印机" in c for c in contents), contents

    def test_no_gap_no_separator(self, monkeypatch):
        """阈值内的连续对话不插提示——避免每轮都堆无用 system 噪音。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())

        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        history = [
            _Msg("小雅", "user", "VPN 连不上了", base),
            _Msg("小雅", "user", "还是不行", base + timedelta(minutes=2)),
        ]
        incoming = _Msg("小雅", "user", "怎么办", base + timedelta(minutes=3))

        messages = pb.build_user_message(incoming, history)
        systems = _systems(messages)

        assert not any("换了一件事" in s for s in systems), systems
        assert not any("距上一条消息" in s for s in systems), systems

    def test_incoming_gap_notice_injected_before_guard(self, monkeypatch):
        """当前消息距最后一条历史过久 -> 在 user 消息前插提示（近因位）。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())

        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        history = [
            _Msg("小雅", "user", "改完了。老数据不用挪吧", base),
            _Msg("主人", "user", "老数据先不用了", base + timedelta(minutes=1)),
        ]
        # 隔 5 小时后对方换了个事来问
        incoming = _Msg("小雅", "user", "打印机怎么加", base + timedelta(hours=5))

        messages = pb.build_user_message(incoming, history)
        systems = _systems(messages)

        assert any("距上一条消息" in s for s in systems), systems
        # 位置：必须在最后一条 user（当前消息）之前
        idx_notice = next(
            i for i, m in enumerate(messages)
            if m["role"] == "system" and "距上一条消息" in m["content"]
        )
        assert idx_notice < len(messages) - 1

    def test_missing_timestamps_degrade_silently(self, monkeypatch):
        """历史无 timestamp（老数据/鸭子类型桩）时不炸，也不产生标签。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())

        history = [
            _Msg("小雅", "user", "帮我看下报表", None),
            _Msg("王五", "user", "好的", None),
        ]
        incoming = _Msg("小雅", "user", "在吗", None)

        messages = pb.build_user_message(incoming, history)
        contents = _users(messages)

        # 发言人前缀仍在（不受时间缺失影响），但没有 [xx:xx] 标签
        assert any(c.startswith("小雅：") for c in contents), contents
        assert not any(c.startswith("[") for c in contents), contents

    def test_speaker_prefix_still_present_with_label(self, monkeypatch):
        """时间标签不得挤掉发言人姓名——两者必须共存。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())

        base = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)
        history = [
            _Msg("小雅", "user", "A 事", base),
            _Msg("王五", "user", "B 事", base + timedelta(minutes=1)),
        ]
        incoming = _Msg("小雅", "user", "在吗", base + timedelta(minutes=2))

        messages = pb.build_user_message(incoming, history)
        contents = _users(messages)

        assert any("小雅：" in c and c.startswith("[") for c in contents), contents
        assert any("王五：" in c and c.startswith("[") for c in contents), contents
        # 不同人依旧不合并
        assert not any("小雅：" in c and "王五：" in c for c in contents), contents


# ---------------------------------------------------------------- 提示词


def test_system_prompt_has_topic_boundary_guidance():
    """system prompt 须用自然语言告知「先判断是不是同一件事」。"""
    agent = MagicMock()
    agent.user_name = "主人"
    agent.platform_id = "dingtalk"
    agent.user_dept = "研发部"
    agent.user_title = "工程师"
    agent.org_name = "公司"
    agent.config.system_prompt = "你是{user_name}的{platform}数字分身。"
    agent.config.advanced = SimpleNamespace(
        max_chars_daily_chat=200, max_chars_tech_issue=100)

    prompt = build_system_prompt_core(agent, sender_name="小雅")

    assert "同一件事" in prompt
    assert "时间标记" in prompt
    # 明确禁止把旧话题的待索取信息带进新话题
    assert "工号" in prompt
    # 标注本身不得被模型复述
    assert "不得出现或模仿" in prompt


# ---------------------------------------------------------------- 历史顺序（DESC→ASC 归一化）


class _TieringAgent(_Agent):
    """复刻真实 _apply_history_tiering 的 ASC 切片语义，用于验证「DESC 输入也被归正」。"""

    _history_tiering_recent = 2

    def _apply_history_tiering(self, history):
        if len(history) <= self._history_tiering_recent:
            return history
        return history[-self._history_tiering_recent:]


class TestHistoryOrdering:
    def test_normalize_helper_sorts_asc(self):
        """_normalize_history_asc 把 DESC 拍平成时间正序（旧→新）。"""
        base = datetime(2026, 8, 10, 9, 0)
        desc = [
            _Msg("小雅", "user", "新", base + timedelta(hours=2)),
            _Msg("小雅", "user", "中", base + timedelta(hours=1)),
            _Msg("小雅", "user", "旧", base),
        ]
        asc = _normalize_history_asc(desc)
        ts = [m.timestamp for m in asc]
        assert ts == sorted(ts), ts

    def test_normalize_tolerates_missing_timestamps(self):
        """时间戳缺失时安静退回原顺序，不排序也不炸。"""
        history = [
            _Msg("小雅", "user", "A", None),
            _Msg("小雅", "user", "B", datetime(2026, 8, 10, 9, 0)),
        ]
        assert _normalize_history_asc(history) is history

    def test_desc_input_produces_asc_output(self, monkeypatch):
        """真实返回是 DESC：build_user_message 必须把历史按时间正序呈现，
        且最老那条仍在（证明没在 tiering 阶段被当成『最新』丢掉）。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_Agent())
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        history = [  # DESC（新→旧），模拟 get_conversation_history
            _Msg("小雅", "user", "老消息C", base + timedelta(hours=2)),
            _Msg("小雅", "user", "中消息B", base + timedelta(hours=1)),
            _Msg("小雅", "user", "新消息A", base),
        ]
        incoming = _Msg("小雅", "user", "在吗", base + timedelta(hours=3))
        messages = pb.build_user_message(incoming, history)
        contents = _users(messages)
        # 时间正序：旧（新消息A）→ 新（老消息C）
        assert "新消息A" in contents[0], contents
        assert "老消息C" in contents[-1], contents

    def test_desc_input_tiering_keeps_newest(self, monkeypatch):
        """归一化后 tiering 切片保留『最新的 N 条』，而非 DESC 下的『最老的 N 条』。
        这正是 2026-08-10 发现的『旧话题串进最新提示词』根因。
        时间戳：t9(9:00, 最老) < t10(10:00) < t11(11:00, 最新)。"""
        _patch(monkeypatch)
        pb = PromptBuilder(_TieringAgent())
        base = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        history = [  # DESC（新→旧），模拟 get_conversation_history
            _Msg("小雅", "user", "t11", base + timedelta(hours=2)),
            _Msg("小雅", "user", "t10", base + timedelta(hours=1)),
            _Msg("小雅", "user", "t9", base),
        ]
        incoming = _Msg("小雅", "user", "在吗", base + timedelta(hours=3))
        messages = pb.build_user_message(incoming, history)
        contents = _users(messages)
        # 归一化 ASC=[t9,t10,t11] → tiering 保留最新的 2 条(t10,t11)，最老的 t9 被丢弃
        assert any("t10" in c for c in contents)
        assert any("t11" in c for c in contents)
        assert not any("t9" in c for c in contents)

    def test_sanitize_rag_backtracks_to_most_recent(self):
        """RAG query 回溯必须取『最近一条』历史用户消息（依赖 ASC 历史）。"""
        history_asc = [
            SimpleNamespace(role="user", content="很老的无关问题"),
            SimpleNamespace(role="user", content="原始问题：VPN 怎么连"),
        ]
        out = _sanitize_rag_query("你为什么没查", history_asc)
        assert out == "原始问题：VPN 怎么连", out
