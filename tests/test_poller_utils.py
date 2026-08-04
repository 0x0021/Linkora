"""poller_utils 纯函数单测。"""

from datetime import datetime, timedelta


from src.poller_utils import (
    combine_message_group,
    detect_chat_type,
    is_polite_message,
    merge_consecutive_messages,
    wrap_image_block,
)


# ============ detect_chat_type ============

class TestDetectChatType:
    def test_single_chat_true(self):
        conv = {"singleChat": True, "title": "张三", "sender": "张三"}
        assert detect_chat_type(conv) == "single"

    def test_single_chat_system_sender(self):
        conv = {"singleChat": True, "title": "审批通知", "sender": "系统"}
        assert detect_chat_type(conv) == "other"

    def test_group_chat_false(self):
        conv = {"singleChat": False, "title": "项目A", "sender": ""}
        assert detect_chat_type(conv) == "group"

    def test_no_singleChat_group_title(self):
        conv = {"title": "研发讨论群", "sender": ""}
        assert detect_chat_type(conv) == "group"

    def test_no_singleChat_external_friend(self):
        conv = {"title": "李四", "sender": "李四"}
        assert detect_chat_type(conv) == "single"


# ============ is_polite_message ============

class TestIsPoliteMessage:
    def test_empty(self):
        assert is_polite_message("") is False

    def test_thanks(self):
        assert is_polite_message("谢谢") is True
        assert is_polite_message("感谢") is True

    def test_ack(self):
        assert is_polite_message("收到") is True
        assert is_polite_message("好的 明白了") is True
        assert is_polite_message("好的呢") is True

    def test_business_content(self):
        assert is_polite_message("收到，帮我导出报表") is False
        assert is_polite_message("谢谢，已处理") is False

    def test_ok(self):
        assert is_polite_message("OK") is True
        assert is_polite_message("ok") is True

    def test_bye(self):
        assert is_polite_message("拜拜") is True
        assert is_polite_message("晚安") is True

    def test_with_filler(self):
        assert is_polite_message("谢谢老板") is True
        assert is_polite_message("知道了亲") is True


# ============ wrap_image_block ============

class TestWrapImageBlock:
    def test_empty(self):
        assert wrap_image_block("") == ""

    def test_wraps_content(self):
        result = wrap_image_block("订单号 123")
        assert "———— 图片识别内容 ————" in result
        assert "订单号 123" in result
        assert "———— 图片识别内容结束 ————" in result


# ============ combine_message_group ============

class TestCombineMessageGroup:
    def make_msg(self, msg_id="m1", content="hello", msg_type="text", chat_id="c1",
                 sender_id="u1", sender_name="user", ts=None):
        from src.models import Message
        return Message(
            msg_id=msg_id, chat_id=chat_id, chat_type="single", chat_name="test",
            sender_id=sender_id, sender_name=sender_name,
            content=content, msg_type=msg_type,
            timestamp=ts or datetime.now(), raw={},
        )

    def test_single_message_returns_unchanged(self):
        m = self.make_msg()
        result = combine_message_group([m])
        assert result is m

    def test_merge_two_text(self):
        now = datetime.now()
        m1 = self.make_msg(msg_id="m1", content="line1", ts=now)
        m2 = self.make_msg(msg_id="m2", content="line2", ts=now + timedelta(seconds=5))
        result = combine_message_group([m1, m2])
        assert result.content == "line1\nline2"
        assert result.msg_type == "text"  # no image → not mixed

    def test_merge_with_image(self):
        now = datetime.now()
        m1 = self.make_msg(msg_id="m1", content="text", msg_type="text", ts=now)
        m2 = self.make_msg(msg_id="m2", content="invoice.png", msg_type="image", ts=now + timedelta(seconds=3))
        result = combine_message_group([m1, m2])
        assert "text" in result.content
        assert "图片识别内容" in result.content
        assert result.msg_type == "mixed"

    def test_merge_all_polite(self):
        now = datetime.now()
        m1 = self.make_msg(msg_id="m1", content="收到", ts=now)
        m2 = self.make_msg(msg_id="m2", content="谢谢", ts=now + timedelta(seconds=3))
        result = combine_message_group([m1, m2])
        assert result is m1  # 全部礼貌 → 返回第一条


# ============ merge_consecutive_messages ============

class TestMergeConsecutiveMessages:
    def make_msg(self, msg_id="m1", content="hello", msg_type="text", chat_id="c1",
                 sender_id="u1", sender_name="user", ts=None):
        from src.models import Message
        return Message(
            msg_id=msg_id, chat_id=chat_id, chat_type="single", chat_name="test",
            sender_id=sender_id, sender_name=sender_name,
            content=content, msg_type=msg_type,
            timestamp=ts or datetime.now(), raw={},
        )

    def test_empty_list(self):
        assert merge_consecutive_messages([]) == []

    def test_single_message(self):
        m = self.make_msg()
        result = merge_consecutive_messages([m])
        assert len(result) == 1
        assert result[0] is m

    def test_merge_consecutive_same_sender(self):
        now = datetime.now()
        msgs = [
            self.make_msg(msg_id="m1", content="a", ts=now),
            self.make_msg(msg_id="m2", content="b", ts=now + timedelta(seconds=5)),
            self.make_msg(msg_id="m3", content="c", ts=now + timedelta(seconds=10)),
        ]
        result = merge_consecutive_messages(msgs, window_seconds=60)
        assert len(result) == 1
        assert "a" in result[0].content
        assert "b" in result[0].content
        assert "c" in result[0].content

    def test_split_different_sender(self):
        now = datetime.now()
        msgs = [
            self.make_msg(msg_id="m1", content="a", sender_id="u1", ts=now),
            self.make_msg(msg_id="m2", content="b", sender_id="u2", ts=now + timedelta(seconds=5)),
        ]
        result = merge_consecutive_messages(msgs, window_seconds=60)
        assert len(result) == 2

    def test_split_exceeds_window(self):
        now = datetime.now()
        msgs = [
            self.make_msg(msg_id="m1", content="a", ts=now),
            self.make_msg(msg_id="m2", content="b", ts=now + timedelta(seconds=120)),
        ]
        result = merge_consecutive_messages(msgs, window_seconds=30)
        assert len(result) == 2
