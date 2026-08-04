"""F14 回复发送退避单测。

覆盖两层：
1. 退避原语：连续回复最小间隔护栏（_throttle_reply_send / _reply_send_min_interval）、
   限频异常识别（_is_rate_limit_exception）、限频退避记账（_handle_reply_rate_limited /
   _reply_rate_limited）。
2. 运行时编排：_send_possibly_sharded 接入护栏（finally 更新时间戳）、_send_reply 在
   命中平台限频时正确路由到退避（暂停本轮剩余回复 + 按消息退避）。
"""

from __future__ import annotations

import time
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.im_adapter.errors import IMAdapterRateLimitError, IMAdapterNonRetryableError
from src.platform.runtime import (
    RuntimeMixin,
    REPLY_SEND_MIN_INTERVAL_DEFAULT,
    REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT,
)


class _FakeRuntime(RuntimeMixin):
    """继承 mixin 但跳过引擎初始化的最小宿主，只喂被测方法需要的几个属性。"""

    def __init__(self, *, min_interval=None, rate_backoff=None, dry_run=False):
        self.config = SimpleNamespace(poller=SimpleNamespace(
            reply_send_min_interval=min_interval if min_interval is not None else 0.0,
            reply_send_rate_limit_backoff_seconds=rate_backoff if rate_backoff is not None else 60.0,
            reply_shard_limit=4000,
        ))
        self.dws = MagicMock()
        self.dws.dry_run = dry_run
        self.dws.chat_message_send.side_effect = lambda **kw: {
            "result": {"openTaskId": f"task_{kw.get('uuid')}"}
        }
        self.poller = MagicMock()
        # F14：退避状态（_send_possibly_sharded 现在会访问）
        self._last_reply_send_ts = 0.0
        self._reply_send_throttle_lock = threading.Lock()
        self._reply_rate_limited_until = 0.0
        self._send_backoff_until = {}


@pytest.fixture(autouse=True)
def _no_shard_sleep(monkeypatch):
    """分片间的 0.2s 间隔在单测里没意义，去掉以免拖慢用例。"""
    monkeypatch.setattr("src.platform.runtime_reply_guard.SHARD_SEND_INTERVAL_SECONDS", 0)


# ===================== Layer 1：退避原语 =====================

def test_reply_send_min_interval_from_config():
    assert _FakeRuntime(min_interval=0.7)._reply_send_min_interval() == 0.7


@pytest.mark.parametrize("bad", [0, -1, None, "abc"])
def test_reply_send_min_interval_falls_back_on_invalid(bad):
    assert _FakeRuntime(min_interval=bad)._reply_send_min_interval() == REPLY_SEND_MIN_INTERVAL_DEFAULT


def test_reply_send_rate_limit_backoff_from_config():
    assert _FakeRuntime(rate_backoff=120.0)._reply_send_rate_limit_backoff_seconds() == 120.0


@pytest.mark.parametrize("bad", [0, -1, None, "abc"])
def test_reply_send_rate_limit_backoff_falls_back_on_invalid(bad):
    assert (_FakeRuntime(rate_backoff=bad)._reply_send_rate_limit_backoff_seconds()
            == REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT)


def test_throttle_skips_when_interval_zero(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.platform.runtime.time.sleep", lambda s: sleeps.append(s))
    rt = _FakeRuntime(min_interval=0.0, dry_run=False)
    rt._throttle_reply_send()
    assert sleeps == []


def test_throttle_skips_in_dry_run(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.platform.runtime.time.sleep", lambda s: sleeps.append(s))
    # dry_run=True 即便 interval>0 也不 sleep（无真实发送）
    rt = _FakeRuntime(min_interval=1.0, dry_run=True)
    rt._throttle_reply_send()
    assert sleeps == []


def test_throttle_skips_when_gap_large(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.platform.runtime.time.sleep", lambda s: sleeps.append(s))
    rt = _FakeRuntime(min_interval=1.0, dry_run=False)
    rt._last_reply_send_ts = time.time() - 10.0  # 上次发送在 10s 前
    rt._throttle_reply_send()
    assert sleeps == []


def test_throttle_sleeps_when_gap_small(monkeypatch):
    fake_now = {"t": 1000.0}
    monkeypatch.setattr("src.platform.runtime.time.time", lambda: fake_now["t"])
    sleeps = []
    monkeypatch.setattr("src.platform.runtime.time.sleep", lambda s: sleeps.append(s))
    rt = _FakeRuntime(min_interval=1.0, dry_run=False)
    rt._last_reply_send_ts = 1000.0  # 上次发送就在此刻
    rt._throttle_reply_send()  # gap=0 < 1 → 睡 1.0
    assert sleeps == [1.0]
    # 护栏不更新 _last_reply_send_ts（那是 _mark_reply_sent 的职责），
    # 推进时钟后再次调用按原基准计算
    fake_now["t"] = 1000.5
    rt._throttle_reply_send()  # gap=0.5 < 1 → 睡 0.5
    assert sleeps == [1.0, 0.5]


def test_is_rate_limit_isinstance():
    rt = _FakeRuntime()
    assert rt._is_rate_limit_exception(IMAdapterRateLimitError("429")) is True


def test_is_rate_limit_text_dingtalk():
    # 钉钉把 429 / rate limit exceeded 归类为不可重试错误，需文本兜底识别
    rt = _FakeRuntime()
    assert rt._is_rate_limit_exception(
        IMAdapterNonRetryableError("dws exit 1: rate limit exceeded, slow down")) is True


def test_is_rate_limit_text_429():
    rt = _FakeRuntime()
    assert rt._is_rate_limit_exception(Exception("HTTP 429 Too Many Requests")) is True


def test_is_rate_limit_negative():
    rt = _FakeRuntime()
    assert rt._is_rate_limit_exception(Exception("boom")) is False
    assert rt._is_rate_limit_exception(IMAdapterNonRetryableError("permission denied")) is False


def test_handle_reply_rate_limited_sets_state():
    rt = _FakeRuntime()
    rt._handle_reply_rate_limited("m1", IMAdapterNonRetryableError("rate limit exceeded"))
    assert rt._send_backoff_until.get("m1", 0) > time.time()
    assert rt._reply_rate_limited_until > time.time()
    # 护栏当前生效
    assert rt._reply_rate_limited() is True
    # 过期后失效
    rt._reply_rate_limited_until = time.time() - 1
    assert rt._reply_rate_limited() is False


# ===================== Layer 2：运行时编排 =====================

def test_send_records_timestamp_in_finally(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.platform.runtime.time.sleep", lambda s: sleeps.append(s))
    rt = _FakeRuntime(min_interval=0.0, dry_run=False)
    rt._send_possibly_sharded(chat_id="oc_1", reply_title="标题",
                              filtered="短回复", reply_uuid="u-1", group="oc_1")
    # finally 已更新「上次发送时间戳」
    assert rt._last_reply_send_ts > 0.0
    # min_interval=0 → 护栏不 sleep
    assert sleeps == []


def test_mark_reply_sent_runs_in_finally_on_send_error(monkeypatch):
    rt = _FakeRuntime(min_interval=0.0, dry_run=False)
    rt.dws.chat_message_send.side_effect = RuntimeError("dws boom")
    with pytest.raises(RuntimeError):
        rt._send_possibly_sharded(chat_id="oc_1", reply_title="标题",
                                  filtered="短回复", reply_uuid="u-1", group="oc_1")
    # 即便发送抛错，finally 仍更新时间戳
    assert rt._last_reply_send_ts > 0.0


class _SendReplyFake(RuntimeMixin):
    """驱动真实 _send_reply 的最小宿主（群消息路径，绕过 store 查询）。

    只 stub 掉 _send_reply 在分发前依赖的几个方法，其余走 mixin 真实实现，
    以验证「命中限频 → 退避记账」的端到端路由。
    """

    def __init__(self):
        self.config = SimpleNamespace(poller=SimpleNamespace(
            reply_send_min_interval=0.0,
            reply_send_rate_limit_backoff_seconds=60.0,
            reply_shard_limit=4000,
        ))
        self.dws = MagicMock()
        self.dws.dry_run = False
        self.dws.chat_message_send.side_effect = IMAdapterNonRetryableError(
            "dws exit 1: rate limit exceeded")
        self.poller = MagicMock()
        self._last_reply_send_ts = 0.0
        self._reply_send_throttle_lock = threading.Lock()
        self._reply_rate_limited_until = 0.0
        self._send_backoff_until = {}

    def _reply_cooldown_active(self, message):
        return False

    def _filter_sensitive_words(self, text):
        return text

    def _prepare_outgoing_text(self, filtered, message):
        return ("标题", filtered)

    def _mark_read_before_reply(self, message):
        pass


def test_send_reply_routes_rate_limit_to_backoff():
    rt = _SendReplyFake()
    msg = SimpleNamespace(msg_id="m1", raw={}, chat_type="group", chat_id="gid",
                         chat_name="g", sender_id="", timestamp=None, content="hi")
    sent = rt._send_reply(msg, "你好")
    # 命中限频：本轮不发送成功，返回 False
    assert sent is False
    # 平台级限频护栏已置位（暂停本轮剩余回复）
    assert rt._reply_rate_limited_until > time.time()
    # 按消息退避窗口已置位（下轮再试）
    assert rt._send_backoff_until.get("m1", 0) > time.time()


def test_send_reply_generic_error_still_returns_false_without_rate_guard():
    rt = _SendReplyFake()
    rt.dws.chat_message_send.side_effect = RuntimeError("dws transient boom")
    msg = SimpleNamespace(msg_id="m2", raw={}, chat_type="group", chat_id="gid",
                         chat_name="g", sender_id="", timestamp=None, content="hi")
    sent = rt._send_reply(msg, "你好")
    assert sent is False
    # 普通瞬时错误走原有 SEND_RETRY_BACKOFF_SECONDS 路径，不触发平台级限频护栏
    assert rt._reply_rate_limited_until == 0.0
    assert rt._send_backoff_until.get("m2", 0) > time.time()
