"""P1-C：防抖「纯数据/不完整」批次识别 + 硬性超时上限 + 监控指标测试。

覆盖：
1. 批次级判定：先发纯数据（人员清单）、再发请求 → 窗口应缩短（不再傻等 60s）；
   旧实现是按单条消息判定，导致请求到达后仍等 60s。
2. 纯数据批次始终无后续请求 → 触发（拉长窗口有效/偏慢）指标正确落账。
3. 纯数据批次在窗口内收到后续请求 → fired_with_request 计数。
4. 硬性超时上限：即便批次已存在很久，delay 也被 cap 到 1s，保证必触发。
5. 普通消息（无结构化数据）不触发拉长窗口。
6. get_debounce_metrics 返回结构正确。
"""
from __future__ import annotations

import threading
import time
import types
from datetime import datetime
from unittest.mock import MagicMock

from main import LinkoraEngine
from src.models import Message


def _make_app(cooldown: int = 5):
    """构造最小可用 LinkoraEngine 裸实例，仅装配 P1-C 相关属性。"""
    app = LinkoraEngine.__new__(LinkoraEngine)
    app._pending_messages = {}
    app._pending_timers = {}
    app._pending_first_seen = {}
    app._pending_incomplete_wait = {}
    app._incomplete_delay_count = 0
    app._incomplete_extra_sec = 0.0
    app._incomplete_fired_with_request = 0
    app._incomplete_fired_without_request = 0
    app._timer_lock = threading.Lock()
    app._metrics_lock = threading.Lock()
    poller_cfg = types.SimpleNamespace(reply_cooldown_seconds=cooldown)
    app.config = types.SimpleNamespace(poller=poller_cfg)
    return app


def _msg(content: str, chat_id: str = "c1", sender_id: str = "u1", msg_id: str = "m1") -> Message:
    return Message(
        msg_id=msg_id,
        chat_id=chat_id,
        chat_type="single",
        chat_name="测试",
        sender_id=sender_id,
        sender_name="张三",
        content=content,
        msg_type="text",
        timestamp=datetime.now(),
    )


def _cancel_timers(app):
    for t in list(app._pending_timers.values()):
        t.cancel()


def test_data_then_request_shortens_window():
    """先纯数据、后请求：第二条到达后窗口应从 60s 缩短到 base+5。"""
    app = _make_app(5)
    key = ("c1", "u1")

    app.handle_message(_msg("孙泰 SX-366 技术服务部", msg_id="m1"))
    assert app._pending_timers[key].interval == 60, "纯数据批次应拉长到 60s"
    assert app._pending_incomplete_wait[key] is True

    app.handle_message(_msg("这三位麻烦开一下CRM号", msg_id="m2"))
    # 请求已到齐 → 不再等 60s，应按 base+5=10s 触发
    assert app._pending_timers[key].interval == 10, "请求到达后窗口应缩短到 base+5"
    # 标记保持 True（用于最终判断拉长是否见效），但 incomplete_pending 已为 False
    assert app._pending_incomplete_wait[key] is True
    _cancel_timers(app)


def test_normal_message_not_extended():
    """普通闲聊消息不应触发拉长窗口。"""
    app = _make_app(5)
    key = ("c1", "u1")
    app.handle_message(_msg("你好", msg_id="m1"))
    assert app._pending_timers[key].interval == 10
    assert app._pending_incomplete_wait.get(key) is None
    _cancel_timers(app)


def test_incomplete_without_followup_records_metric():
    """纯数据批次始终无后续请求 → fired_without_request += 1，delay_count += 1。"""
    app = _make_app(5)
    app.poller = MagicMock()
    app.poller.get_image_path.return_value = ""
    app.store = MagicMock()
    app._handle_message_impl = MagicMock()
    key = ("c1", "u1")

    app.handle_message(_msg("孙泰 SX-366 技术服务部", msg_id="m1"))
    app._process_pending_messages(key)

    assert app._incomplete_delay_count == 1
    assert app._incomplete_fired_without_request == 1
    assert app._incomplete_fired_with_request == 0
    app._handle_message_impl.assert_called_once()
    _cancel_timers(app)


def test_incomplete_with_followup_records_with_request():
    """纯数据批次在窗口内收到后续请求 → fired_with_request += 1。"""
    app = _make_app(5)
    app.poller = MagicMock()
    app.poller.get_image_path.return_value = ""
    app.store = MagicMock()
    app._handle_message_impl = MagicMock()
    key = ("c1", "u1")

    app.handle_message(_msg("孙泰 SX-366 技术服务部", msg_id="m1"))
    app.handle_message(_msg("这三位麻烦开一下CRM号", msg_id="m2"))
    app._process_pending_messages(key)

    assert app._incomplete_delay_count == 1  # 只在首次进入 incomplete 时计数一次
    assert app._incomplete_fired_with_request == 1
    assert app._incomplete_fired_without_request == 0
    # 合并后两条都在，且含请求动词 → 合并消息应被调用一次
    app._handle_message_impl.assert_called_once()
    merged = app._handle_message_impl.call_args.args[0]
    assert "麻烦开一下" in merged.content and "SX-366" in merged.content
    _cancel_timers(app)


def test_hard_cap_ensures_timeout():
    """硬性超时上限：批次已存在 119s，delay 应被 cap 到 1s，保证必触发。

    通过预置一个历史批次（key 已在 _pending_messages 中）来保留 first_seen，
    避免 handle_message 把新批次的起始时间重置为 now。
    """
    app = _make_app(5)
    key = ("c1", "u1")
    app._pending_messages[key] = [_msg("历史消息", msg_id="old")]
    app._pending_first_seen[key] = time.time() - 119  # 模拟批次很久前就开始

    app.handle_message(_msg("孙泰 SX-366 技术服务部", msg_id="m1"))
    # hard_cap = max(10,60)+60 = 120；age≈119 → cap = max(1, 120-119) = 1
    assert app._pending_timers[key].interval == 1.0, "超时上限应把 delay cap 到 1s"
    _cancel_timers(app)


def test_image_with_caption_ocr_preserves_text():
    """P0 回归：图片消息含随图文字(caption)时，OCR 刷新必须保留 caption，
    不能整体替换为 OCR 结果导致用户手打文字丢失（AI 只看到图片内容、看不到指令）。"""
    app = _make_app(5)
    app.poller = MagicMock()
    app.poller.wait_for_ocr.return_value = "工单表：三笔苏州佳世达返厂换新"
    app.poller.get_image_path.return_value = ""
    app.store = MagicMock()
    app._handle_message_impl = MagicMock()
    key = ("c1", "u1")

    caption = "坤哥，2026-07-042378流程需要终止"
    img = _msg(f"{caption}\n[图片识别中...]", msg_id="img1")
    img.msg_type = "image"
    app._pending_messages[key] = [img]

    app._process_pending_messages(key)

    app._handle_message_impl.assert_called_once()
    merged = app._handle_message_impl.call_args.args[0]
    assert caption in merged.content, "随图文字(caption)必须保留"
    assert "工单表" in merged.content, "OCR 内容必须存在"
    assert "[图片识别中...]" not in merged.content, "占位符必须被替换"


def test_get_debounce_metrics_shape():
    """get_debounce_metrics 返回结构正确。"""
    app = _make_app(5)
    app.poller = MagicMock()
    app.poller.get_image_path.return_value = ""
    app.store = MagicMock()
    app._handle_message_impl = MagicMock()
    key = ("c1", "u1")
    app.handle_message(_msg("孙泰 SX-366 技术服务部", msg_id="m1"))
    app._process_pending_messages(key)

    m = app.get_debounce_metrics()
    assert set(m.keys()) == {
        "delay_count", "extra_sec", "fired_with_request",
        "fired_without_request", "pending_batches",
    }
    assert m["delay_count"] == 1
    assert m["fired_without_request"] == 1
    assert isinstance(m["extra_sec"], float)
    _cancel_timers(app)
