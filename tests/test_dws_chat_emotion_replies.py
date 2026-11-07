"""dws 适配器表情回复读取方法的单测。"""
from __future__ import annotations

from src.dws_adapter.chat import DwsAdapterChatMixin


def _fake_adapter(data_returned, *, raises=False):
    class Fake(DwsAdapterChatMixin):
        timeout = 30

        def __init__(self):
            self._data = data_returned
            self._raises = raises

        def run(self, args, **kwargs):
            if self._raises:
                raise RuntimeError("boom")
            return self._data

        def _get_result(self, data):
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            return data

    return Fake()


def test_normalize_emotion_replies_field_variants():
    raw = [
        {"msgId": "m1", "emotionReplies": [
            {"emoji": "Done", "senderId": "u1", "senderName": "张三"}]},
        {"msg_id": "m2", "reactions": [
            {"emojiName": "OK", "openDingTalkId": "u2", "sender_name": "张三"}]},
    ]
    out = DwsAdapterChatMixin._normalize_emotion_replies(raw)
    assert out["m1"][0]["emoji"] == "Done"
    assert out["m1"][0]["sender_id"] == "u1"
    assert out["m1"][0]["sender_name"] == "张三"
    assert out["m2"][0]["emoji"] == "OK"
    assert out["m2"][0]["sender_id"] == "u2"


def test_normalize_emotion_replies_skip_missing_msg_id():
    raw = [{"emotionReplies": [{"emoji": "Done", "senderId": "u1"}]}]
    assert DwsAdapterChatMixin._normalize_emotion_replies(raw) == {}


def test_list_emotion_replies_parses_wrapped_result():
    data = {"success": True, "result": [
        {"msgId": "m1", "emotionReplies": [
            {"emoji": "Done", "senderId": "u1", "senderName": "张三"}]}]}
    out = _fake_adapter(data).chat_message_list_emotion_replies(["m1"])
    assert out["m1"][0]["emoji"] == "Done"
    assert out["m1"][0]["sender_name"] == "张三"


def test_list_emotion_replies_returns_empty_on_error():
    out = _fake_adapter(None, raises=True).chat_message_list_emotion_replies(["m1"])
    assert out == {}


def test_list_emotion_replies_empty_input():
    assert _fake_adapter(None).chat_message_list_emotion_replies([]) == {}
