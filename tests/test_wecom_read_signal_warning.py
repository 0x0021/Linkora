"""T-A4: 企微已读闸门静默失效 → 启动期显式告警。

企微 CLI 无已读回执能力（mark_read 为基类空操作，chat_message_list_unread_conversations
仅返回近期活跃会话近似替代），导致依赖对方「已读」信号的 suppress_when_owner_read（已读
闸门）与 mark_read_after_process（处理后标记已读）在企微静默失效。本测试验证：当企微平台
且开关开启时，产生一条 [wecom] WARNING；开关关闭时不产生；钉钉/飞书原生支持已读回执，
不存在该告警方法。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from src.dws_adapter import DwsAdapter
from src.im_adapter.feishu import FeishuCliAdapter
from src.im_adapter.wecom import WecomCliAdapter


def _poller(suppress: bool = False, mark_read: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        suppress_when_owner_read=suppress,
        mark_read_after_process=mark_read,
    )


def test_wecom_warns_when_suppress_owner_read_enabled(caplog):
    """企微 + suppress_when_owner_read 开启 → 产生 [wecom] WARNING。"""
    adapter = WecomCliAdapter()
    with caplog.at_level(logging.WARNING, logger="src.im_adapter.wecom"):
        adapter.warn_read_signal_unsupported(_poller(suppress=True))
    assert any("[wecom]" in r.message for r in caplog.records)


def test_wecom_warns_when_mark_read_enabled(caplog):
    """企微 + mark_read_after_process 开启 → 产生 [wecom] WARNING。"""
    adapter = WecomCliAdapter()
    with caplog.at_level(logging.WARNING, logger="src.im_adapter.wecom"):
        adapter.warn_read_signal_unsupported(_poller(mark_read=True))
    assert any("[wecom]" in r.message for r in caplog.records)


def test_wecom_no_warning_when_both_disabled(caplog):
    """企微 + 两个开关都关闭 → 不产生 [wecom] WARNING。"""
    adapter = WecomCliAdapter()
    with caplog.at_level(logging.WARNING, logger="src.im_adapter.wecom"):
        adapter.warn_read_signal_unsupported(_poller(suppress=False, mark_read=False))
    assert not any("[wecom]" in r.message for r in caplog.records)


def test_dingtalk_feishu_do_not_warn():
    """钉钉/飞书原生支持已读回执，不存在该告警方法（仅企微静默失效）。"""
    assert not hasattr(DwsAdapter, "warn_read_signal_unsupported")
    assert not hasattr(FeishuCliAdapter, "warn_read_signal_unsupported")
