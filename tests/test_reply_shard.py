"""F15 超长回复分片单测。

覆盖三层：
1. 纯函数 shard_reply_text 的边界（不超限 / 刚好超限 / 语义切点 / 代码围栏 / 标记）
2. RuntimeMixin._reply_shard_limit 的配置读取与回落
3. RuntimeMixin._send_possibly_sharded 的发送编排（单片行为不变、多片 uuid/标题/去重）
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
import threading

import pytest

from src.platform.reply_shard import REPLY_SHARD_LIMIT_DEFAULT, shard_reply_text


# ---------------------------------------------------------------- 纯函数层

def test_under_limit_returns_original_untouched():
    """未超限时必须原样返回，且绝不加「（1/1）」标记——保护绝大多数正常回复。"""
    text = "这是一条正常长度的回复。"
    assert shard_reply_text(text, 1000) == [text]


def test_exactly_at_limit_not_sharded():
    text = "a" * 100
    assert shard_reply_text(text, 100) == [text]


def test_one_char_over_limit_is_sharded():
    text = "a" * 101
    shards = shard_reply_text(text, 100)
    assert len(shards) >= 2
    assert all(len(s) <= 100 for s in shards)


def test_empty_text_returns_single_empty():
    assert shard_reply_text("", 100) == [""]


def test_every_shard_within_limit():
    text = "".join(f"第{i}行内容，用于填充长度测试。\n" for i in range(400))
    limit = 800
    shards = shard_reply_text(text, limit)
    assert len(shards) > 1
    for s in shards:
        assert len(s) <= limit, f"分片超限: {len(s)} > {limit}"


def test_continuation_markers_are_sequential():
    text = "段落内容。\n\n" * 300
    shards = shard_reply_text(text, 600)
    total = len(shards)
    assert total >= 3
    for i, s in enumerate(shards, 1):
        assert s.startswith(f"（{i}/{total}）\n\n"), f"第 {i} 片标记不对: {s[:20]!r}"


def test_marker_disabled():
    text = "内容。\n\n" * 300
    shards = shard_reply_text(text, 600, marker=False)
    assert len(shards) > 1
    assert not any(s.startswith("（") for s in shards)


def test_no_content_loss():
    """分片后去掉标记，正文字符（忽略空白）必须与原文一致——绝不能丢内容。"""
    text = "".join(f"句子{i}，内容内容内容。" for i in range(500))
    shards = shard_reply_text(text, 700)
    joined = "".join(s.split("\n\n", 1)[1] for s in shards)
    strip = str.maketrans("", "", " \n\t")
    assert joined.translate(strip) == text.translate(strip)


def test_prefers_paragraph_break():
    """有空行可断时，不应把段落从中间劈开。"""
    para = "这是一个完整的段落，内容大约五十个字符左右用于测试断点选择逻辑。"
    text = "\n\n".join([para] * 30)
    shards = shard_reply_text(text, 400, marker=False)
    assert len(shards) > 1
    # 每片都应以完整段落结尾（末尾是句号）
    for s in shards:
        assert s.rstrip().endswith("。"), f"切点不在段落边界: ...{s[-20:]!r}"


def test_code_fence_is_balanced_across_shards():
    """代码围栏被切开时，前片自动收尾、后片自动补开头，每片可独立渲染。"""
    code = "\n".join(f"line_{i} = compute(i)  # 填充长度用的注释内容" for i in range(120))
    text = f"下面是代码：\n\n```python\n{code}\n```\n\n以上。"
    shards = shard_reply_text(text, 900, marker=False)
    assert len(shards) > 1
    for s in shards:
        assert s.count("```") % 2 == 0, f"围栏不成对:\n{s[:120]}"
    # 后续片应带回语言标识
    assert any(s.lstrip().startswith("```python") for s in shards[1:])


def test_default_limit_is_conservative():
    assert REPLY_SHARD_LIMIT_DEFAULT == 4000


# ------------------------------------------------------- Runtime 编排层

from src.platform.runtime import RuntimeMixin  # noqa: E402


class _FakeRuntime(RuntimeMixin):
    """继承 mixin 但跳过引擎初始化的最小宿主，只喂被测方法需要的几个属性。"""

    def __init__(self, *, limit=None, dry_run=False):  # noqa: D107  (不调用 super)
        self.config = SimpleNamespace(
            poller=SimpleNamespace(reply_shard_limit=limit) if limit is not None
            else SimpleNamespace()
        )
        self.dws = MagicMock()
        self.dws.dry_run = dry_run
        self.dws.chat_message_send.side_effect = lambda **kw: {
            "result": {"openTaskId": f"task_{kw.get('uuid')}"}
        }
        self.poller = MagicMock()
        # F14：初始化退避状态（_send_possibly_sharded 现在会访问），并关掉退避 sleep
        # 避免影响 F15 既有断言与用例速度（dry_run=False 时也会进入护栏逻辑）。
        self._last_reply_send_ts = 0.0
        self._reply_send_throttle_lock = threading.Lock()
        self._reply_rate_limited_until = 0.0
        self._reply_send_min_interval = lambda: 0.0

    def limit(self):
        return self._reply_shard_limit()

    def send(self, **kw):
        return self._send_possibly_sharded(**kw)


@pytest.fixture(autouse=True)
def _no_shard_sleep(monkeypatch):
    """分片间的 0.2s 间隔在单测里没意义，去掉以免拖慢用例。"""
    monkeypatch.setattr("src.platform.runtime_reply_guard.SHARD_SEND_INTERVAL_SECONDS", 0)


def test_shard_limit_from_config():
    assert _FakeRuntime(limit=1234).limit() == 1234


@pytest.mark.parametrize("bad", [0, -1, None, "abc"])
def test_shard_limit_falls_back_on_invalid(bad):
    assert _FakeRuntime(limit=bad).limit() == REPLY_SHARD_LIMIT_DEFAULT


def test_single_shard_call_is_unchanged():
    """未超限时必须是一次调用、原文本、原 uuid、带 title —— 与分片前完全一致。"""
    rt = _FakeRuntime(limit=1000)
    rt.send(chat_id="oc_1", reply_title="标题", filtered="短回复", reply_uuid="u-1",
            group="oc_1")
    rt.dws.chat_message_send.assert_called_once_with(
        title="标题", text="短回复", uuid="u-1", group="oc_1",
    )
    rt.poller._mark_msg_processed.assert_not_called()


def test_multi_shard_uuid_title_and_target():
    """多片：uuid 必须各不相同（否则平台按幂等键丢弃续片），标题只跟首片，目标不变。"""
    rt = _FakeRuntime(limit=300)
    long_text = "内容内容内容内容内容。\n\n" * 100
    rt.send(chat_id="oc_1", reply_title="标题", filtered=long_text, reply_uuid="u-1",
            open_dingtalk_id="ou_peer")

    calls = rt.dws.chat_message_send.call_args_list
    assert len(calls) > 1
    uuids = [c.kwargs["uuid"] for c in calls]
    assert uuids[0] == "u-1"
    assert len(set(uuids)) == len(uuids), f"续片 uuid 重复: {uuids}"
    assert calls[0].kwargs["title"] == "标题"
    assert all(c.kwargs["title"] == "" for c in calls[1:])
    assert all(c.kwargs["open_dingtalk_id"] == "ou_peer" for c in calls)
    assert all(len(c.kwargs["text"]) <= 300 for c in calls)


def test_multi_shard_marks_intermediate_but_not_last():
    """非最后片就地标记去重（防轮询回捞自己的分片）；最后一片留给 _record_reply_success。"""
    rt = _FakeRuntime(limit=300)
    long_text = "内容内容内容内容内容。\n\n" * 100
    result = rt.send(chat_id="oc_1", reply_title="T", filtered=long_text,
                     reply_uuid="u-1", group="oc_1")

    n = rt.dws.chat_message_send.call_count
    assert rt.poller._mark_msg_processed.call_count == n - 1
    # 返回值是最后一片的 DWS 结果
    last_uuid = rt.dws.chat_message_send.call_args_list[-1].kwargs["uuid"]
    assert result == {"result": {"openTaskId": f"task_{last_uuid}"}}


def test_dry_run_skips_shard_dedup_marking():
    rt = _FakeRuntime(limit=300, dry_run=True)
    rt.send(chat_id="oc_1", reply_title="T", filtered="内容。\n\n" * 200,
            reply_uuid="u-1", group="oc_1")
    assert rt.dws.chat_message_send.call_count > 1
    rt.poller._mark_msg_processed.assert_not_called()


def test_shard_dedup_marking_failure_does_not_abort_send():
    """标记去重失败只告警，剩余分片必须继续发完——否则用户只收到半截回复。"""
    rt = _FakeRuntime(limit=300)
    rt.poller._mark_msg_processed.side_effect = RuntimeError("db locked")
    long_text = "内容内容内容内容内容。\n\n" * 100
    expected = len(shard_reply_text(long_text, 300))
    rt.send(chat_id="oc_1", reply_title="T", filtered=long_text,
            reply_uuid="u-1", group="oc_1")
    assert rt.dws.chat_message_send.call_count == expected
