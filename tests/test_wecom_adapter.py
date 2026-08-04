"""WecomCliAdapter 单元测试（mock subprocess，不依赖真实 wecom-cli / 网络）。

覆盖：命令拼接（无 --dry-run）、JSON-RPC 信封解析（成功 / 信封 isError /
内层 errcode）、错误分类、认证判定、联系人过滤、发送 payload 构造、媒体下载解码。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.im_adapter.base import BaseIMAdapter  # noqa: E402
from src.im_adapter.base_adapter import BaseIMAdapter as BaseIMAdapterCore  # noqa: E402
from src.im_adapter.errors import (  # noqa: E402
    IMAdapterError,
)
from src.im_adapter.wecom import WecomCliAdapter  # noqa: E402


def _fake(stdout="{}", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class _RunCapture:
    """记录 subprocess.run 调用，并按配置返回假响应。"""

    def __init__(self, response=None, side_effect=None):
        self.calls = []
        self._response = response
        self._side_effect = side_effect

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._side_effect is not None:
            return self._side_effect(*args, **kwargs)
        return self._response

    @property
    def last_cmd(self):
        return self.calls[-1][0][0] if self.calls else None


def _env(inner, *, is_error: bool = False) -> str:
    """构造 wecom-cli 的 JSON-RPC 信封 stdout。"""
    text = inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
    return json.dumps({
        "id": "mcp_test", "jsonrpc": "2.0",
        "result": {"content": [{"text": text, "type": "text"}], "isError": is_error},
    })


USERLIST = {"errcode": 0, "errmsg": "ok", "userlist": [
    {"userid": "owner", "name": "OWNER", "alias": "助手"},
    {"userid": "lila", "name": "李拉", "alias": ""},
]}


class TestBuildCommand(unittest.TestCase):
    def test_default_cli_path(self):
        a = WecomCliAdapter()
        self.assertEqual(a.cli_path, "/opt/homebrew/bin/wecom-cli")

    def test_inherits_base_and_skeleton(self):
        self.assertTrue(issubclass(WecomCliAdapter, BaseIMAdapterCore))
        self.assertTrue(issubclass(WecomCliAdapter, BaseIMAdapter))

    def test_build_no_dry_run_flag(self):
        a = WecomCliAdapter(dry_run=True)
        cmd = a._build_command(["msg", "send_message", "--json", "{}"])
        self.assertEqual(cmd[0], "/opt/homebrew/bin/wecom-cli")
        self.assertNotIn("--dry-run", cmd)
        self.assertNotIn("--profile", cmd)


class TestEnvelopeParsing(unittest.TestCase):
    def test_success_returns_inner(self):
        cap = _RunCapture(_fake(_env({"errcode": 0, "msglist": [1, 2]})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            data = a.run(["msg", "get_message", "--json", "{}"])
        self.assertEqual(data, {"errcode": 0, "msglist": [1, 2]})

    def test_envelope_iserror_raises(self):
        cap = _RunCapture(_fake(_env("Error executing tool x: boom", is_error=True)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            with self.assertRaises(IMAdapterError):
                a.run(["msg", "get_message", "--json", "{}"])

    def test_inner_errcode_nonzero_raises(self):
        cap = _RunCapture(_fake(_env({"errcode": 850017, "errmsg": "invalid media id"})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            with self.assertRaises(IMAdapterError):
                a.run(["msg", "get_msg_media", "--json", "{}"])

    def test_error_prefix_stripped(self):
        cap = _RunCapture(_fake("Error: 请求失败：" + _env(
            "Error executing tool y: validation", is_error=True)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            with self.assertRaises(IMAdapterError):
                a.run(["msg", "get_msg_chat_list", "--json", "{}"])

    def test_empty_output_returns_empty_dict(self):
        cap = _RunCapture(_fake(""))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            self.assertEqual(a.run(["x"]), {})


class TestErrorClassification(unittest.TestCase):
    def test_permission_code(self):
        a = WecomCliAdapter()
        self.assertIs(a._classify_error(json.dumps({"errcode": 60020, "errmsg": "not allowed"})),
                      a._permission_error_class())

    def test_retryable_code(self):
        a = WecomCliAdapter()
        self.assertIs(a._classify_error(json.dumps({"errcode": 45009, "errmsg": "freq out of limit"})),
                      a._retryable_error_class())

    def test_base_error_default(self):
        a = WecomCliAdapter()
        self.assertIs(a._classify_error("some weird failure"),
                      a._base_error_class())

    def test_permission_hint(self):
        a = WecomCliAdapter()
        self.assertIs(a._classify_error("api forbidden: no permission"),
                      a._permission_error_class())

    def test_retryable_hint(self):
        a = WecomCliAdapter()
        self.assertIs(a._classify_error("api freq out of limit"),
                      a._retryable_error_class())


class TestAuth(unittest.TestCase):
    def test_is_authenticated_true(self):
        cap = _RunCapture(_fake(_env(USERLIST)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            self.assertTrue(a.is_authenticated())

    def test_is_authenticated_false_on_error(self):
        cap = _RunCapture(_fake(_env({"errcode": 40014, "errmsg": "invalid token"})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            self.assertFalse(a.is_authenticated())

    def test_auth_status_shape(self):
        cap = _RunCapture(_fake(_env(USERLIST)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            st = a.auth_status()
        self.assertTrue(st["authenticated"])
        self.assertEqual(st["userlist_count"], 2)


class TestContact(unittest.TestCase):
    def test_search_filters_by_keyword(self):
        cap = _RunCapture(_fake(_env(USERLIST)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            res = a.contact_user_search("OWNER")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["userid"], "owner")

    def test_search_empty_keyword(self):
        a = WecomCliAdapter()
        self.assertEqual(a.contact_user_search(""), [])

    def test_get_self_returns_empty(self):
        # wecom-cli 不可用（subprocess 抛异常）且无 WECOM_USER_ID / $USER 时，应回退为 {}
        cap = _RunCapture(side_effect=Exception("wecom-cli unavailable"))
        with patch("src.im_adapter.wecom.subprocess.run", cap), \
                patch.dict(os.environ, {"USER": "", "WECOM_USER_ID": ""}):
            self.assertEqual(WecomCliAdapter().contact_user_get_self(), {})


class TestMessages(unittest.TestCase):
    def test_send_payload_construction(self):
        cap = _RunCapture(_fake(_env({"errcode": 0, "errmsg": "ok"})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            a.chat_message_send(user="owner", text="你好世界")
        cmd = cap.last_cmd
        self.assertEqual(cmd[0], "/opt/homebrew/bin/wecom-cli")
        self.assertEqual(cmd[1:3], ["msg", "send_message"])
        payload = json.loads(cmd[3 + 1])  # --json <payload>
        self.assertEqual(payload["chat_type"], 1)
        self.assertEqual(payload["chatid"], "owner")
        self.assertEqual(payload["msgtype"], "text")
        self.assertEqual(payload["text"]["content"], "你好世界")

    def test_send_group_chat_type(self):
        cap = _RunCapture(_fake(_env({"errcode": 0, "errmsg": "ok"})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            a.chat_message_send(group="grp_123", text="hi")
        payload = json.loads(cap.last_cmd[3 + 1])
        self.assertEqual(payload["chat_type"], 2)
        self.assertEqual(payload["chatid"], "grp_123")

    def test_list_unread_conversations(self):
        chats = {"errcode": 0, "chats": [{"chatid": "a"}, {"chatid": "b"}], "has_more": False}
        cap = _RunCapture(_fake(_env(chats)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            res = a.chat_message_list_unread_conversations(count=10)
        self.assertEqual(len(res), 2)

    def test_chat_message_list_group(self):
        msgs = {"errcode": 0, "messages": [{"msgid": "1"}, {"msgid": "2"}]}
        cap = _RunCapture(_fake(_env(msgs)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            res = a.chat_message_list(group="grp_123", time_str="")
        self.assertEqual(len(res), 2)
        payload = json.loads(cap.last_cmd[3 + 1])
        self.assertEqual(payload["chat_type"], 2)

    def test_chat_message_list_accepts_cached_result(self):
        """poller 对群聊无差别传 cached_result；企微忽略该参数（不报 TypeError）。"""
        msgs = {"errcode": 0, "messages": [{"msgid": "1"}]}
        cap = _RunCapture(_fake(_env(msgs)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            res = a.chat_message_list(group="grp_123", time_str="",
                                     cached_result={"conversationMessagesList": []})
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["msgid"], "1")

    def test_chat_message_reply_degrades_to_send(self):
        # 企微无原生回复；reply 降级为向会话重发，thread 忽略、uuid 以 noop_uuid 回传
        cap = _RunCapture(_fake(_env({"errcode": 0, "errmsg": "ok"})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap), \
                self.assertLogs("src.im_adapter.wecom", level="WARNING") as logs:
            res = a.chat_message_reply(message_id="msgid_x", text="回复",
                                       reply_in_thread=True, uuid="u9", group="grp_123")
        payload = json.loads(cap.last_cmd[3 + 1])
        self.assertEqual(payload["chat_type"], 2)
        self.assertEqual(payload["chatid"], "grp_123")
        self.assertEqual(payload["text"]["content"], "回复")
        self.assertEqual(res.get("noop_uuid"), "u9")
        self.assertTrue(any("reply_in_thread" in m for m in logs.output))

    def test_chat_message_reply_requires_target(self):
        a = WecomCliAdapter()
        with self.assertRaises(ValueError):
            a.chat_message_reply(message_id="x", text="hi")  # 企微需显式 group/user


class TestDownloadMedia(unittest.TestCase):
    def test_decodes_base64_to_file(self):
        sample = b"hello-wecom-file"
        b64 = base64.b64encode(sample).decode()
        resp = _env({"errcode": 0, "content": b64, "filename": "x.txt"})
        cap = _RunCapture(_fake(resp))
        a = WecomCliAdapter()
        out = "/tmp/_wecom_test_media.txt"
        if os.path.exists(out):
            os.remove(out)
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            path = a.download_media(media_id="m_1", message_id="msg_1",
                                    conversation_id="c_1", output_path=out)
        self.assertTrue(os.path.exists(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), sample)
        os.remove(out)

    def test_no_base64_raises(self):
        cap = _RunCapture(_fake(_env({"errcode": 0, "filename": "x.txt"})))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            with self.assertRaises(IMAdapterError):
                a.download_media(media_id="m_1", message_id="m", conversation_id="c",
                                 output_path="/tmp/_wecom_x.txt")


class TestOrg(unittest.TestCase):
    def test_get_current_org_placeholder(self):
        org = WecomCliAdapter().get_current_org()
        self.assertEqual(org["corp_id"], "wecom")

    def test_list_orgs_single(self):
        self.assertEqual(len(WecomCliAdapter().list_orgs()), 1)

    def test_use_org_true(self):
        self.assertTrue(WecomCliAdapter().use_org("wecom"))


class TestNormalizeChat(unittest.TestCase):
    """验证 _normalize_chat 将企微会话映射为 poller 兼容字段（镜像飞书）。"""

    def test_chatid_maps_to_openConversationId_and_single(self):
        out = WecomCliAdapter()._normalize_chat({"chatid": "c1", "chat_type": 1})
        self.assertEqual(out["openConversationId"], "c1")
        self.assertTrue(out["singleChat"])

    def test_id_fallback_group(self):
        out = WecomCliAdapter()._normalize_chat({"id": "c2", "chat_type": 2})
        self.assertEqual(out["openConversationId"], "c2")
        self.assertFalse(out["singleChat"])

    def test_chat_id_fallback(self):
        out = WecomCliAdapter()._normalize_chat({"chat_id": "c3"})
        self.assertEqual(out["openConversationId"], "c3")

    def test_title_defaults_to_cid(self):
        out = WecomCliAdapter()._normalize_chat({"chatid": "c4"})
        self.assertEqual(out["title"], "c4")

    def test_title_from_name(self):
        out = WecomCliAdapter()._normalize_chat({"chatid": "c5", "name": "项目组"})
        self.assertEqual(out["title"], "项目组")

    def test_no_cid_returns_unchanged(self):
        chat = {"foo": "bar"}
        self.assertIs(WecomCliAdapter()._normalize_chat(chat), chat)

    def test_singleChat_via_is_group_when_no_chat_type(self):
        a = WecomCliAdapter()
        self.assertTrue(a._normalize_chat({"chatid": "x", "is_group": False})["singleChat"])
        self.assertFalse(a._normalize_chat({"chatid": "x", "is_group": True})["singleChat"])


class TestUnreadConversationsNormalized(unittest.TestCase):
    """验证未读/最近会话返回已归一化（含 openConversationId）。"""

    def test_normalizes_openConversationId_title_singleChat(self):
        chats = {"errcode": 0, "chats": [
            {"chatid": "a", "chat_type": 1, "name": "甲"},
            {"chatid": "b", "chat_type": 2},
        ], "has_more": False}
        cap = _RunCapture(_fake(_env(chats)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            res = a.chat_message_list_unread_conversations(count=10)
        self.assertEqual(len(res), 2)
        self.assertEqual(res[0]["openConversationId"], "a")
        self.assertEqual(res[0]["title"], "甲")
        self.assertTrue(res[0]["singleChat"])
        self.assertEqual(res[1]["openConversationId"], "b")
        self.assertFalse(res[1]["singleChat"])

    def test_top_conversations_reuses_normalization(self):
        chats = {"errcode": 0, "chats": [{"chatid": "z", "chat_type": 1}], "has_more": False}
        cap = _RunCapture(_fake(_env(chats)))
        a = WecomCliAdapter()
        with patch("src.im_adapter.wecom.subprocess.run", cap):
            res = a.chat_list_top_conversations(limit=5)
        self.assertEqual(res[0]["openConversationId"], "z")


class TestContactGetSelf(unittest.TestCase):
    """验证自身 user_id 解析（供 main._build_platform_context 自我消息过滤）。"""

    def test_from_env(self):
        with patch.dict(os.environ, {"WECOM_USER_ID": "env_user", "USER": ""}):
            self.assertEqual(WecomCliAdapter().contact_user_get_self(),
                             {"user_id": "env_user", "name": "env_user", "title": ""})

    def test_single_user_in_list(self):
        cap = _RunCapture(_fake(_env({"errcode": 0, "userlist": [
            {"userid": "me", "name": "我"}]})))
        with patch("src.im_adapter.wecom.subprocess.run", cap), \
                patch.dict(os.environ, {"WECOM_USER_ID": "", "USER": ""}):
            info = WecomCliAdapter().contact_user_get_self()
        self.assertEqual(info.get("user_id"), "me")

    def test_position_extracted_as_title(self):
        cap = _RunCapture(_fake(_env({"errcode": 0, "userlist": [
            {"userid": "me", "name": "我", "position": "后端工程师"}]})))
        with patch("src.im_adapter.wecom.subprocess.run", cap), \
                patch.dict(os.environ, {"WECOM_USER_ID": "", "USER": ""}):
            info = WecomCliAdapter().contact_user_get_self()
        self.assertEqual(info.get("title"), "后端工程师")


if __name__ == "__main__":
    unittest.main()
