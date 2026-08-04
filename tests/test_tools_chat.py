"""发送消息工具测试。"""
import os
from unittest.mock import MagicMock, patch


from src.tools.chat import SendMessageTool


class TestSendMessage:
    def _tool(self, store=None, self_user_id=""):
        dws = MagicMock()
        return SendMessageTool(dws, store=store, self_user_id=self_user_id), dws

    def test_text_to_group(self):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "hello"})
        assert res["success"] is True
        dws.chat_message_send.assert_called_once()
        kwargs = dws.chat_message_send.call_args.kwargs
        assert kwargs["group"] == "g1"
        assert kwargs["text"] == "hello"

    def test_text_to_single_no_store(self):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "u1", "chat_type": "single", "text": "hi"})
        assert res["success"] is True
        kwargs = dws.chat_message_send.call_args.kwargs
        assert kwargs["open_dingtalk_id"] == "u1"

    def test_text_to_single_with_store_cache(self):
        store = MagicMock()
        store._blacklist_repo.is_conversation_blocked.return_value = False
        store._blacklist_repo.list_blocked_conversations.return_value = []
        store._conversation_repo.get_conversation.return_value = {"peer_open_dingtalk_id": "oid-123"}
        tool, dws = self._tool(store=store)
        res = tool.execute({"chat_id": "u1", "chat_type": "single", "text": "hi"})
        assert res["success"] is True
        kwargs = dws.chat_message_send.call_args.kwargs
        assert kwargs["open_dingtalk_id"] == "oid-123"

    def test_text_to_single_with_user_id_cache(self):
        store = MagicMock()
        store._blacklist_repo.is_conversation_blocked.return_value = False
        store._blacklist_repo.list_blocked_conversations.return_value = []
        store._conversation_repo.get_conversation.return_value = {"peer_user_id": "uid-abc"}
        tool, dws = self._tool(store=store)
        res = tool.execute({"chat_id": "u1", "chat_type": "single", "text": "hi"})
        assert res["success"] is True
        kwargs = dws.chat_message_send.call_args.kwargs
        assert kwargs["user"] == "uid-abc"

    def test_missing_chat_id(self):
        tool, _ = self._tool()
        res = tool.execute({"chat_type": "group", "text": "x"})
        assert "error" in res

    def test_empty_text_for_text_type(self):
        tool, _ = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": ""})
        assert "error" in res

    def test_self_user_id_guard(self):
        tool, dws = self._tool(self_user_id="u-self")
        res = tool.execute({"chat_id": "u-self", "chat_type": "single", "text": "x"})
        assert "error" in res
        assert "禁止" in res["error"]
        dws.chat_message_send.assert_not_called()

    def test_rate_limit_guard(self):
        tool, dws = self._tool()
        for i in range(3):
            res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": f"msg{i}"})
        assert "error" in res
        assert "频次" in res["error"]

    @patch.object(os.path, "isfile", return_value=True)
    def test_image_with_media_id(self, _isfile):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "image", "media_id": "m123"})
        assert res["success"] is True

    @patch.object(os.path, "isfile", return_value=True)
    def test_image_with_file_path(self, _isfile):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "image", "file_path": "/tmp/x.png"})
        assert res["success"] is True

    def test_image_missing_both(self):
        tool, _ = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "image"})
        assert "error" in res

    def test_image_file_not_found(self):
        tool, _ = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "image",
                            "file_path": "/nonexistent/img.png"})
        assert "error" in res

    @patch.object(os.path, "isfile", return_value=True)
    def test_file_type(self, _isfile):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "file", "file_path": "/tmp/a.pdf"})
        assert res["success"] is True

    def test_file_missing_path(self):
        tool, _ = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "file"})
        assert "error" in res

    def test_file_not_found(self):
        tool, _ = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "file", "file_path": "/nonexistent/a.pdf"})
        assert "error" in res

    @patch.object(os.path, "isfile", return_value=True)
    def test_audio_type(self, _isfile):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "audio", "file_path": "/tmp/a.mp3"})
        assert res["success"] is True

    @patch.object(os.path, "isfile", return_value=True)
    def test_video_type(self, _isfile):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group",
                            "msg_type": "video", "file_path": "/tmp/a.mp4"})
        assert res["success"] is True

    def test_send_exception(self):
        tool, dws = self._tool()
        dws.chat_message_send.side_effect = RuntimeError("网络错误")
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "hello"})
        assert "error" in res
        assert "发送失败" in res["error"]

    def test_at_params(self):
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "hi",
                            "at_all": True, "at_open_dingtalk_ids": "id1,id2"})
        assert res["success"] is True
        kwargs = dws.chat_message_send.call_args.kwargs
        assert kwargs["at_all"] is True
        assert kwargs["at_open_dingtalk_ids"] == "id1,id2"

    def test_persists_bot_reply(self):
        store = MagicMock()
        store._blacklist_repo.is_conversation_blocked.return_value = False
        store._blacklist_repo.list_blocked_conversations.return_value = []
        store._conversation_repo.get_conversation.return_value = {"chat_name": "测试群"}
        tool, dws = self._tool(store=store)
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "bot says"})
        assert res["success"] is True
        store._message_repo.save_message.assert_called_once()

    def test_default_msg_type_is_auto(self):
        """send_message 默认 msg_type=auto（按内容自动判定 text / markdown）。"""
        tool, dws = self._tool()
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "ok"})
        assert res["msg_type"] == "auto"
        # auto 透传给适配器，由其按内容结构分类
        kwargs = dws.chat_message_send.call_args.kwargs
        assert kwargs["msg_type"] == "auto"

    def test_cleanup_expired_recent_sends(self):
        """line 129: 清理超过 10 秒的历史发送记录。"""
        import time
        tool, dws = self._tool()
        # 注入过期记录
        tool._recent_sends = [("old", time.time() - 20), ("old2", time.time() - 15)]
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "hello"})
        assert res["success"] is True
        # 过期记录应被清理，只留本次
        assert len(tool._recent_sends) == 1

    def test_truncate_recent_sends(self):
        """line 142: _recent_sends 超过 200 条时截断到 100 条。"""
        import time
        tool, dws = self._tool()
        tool._recent_sends = [(f"g{i}", time.time()) for i in range(201)]
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "hello"})
        assert res["success"] is True
        assert len(tool._recent_sends) == 100  # 追后 202 → 截断到 100

    def test_concurrent_send_no_index_error(self):
        """[P1-#7 回归] 多线程并发发送同一 chat_id：_recent_sends 的 pop/append 在锁保护下
        不抛 IndexError，且短时重复护栏仍生效（不崩溃）。"""
        import threading

        tool, dws = self._tool()
        errors = []

        def send_once(i):
            try:
                tool.execute({"chat_id": "g100", "chat_type": "group", "text": f"msg{i}"})
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=send_once, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"并发发送抛出异常: {errors}"
        # 限容保护：列表长度始终受限
        assert len(tool._recent_sends) <= 200
        # 实例锁已建立
        assert hasattr(tool, "_send_lock")

    def test_save_message_exception_handled(self):
        """lines 233-234: store.save_message 异常被捕获不传播。"""
        store = MagicMock()
        store._blacklist_repo.is_conversation_blocked.return_value = False
        store._blacklist_repo.list_blocked_conversations.return_value = []
        store._conversation_repo.get_conversation.return_value = {"chat_name": "测试群"}
        store._message_repo.save_message.side_effect = RuntimeError("db down")
        tool, dws = self._tool(store=store)
        res = tool.execute({"chat_id": "g1", "chat_type": "group", "text": "hello"})
        assert res["success"] is True  # 不因 save_message 失败而崩溃
