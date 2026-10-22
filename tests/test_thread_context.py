"""话题(Thread)上下文装配的回归测试。

背景（2026-10-22）：P0-1 让 Linkora 能**回复**进钉钉话题，但 LLM 的历史来自
``get_conversation_history(chat_id)``（主群流）——用户在话题下讨论的内容不进
主群流，AI 在话题里回复时看不到「大家在聊什么」。

``src/platform/thread_context.py`` 负责按 ``thread_id`` 拉取话题回复并入历史。
关键约束：
- 拉取失败 / 平台不支持 → 降级为「无话题上下文」，绝不阻断主回复；
- 话题上下文**不落库**（仅当轮生效），否则会重复计入主群流历史、污染后续轮次；
- 同一条消息可能既在主群流又在话题里 → 按 msg_id 去重。
"""
from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, "src")

from src.im_adapter.errors import IMAdapterError
from src.models import Message
from src.platform.thread_context import (
    MAX_THREAD_CONTEXT_MESSAGES,
    fetch_thread_context,
    merge_thread_context,
)


def _msg(mid: str, sender: str, content: str, ts: datetime, role: str = "user") -> Message:
    return Message(
        msg_id=mid, chat_id="cid_g", chat_type="group", chat_name="群",
        sender_id="", sender_name=sender, content=content, msg_type="text",
        timestamp=ts, raw={}, role=role,
    )


class _FakeAdapter:
    """记录调用参数；可配置返回内容或抛异常。"""

    def __init__(self, replies=None, fail=False):
        self._replies = replies if replies is not None else []
        self._fail = fail
        self.calls: list[dict] = []

    def chat_thread_replies(self, *, thread_id="", message_id="", group="",
                            limit=50, page_all=False):
        self.calls.append({
            "thread_id": thread_id, "message_id": message_id, "group": group,
            "limit": limit, "page_all": page_all,
        })
        if self._fail:
            raise IMAdapterError("dws exit 1: thread replies failed")
        return self._replies


# ============ 1. 拉取 ============

def test_no_thread_id_skips_call():
    """无 thread_id → 不发起任何调用（普通群消息走原路径，不额外开销）。"""
    a = _FakeAdapter([{"text": "x"}])
    m = _msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))
    assert fetch_thread_context(a, m) == []
    assert a.calls == []


def test_fetch_passes_thread_and_group():
    """透传 thread_id 与 group（群 id 用于话题定位）。"""
    a = _FakeAdapter([{"message_id": "om_2", "sender": "乙", "text": "你好",
                        "timestamp": "2026-10-22 10:05:00"}])
    m = _msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))
    m.thread_id = "ctc_abc"
    out = fetch_thread_context(a, m)
    assert a.calls[0]["thread_id"] == "ctc_abc"
    assert a.calls[0]["group"] == "cid_g"
    assert a.calls[0]["page_all"] is True
    assert len(out) == 1
    assert out[0].content == "你好"
    assert out[0].sender_name == "乙"
    assert out[0].thread_id == "ctc_abc"
    assert out[0].role == "user"


def test_failure_degrades_to_empty():
    """拉取失败 → 空列表（降级，不抛）。"""
    a = _FakeAdapter(fail=True)
    m = _msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))
    m.thread_id = "ctc_abc"
    assert fetch_thread_context(a, m) == []


def test_adapter_without_capability_returns_empty():
    """适配器无 chat_thread_replies（飞书 / 企微）→ 空列表，不抛 AttributeError。"""
    class _NoCap:
        pass

    m = _msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))
    m.thread_id = "ctc_abc"
    assert fetch_thread_context(_NoCap(), m) == []


def test_empty_text_filtered():
    """空文本的话题消息被过滤（不注入无信息条目）。"""
    a = _FakeAdapter([
        {"message_id": "om_2", "sender": "乙", "text": "   "},
        {"message_id": "om_3", "sender": "丙", "text": "有效"},
    ])
    m = _msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))
    m.thread_id = "ctc_abc"
    out = fetch_thread_context(a, m)
    assert len(out) == 1
    assert out[0].content == "有效"


def test_sorted_ascending_by_time():
    """话题消息按时间正序（旧→新），与主群流历史的 ASC 契约一致。"""
    a = _FakeAdapter([
        {"message_id": "om_3", "sender": "丙", "text": "第三条",
         "timestamp": "2026-10-22 10:10:00"},
        {"message_id": "om_2", "sender": "乙", "text": "第二条",
         "timestamp": "2026-10-22 10:05:00"},
        {"message_id": "om_1", "sender": "甲", "text": "第一条",
         "timestamp": "2026-10-22 10:00:00"},
    ])
    m = _msg("om_x", "丁", "触发", datetime(2026, 10, 22, 10, 12))
    m.thread_id = "ctc_abc"
    out = fetch_thread_context(a, m)
    assert [o.content for o in out] == ["第一条", "第二条", "第三条"]


def test_limit_caps_injected_count():
    """话题消息条数受上限约束（防止超长话题撑爆 LLM 上下文）。"""
    a = _FakeAdapter([
        {"message_id": f"om_{i}", "sender": "甲", "text": f"第{i}条",
         "timestamp": f"2026-10-22 10:{i:02d}:00"}
        for i in range(50)
    ])
    m = _msg("om_x", "丁", "触发", datetime(2026, 10, 22, 11, 0))
    m.thread_id = "ctc_abc"
    out = fetch_thread_context(a, m, limit=5)
    assert len(out) == 5
    assert a.calls[0]["limit"] == 5


def test_bad_timestamp_does_not_crash():
    """时间字段异常不崩（回退当前时间）。"""
    a = _FakeAdapter([{"message_id": "om_2", "sender": "乙", "text": "你好",
                        "timestamp": "不是时间"}])
    m = _msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))
    m.thread_id = "ctc_abc"
    out = fetch_thread_context(a, m)
    assert len(out) == 1
    assert isinstance(out[0].timestamp, datetime)


# ============ 2. 合并 ============

def test_merge_dedups_by_msg_id():
    """同一条消息既在主群流又在话题里 → 只保留一份。"""
    hist = [_msg("om_2", "乙", "重复内容", datetime(2026, 10, 22, 10, 5))]
    thread = [_msg("om_2", "乙", "重复内容", datetime(2026, 10, 22, 10, 5)),
              _msg("om_3", "丙", "新内容", datetime(2026, 10, 22, 10, 6))]
    merged = merge_thread_context(hist, thread)
    assert len(merged) == 2
    assert [m.content for m in merged] == ["重复内容", "新内容"]


def test_merge_sorted_ascending():
    """合并后按时间正序（旧→新）。"""
    hist = [_msg("om_5", "戊", "主群流较晚", datetime(2026, 10, 22, 10, 10))]
    thread = [_msg("om_3", "丙", "话题较早", datetime(2026, 10, 22, 10, 2))]
    merged = merge_thread_context(hist, thread)
    assert [m.content for m in merged] == ["话题较早", "主群流较晚"]


def test_merge_empty_thread_returns_history_unchanged():
    """无话题上下文 → 历史原样返回。"""
    hist = [_msg("om_1", "甲", "内容", datetime(2026, 10, 22, 10, 0))]
    assert merge_thread_context(hist, []) == hist


def test_merge_empty_history_returns_thread():
    """主群流无历史 → 直接用话题上下文。"""
    thread = [_msg("om_2", "乙", "话题内容", datetime(2026, 10, 22, 10, 2))]
    merged = merge_thread_context([], thread)
    assert len(merged) == 1
    assert merged[0].content == "话题内容"


def test_merge_dedups_empty_msg_id_by_content():
    """msg_id 为空的话题消息按（发言人+内容+时间）粗略去重。"""
    hist = [_msg("om_1", "甲", "同一条", datetime(2026, 10, 22, 10, 0))]
    thread = [_msg("", "甲", "同一条", datetime(2026, 10, 22, 10, 0))]
    merged = merge_thread_context(hist, thread)
    assert len(merged) == 1


def test_default_limit_constant():
    """默认注入上限为常量（防止无上限拉取）。"""
    assert MAX_THREAD_CONTEXT_MESSAGES == 20
