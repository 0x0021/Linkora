"""src/models.py 核心数据类测试。

覆盖 Message 的字段默认值、边界值、序列化/反序列化（dataclass asdict / 重建）。
"""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import datetime


from src.models import Message


# ============ 字段默认值 ============

def test_defaults():
    """未显式传入的可选字段应使用默认值。"""
    ts = datetime(2026, 7, 7, 12, 0, 0)
    msg = Message(
        msg_id="m1",
        chat_id="c1",
        chat_type="single",
        chat_name=None,
        sender_id="s1",
        sender_name="张三",
        content="你好",
        msg_type="text",
        timestamp=ts,
    )
    assert msg.raw == {}
    assert msg.role == ""
    assert msg.image_path == ""
    assert msg.is_bot is False


def test_all_field_names():
    """字段集合与预期一致，防止意外增删字段破坏兼容性。"""
    names = {f.name for f in fields(Message)}
    assert names == {
        "msg_id", "chat_id", "chat_type", "chat_name", "sender_id",
        "sender_name", "content", "msg_type", "timestamp", "raw",
        "role", "image_path", "is_bot", "is_withdrawn", "is_archived",
    }


# ============ 边界值 ============

def test_empty_strings():
    """空字符串内容/名称应被正常接受。"""
    msg = Message(
        msg_id="",
        chat_id="",
        chat_type="",
        chat_name="",
        sender_id="",
        sender_name="",
        content="",
        msg_type="",
        timestamp=datetime(2026, 1, 1),
    )
    assert msg.content == ""
    assert msg.chat_name == ""


def test_is_bot_true():
    """is_bot 可显式置 True。"""
    msg = Message(
        msg_id="m",
        chat_id="c",
        chat_type="group",
        chat_name="群",
        sender_id="s",
        sender_name="机器人",
        content="回复",
        msg_type="text",
        timestamp=datetime(2026, 1, 1),
        is_bot=True,
    )
    assert msg.is_bot is True


def test_optional_chat_name_none():
    """chat_name 允许为 None（系统消息/未知场景）。"""
    msg = Message(
        msg_id="m",
        chat_id="c",
        chat_type="single",
        chat_name=None,
        sender_id="s",
        sender_name="x",
        content="y",
        msg_type="text",
        timestamp=datetime(2026, 1, 1),
    )
    assert msg.chat_name is None


# ============ 序列化 / 反序列化 ============

def test_asdict_roundtrip():
    """asdict 后按字段重建，得到等价对象。"""
    ts = datetime(2026, 7, 7, 9, 30, 15)
    raw = {"event": "message", "source": "dingtalk"}
    msg = Message(
        msg_id="m1",
        chat_id="c1",
        chat_type="group",
        chat_name="技术群",
        sender_id="s1",
        sender_name="李四",
        content="在吗",
        msg_type="text",
        timestamp=ts,
        raw=raw,
        role="user",
        image_path="data/tmp_images/x.png",
        is_bot=False,
    )
    d = asdict(msg)
    assert isinstance(d, dict)
    assert d["msg_id"] == "m1"
    assert d["timestamp"] == ts
    assert d["raw"] == raw

    # 用 asdict 结果重建
    rebuilt = Message(**d)
    assert rebuilt == msg


def test_equality():
    """相同字段构造的两个实例应相等。"""
    ts = datetime(2026, 7, 7, 12, 0, 0)
    a = Message("m", "c", "single", None, "s", "n", "x", "text", ts)
    b = Message("m", "c", "single", None, "s", "n", "x", "text", ts)
    assert a == b
    assert a != Message("m2", "c", "single", None, "s", "n", "x", "text", ts)


def test_raw_mutable_default_isolation():
    """raw 默认工厂应每次返回独立 dict，避免跨实例共享可变状态。"""
    m1 = Message("m1", "c", "single", None, "s", "n", "x", "text", datetime(2026, 1, 1))
    m2 = Message("m2", "c", "single", None, "s", "n", "x", "text", datetime(2026, 1, 1))
    m1.raw["k"] = "v"
    assert "k" not in m2.raw
