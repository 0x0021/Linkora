"""企微 send / reply 收敛后的等价性测试（P1-5 续）。

``chat_message_send`` 与 ``chat_message_reply`` 此前各写了一份**完全相同**的
payload 构造（含 ``content[:2048]`` 截断），现已收敛到 ``_send_text``。本测试
锁住收敛后的行为：

- send 与 reply 产出的 **CLI argv 完全一致**（只有前置校验与告警措辞不同）；
- 截断边界精确（2047 / 2048 / 2049 三点）；
- ``chat_type`` 随目标类型变化（群 2 / 单 1）；
- ``uuid`` 回传标记 ``noop_uuid`` 保持不变；
- 下载大小上限已上提为**双方共用**的模块常量。
"""
from __future__ import annotations

import json
from typing import Any

from src.im_adapter.wecom import WECOM_TEXT_MAX_BYTES, WecomCliAdapter


def _adapter_with_captured_run() -> tuple[Any, list[tuple[list[str], dict]]]:
    """构造一个「run 被替换为记录器」的适配器实例（无 CLI、无外部依赖）。"""
    adapter = object.__new__(WecomCliAdapter)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> dict:
        calls.append((cmd, kwargs))
        return {"errcode": 0, "errmsg": "ok"}

    adapter.run = fake_run
    return adapter, calls


def _payload_of(call: tuple[list[str], dict]) -> dict:
    argv = call[0]
    assert argv[:3] == ["msg", "send_message", "--json"], argv
    return json.loads(argv[3])


def test_send_and_reply_produce_identical_cli_argv():
    a_send, c_send = _adapter_with_captured_run()
    a_reply, c_reply = _adapter_with_captured_run()
    a_send.chat_message_send(group="g1", text="hello")
    a_reply.chat_message_reply(message_id="m1", text="hello", group="g1")
    assert c_send[0][0] == c_reply[0][0], "send 与 reply 应走同一 payload 构造"
    assert c_send[0][1] == c_reply[0][1], "force_no_dry_run 等调用参数应一致"


def test_text_truncation_boundary():
    for raw_len, expect_len in ((2047, 2047), (2048, 2048), (2049, 2048)):
        adapter, calls = _adapter_with_captured_run()
        adapter.chat_message_send(group="g", text="x" * raw_len)
        payload = _payload_of(calls[0])
        assert len(payload["text"]["content"]) == expect_len, raw_len


def test_chat_type_depends_on_target_kind():
    adapter, calls = _adapter_with_captured_run()
    adapter.chat_message_send(group="g", text="t")
    assert _payload_of(calls[0])["chat_type"] == 2, "群聊 chat_type=2"

    adapter2, calls2 = _adapter_with_captured_run()
    adapter2.chat_message_send(user="u", text="t")
    assert _payload_of(calls2[0])["chat_type"] == 1, "单聊 chat_type=1"


def test_uuid_is_echoed_as_noop_uuid():
    adapter, calls = _adapter_with_captured_run()
    result = adapter.chat_message_send(group="g", text="t", uuid="uu-1")
    assert result["noop_uuid"] == "uu-1"

    adapter2, calls2 = _adapter_with_captured_run()
    result2 = adapter2.chat_message_reply(message_id="m", text="t", group="g", uuid="uu-2")
    assert result2["noop_uuid"] == "uu-2"


def test_send_and_reply_share_one_implementation():
    """两个入口必须收敛到同一个 ``_send_text``（防止再次各写一份）。"""
    import inspect

    send_src = inspect.getsource(WecomCliAdapter.chat_message_send)
    reply_src = inspect.getsource(WecomCliAdapter.chat_message_reply)
    assert "_send_text(" in send_src
    assert "_send_text(" in reply_src
    assert "msgtype" not in send_src, "payload 构造不应留在 send 入口"
    assert "msgtype" not in reply_src, "payload 构造不应留在 reply 入口"


def test_download_size_limit_is_shared_between_base_and_wecom():
    """下载大小上限已上提为共用常量（原先 base.py 与 wecom.py 各写一份）。"""
    from src.im_adapter import base as base_mod
    from src.im_adapter import wecom as wecom_mod
    from src.im_adapter.constants import MAX_DOWNLOAD_SIZE

    assert MAX_DOWNLOAD_SIZE == 10 * 1024 * 1024
    assert base_mod.MAX_DOWNLOAD_SIZE == MAX_DOWNLOAD_SIZE
    assert wecom_mod.MAX_DOWNLOAD_SIZE == MAX_DOWNLOAD_SIZE
    assert WECOM_TEXT_MAX_BYTES == 2048
