"""F12 去吞错回归：回复冷却查询异常必须「保守拦截」，不得因 DB 抖动而重复发送。

背景：冷却查询（`get_last_reply_time`）若把 DB 异常静默吞掉并按「无记录」处理，
会导致本应被冷却拦截的会话被放行 → 重复回复刷屏。修复后异常路径返回 True
（保守跳过回复），且以 warning 暴露存储层故障，便于运维定位。

本测试直接驱动 `RuntimeMixin._reply_cooldown_active`（不经过完整引擎初始化），
用最小 fake self 验证两条关键路径：异常→True、无记录→False。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.platform.runtime import RuntimeMixin


def _make_fake_self(raise_on_read: bool):
    def get_last_reply_time(chat_id):
        if raise_on_read:
            raise RuntimeError("simulated DB failure")
        return None

    fake = SimpleNamespace(
        config=SimpleNamespace(poller=SimpleNamespace(reply_cooldown_seconds=60)),
        store=SimpleNamespace(
            _conversation_repo=SimpleNamespace(get_last_reply_time=get_last_reply_time)
        ),
    )
    # 绑定静态方法，供 self._is_followup_message(...) 调用
    fake._is_followup_message = RuntimeMixin._is_followup_message
    return fake


def _msg(content="普通消息", chat_id="c1", chat_name="测试"):
    return SimpleNamespace(content=content, chat_id=chat_id, chat_name=chat_name)


def test_cooldown_exception_conservative_skip():
    """冷却查询 DB 异常 → 返回 True（保守拦截，不发送），避免重复回复。"""
    fake = _make_fake_self(raise_on_read=True)
    assert RuntimeMixin._reply_cooldown_active(fake, _msg()) is True


def test_cooldown_no_recent_reply_allows():
    """无近期回复记录 → 返回 False（允许回复）。"""
    fake = _make_fake_self(raise_on_read=False)
    assert RuntimeMixin._reply_cooldown_active(fake, _msg()) is False


def test_cooldown_followup_bypass_when_disabled():
    """冷却期内收到明显追问（如"？"）→ 放行（返回 False），不被静默吞掉。"""
    fake = _make_fake_self(raise_on_read=False)
    assert RuntimeMixin._reply_cooldown_active(fake, _msg(content="？")) is False
