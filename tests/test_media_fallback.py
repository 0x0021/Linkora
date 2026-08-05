"""P2-G：语音/视频消息处理行为测试（当前策略：彻底跳过，不自动回复）。

覆盖：
1. 配置默认：PollerConfig.skip_msg_types 含 voice/video/feedCard；
   graceful_fallback_msg_types=[]（媒体回退机制暂不启用，留作可配置能力）；
   SafetyConfig.media_fallback_text 仍保留。
2. poller._effective_skip_types 现含 voice/video（graceful 为空不再剔除）。
3. 鲁棒性：即便 skip_msg_types 含 voice/video，只要 graceful 也含，仍会被减去送达（防回归）。
4. main._handle_message_impl 对 voice/video 静默跳过、不回复；
   对 text 走正常规则/LLM 路径。
"""
from __future__ import annotations

import types
from datetime import datetime
from unittest.mock import MagicMock

import threading

from main import LinkoraEngine
from src.config import OaApprovalConfig, PollerConfig, SafetyConfig
from src.models import Message
from src.poller import MessagePoller


def _make_bot(fallback_text: str = "请发文字"):
    """构造最小 LinkoraEngine 裸实例，仅装配 P2-G 相关属性。"""
    bot = LinkoraEngine.__new__(LinkoraEngine)
    bot.config = types.SimpleNamespace(
        poller=types.SimpleNamespace(
            reply_cooldown_seconds=0,
            skip_msg_types=["system", "app", "oa", "file", "call",
                            "read_receipt", "calendar", "schedule",
                            "voice", "video", "feedCard"],
            skip_notification_patterns=[],
            skip_notification_sender_ids=[],
            graceful_fallback_msg_types=[],
            reply_concurrency_timeout_seconds=30,
        ),
        safety=types.SimpleNamespace(media_fallback_text=fallback_text),
        oa_approval=OaApprovalConfig(),
    )
    bot._replying_lock = threading.Lock()
    bot._replying_chats = {}  # dict[chat_id -> 持锁令牌]（见 runtime_lifecycle 初始化）
    bot.tracker = MagicMock()
    bot.rule_engine = MagicMock()
    bot.store = MagicMock()
    bot._active_ctx.reply_semaphore = threading.Semaphore(1)
    bot.store._conversation_repo.get_last_reply_time.return_value = None
    bot.llm_agent = MagicMock()
    bot._bg_throttle = MagicMock()
    bot._backoff_cleanup_counter = 0  # 对应 main.py __init__ 中的初始化（测试用 __new__ 绕过）
    bot._send_reply = MagicMock()
    return bot


def _msg(msg_type: str = "text", content: str = "hi", msg_id: str = "m1") -> Message:
    return Message(
        msg_id=msg_id,
        chat_id="c1",
        chat_type="single",
        chat_name="测试会话",
        sender_id="u1",
        sender_name="张三",
        content=content,
        msg_type=msg_type,
        timestamp=datetime(2026, 7, 13, 10, 0, 0),
        raw={},
    )


def test_config_defaults():
    cfg = PollerConfig()
    assert "voice" in cfg.skip_msg_types
    assert "video" in cfg.skip_msg_types
    assert "feedCard" in cfg.skip_msg_types
    assert cfg.graceful_fallback_msg_types == []
    assert SafetyConfig().media_fallback_text


def test_effective_skip_includes_media():
    cfg = PollerConfig()
    poller = MessagePoller.__new__(MessagePoller)
    poller.config = cfg
    eff = poller._effective_skip_types()
    assert "voice" in eff
    assert "video" in eff
    assert "system" in eff
    assert "file" in eff


def test_effective_skip_subtracts_graceful_even_if_in_skip():
    # 鲁棒性：若某人重新启用 graceful 回退（skip 与 graceful 同时含某类型），
    # 该类型应被减去并送达，而非静默跳过。防止配置逻辑回归。
    cfg = PollerConfig(skip_msg_types=["system", "voice", "video"],
                       graceful_fallback_msg_types=["voice", "video"])
    poller = MessagePoller.__new__(MessagePoller)
    poller.config = cfg
    eff = poller._effective_skip_types()
    assert "voice" not in eff and "video" not in eff
    assert "system" in eff


def test_voice_is_skipped_no_reply():
    bot = _make_bot()
    bot._handle_message_impl(_msg(msg_type="voice"))
    bot._send_reply.assert_not_called()


def test_video_is_skipped_no_reply():
    bot = _make_bot()
    bot._handle_message_impl(_msg(msg_type="video"))
    bot._send_reply.assert_not_called()


def test_text_not_fallbacked():
    bot = _make_bot()
    bot._has_replied_after = MagicMock(return_value=False)
    bot.rule_engine.check.return_value = types.SimpleNamespace(
        action="skip", intent="business", reason="x", reply_text=None)
    bot._handle_message_impl(_msg(msg_type="text"))
    bot._send_reply.assert_not_called()
