"""reaction_resolution 纯函数单测。"""
from __future__ import annotations

from datetime import datetime

from src.llm.reaction_resolution import (
    build_resolution_note,
    collect_owner_resolved_msg_ids,
    is_completion_emoji,
)


def test_is_completion_emoji():
    assert is_completion_emoji("Done")
    assert is_completion_emoji("done")          # 大小写归一化
    assert is_completion_emoji("✅")
    assert is_completion_emoji("OK")
    assert is_completion_emoji("完成")
    # 模糊语义不应算完成，避免把礼貌性回应误判为已办
    assert not is_completion_emoji("赞")
    assert not is_completion_emoji("感谢")
    assert not is_completion_emoji("鼓掌")
    assert not is_completion_emoji("")
    assert not is_completion_emoji(None)


def test_collect_owner_resolved_by_name():
    reactions = {
        "m1": [{"emoji": "Done", "sender_id": "x", "sender_name": "张三"}],
        "m2": [{"emoji": "赞", "sender_id": "x", "sender_name": "张三"}],
    }
    assert collect_owner_resolved_msg_ids(reactions, owner_name="张三") == {"m1"}


def test_collect_owner_resolved_by_id():
    reactions = {
        "m1": [{"emoji": "Done", "sender_id": "u123", "sender_name": "某人"}],
    }
    assert collect_owner_resolved_msg_ids(
        reactions, owner_ids={"u123"}, owner_name="张三") == {"m1"}


def test_collect_skips_non_owner_reaction():
    # 第三人回 Done 不应标主人已办（群聊防误判）
    reactions = {
        "m1": [{"emoji": "Done", "sender_id": "other", "sender_name": "同事"}],
    }
    assert collect_owner_resolved_msg_ids(
        reactions, owner_ids={"u123"}, owner_name="张三") == set()


def test_collect_skips_non_completion_emoji():
    reactions = {
        "m1": [{"emoji": "赞", "sender_id": "u123", "sender_name": "张三"}],
    }
    assert collect_owner_resolved_msg_ids(
        reactions, owner_ids={"u123"}, owner_name="张三") == set()


def test_build_resolution_note_empty():
    assert build_resolution_note(set(), []) == ""


def test_build_resolution_note_content():
    class _M:
        def __init__(self, mid, ts, sender, content):
            self.msg_id = mid
            self.timestamp = ts
            self.sender_name = sender
            self.content = content

    msgs = [_M("m1", datetime(2026, 11, 3, 11, 6), "若汐", "帮我加导出权限～谢谢")]
    note = build_resolution_note({"m1"}, msgs)
    assert "已办" in note
    assert "若汐" in note
    assert "导出权限" in note
    # 列出具体消息，便于 LLM 对应
    assert "[11:06]" in note
