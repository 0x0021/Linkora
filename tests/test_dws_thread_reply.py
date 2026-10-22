"""钉钉话题(Thread)回复支持的回归测试。

背景（2026-10-22）：dws v1.0.63 起提供 ``chat thread reply``（向 openConvThreadId
直接追加回复）与 ``+thread-replies``。Linkora 此前在 ``chat_message_reply`` 里硬编码
"reply_in_thread 钉钉不支持，已忽略"，且入站消息解析完全不捕获话题字段
（``openConvThreadId``），导致群聊里用户在话题下发言时，AI 回复会被甩回主群流、
话题上下文被打断。

本测试锁定三件事：
1. 入站消息解析能捕获并透传 ``openConvThreadId``；
2. 传入 thread_id 时走 ``chat thread reply``（而非引用回复 / 普通发送）；
3. 合并消息（防抖 / 连发合并）不丢话题归属。
"""
from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, "src")

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError
from src.models import Message


class _FakeDws(DwsAdapter):
    """内存 mock：记录命令，支持按需注入失败。

    ``fail`` 语义：
      - ``True``  → 所有命令都失败（模拟 dws 整体不可用）；
      - ``"thread"`` → 仅话题回复命令失败，普通发送正常（模拟话题能力不可用，
        降级路径仍应把回复发出去）。
    """

    def __init__(self, fail: bool | str = False):
        self.cli_path = "dws"
        self.dry_run = False
        self.profile = ""
        self.timeout = 30
        self.ai_tag_default = True
        self._calls: list[list[str]] = []
        self._fail = fail

    def run(self, args, *a, **k):
        self._calls.append(list(args))
        is_thread = args[:3] == ["chat", "thread", "reply"]
        should_fail = (
            self._fail is True
            or (self._fail == "thread" and is_thread)
        )
        if should_fail:
            raise IMAdapterError("dws exit 1: topic reply failed")
        return {"success": True, "result": {"openTaskId": "task-1"}}


# ============ 1. 入站解析捕获话题字段 ============

def test_raw_to_message_captures_thread_id():
    """dws 消息带 openConvThreadId → Message.thread_id 被捕获。"""
    from src.poller_core_parse import ParseMixin

    class _P(ParseMixin):
        def __init__(self):
            self.config = type("C", (), {
                "skip_msg_types": ["image"],
                "graceful_fallback_msg_types": [],
                "image_ocr_enabled": False,
                "image_temp_dir": "/tmp",
            })()
            self._skip_ocr = True
            self.current_user_id = ""
            self.current_user_user_id = ""
            self.current_user_name = ""

    p = _P()
    msg = p._raw_to_message(
        {
            "openMessageId": "om_1",
            "sender": "张三",
            "senderOpenDingTalkId": "ou_1",
            "content": "hi",
            "createTime": "2026-10-22 10:00:00",
            "openConvThreadId": "ctc_abc",
        },
        "cid_group", "group", "测试群",
    )
    assert msg.thread_id == "ctc_abc"


def test_raw_to_message_thread_id_empty_for_normal_message():
    """普通群消息（无 openConvThreadId）→ thread_id 为空串，不影响原行为。"""
    from src.poller_core_parse import ParseMixin

    class _P(ParseMixin):
        def __init__(self):
            self.config = type("C", (), {
                "skip_msg_types": ["image"],
                "graceful_fallback_msg_types": [],
                "image_ocr_enabled": False,
                "image_temp_dir": "/tmp",
            })()
            self._skip_ocr = True
            self.current_user_id = ""
            self.current_user_user_id = ""
            self.current_user_name = ""

    p = _P()
    msg = p._raw_to_message(
        {
            "openMessageId": "om_1",
            "sender": "张三",
            "senderOpenDingTalkId": "ou_1",
            "content": "hi",
            "createTime": "2026-10-22 10:00:00",
        },
        "cid_group", "group", "测试群",
    )
    assert msg.thread_id == ""


# ============ 2. 回复路由：走 chat thread reply ============

def test_thread_id_routes_to_thread_reply():
    """传入 thread_id → 命令必须是 chat thread reply，且 --conversation-id 传 thread id。"""
    a = _FakeDws()
    a.chat_message_reply(
        text="收到", title="", uuid="u1",
        thread_id="ctc_abc", conversation_id="cid_group",
        fallback_to_send=False,
    )
    assert len(a._calls) == 1
    cmd = a._calls[0]
    assert cmd[:3] == ["chat", "thread", "reply"]
    # --conversation-id 传 Thread 子会话 openConvThreadId（不是父群 id）
    assert cmd[cmd.index("--conversation-id") + 1] == "ctc_abc"
    assert cmd[cmd.index("--content") + 1] == "收到"
    # 话题回复不产生引用气泡：不应带 ref-msg-id / ref-sender
    assert "--ref-msg-id" not in cmd
    assert "--ref-sender" not in cmd


def test_no_thread_id_keeps_native_quote_reply():
    """无 thread_id → 保持原有引用回复通道（行为不变）。"""
    a = _FakeDws()
    a.chat_message_reply(
        text="收到", title="", uuid="u1",
        ref_msg_id="om_1", ref_sender="ou_1",
        conversation_id="cid_group", fallback_to_send=False,
    )
    assert len(a._calls) == 1
    cmd = a._calls[0]
    assert cmd[:3] == ["chat", "message", "reply"]
    assert cmd[cmd.index("--ref-msg-id") + 1] == "om_1"
    assert cmd[cmd.index("--ref-sender") + 1] == "ou_1"


def test_thread_reply_failure_falls_back_to_send():
    """话题回复失败且 fallback_to_send=True → 降级为普通发送，回复不丢。"""
    a = _FakeDws(fail="thread")
    a.chat_message_reply(
        text="收到", title="", uuid="u1",
        thread_id="ctc_abc", conversation_id="cid_group",
        fallback_to_send=True,
    )
    # 第一次尝试 thread reply（失败），第二次降级为普通 send
    assert len(a._calls) == 2
    assert a._calls[0][:3] == ["chat", "thread", "reply"]
    assert a._calls[1][:3] == ["chat", "message", "send"]


def test_thread_reply_failure_raises_when_no_fallback():
    """fallback_to_send=False → 抛出异常，交由调用方走自己的分片发送分支。"""
    a = _FakeDws(fail=True)
    try:
        a.chat_message_reply(
            text="收到", title="", uuid="u1",
            thread_id="ctc_abc", conversation_id="cid_group",
            fallback_to_send=False,
        )
    except IMAdapterError:
        pass
    else:
        raise AssertionError("应当抛出 IMAdapterError")


def test_reply_in_thread_without_thread_id_does_not_raise():
    """仅 reply_in_thread=True 而无 thread_id → 降级普通回复，不报错（不刷 warning）。"""
    a = _FakeDws()
    a.chat_message_reply(
        text="收到", title="", uuid="u1",
        reply_in_thread=True, conversation_id="cid_group",
        fallback_to_send=False,
    )
    assert len(a._calls) == 1
    # 无 thread id → 无法定位话题，不应走 thread reply
    assert a._calls[0][:3] != ["chat", "thread", "reply"]


# ============ 3. 合并消息保留话题归属 ============

def test_merge_preserves_thread_id():
    """防抖合并消息继承话题归属（取首条非空），保证回复仍落在原话题。"""
    from src.poller_utils import merge_consecutive_messages

    def _mk(mid: str, thread: str) -> Message:
        return Message(
            msg_id=mid, chat_id="cid_g", chat_type="group", chat_name="群",
            sender_id="ou_1", sender_name="张三", content="内容" + mid,
            msg_type="text", timestamp=datetime(2026, 10, 22, 10, 0, 0),
            raw={}, thread_id=thread,
        )

    merged = merge_consecutive_messages([_mk("om_1", "ctc_x"), _mk("om_2", "ctc_x")])
    assert len(merged) == 1
    assert merged[0].thread_id == "ctc_x"
