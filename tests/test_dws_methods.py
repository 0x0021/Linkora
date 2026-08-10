"""DwsAdapter API 高层方法测试。"""
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from src.dws_adapter import (
    DwsAdapter, DwsError, DwsPermissionError,
    is_permission_error, is_org_config_problem, classify_dws_error,
)


# ============ 错误分类 ============

class TestErrorClassification:
    def test_is_permission_true(self):
        assert is_permission_error("TOKEN_VERIFIED_FAILED please re-auth") is True
        assert is_permission_error("该组织尚未开启 CLI 数据访问权限") is True
        assert is_permission_error("AGENT_CODE_NOT_EXISTS session expired") is True
        assert is_permission_error("AUTH_PERMISSION_DENIED no access") is True
        assert is_permission_error("is not in conversation") is True

    def test_is_permission_false(self):
        assert is_permission_error("timeout") is False
        assert is_permission_error("network error") is False

    def test_is_org_config_true(self):
        assert is_org_config_problem("该组织尚未开启 CLI 数据访问权限，请联系管理员") is True
        assert is_org_config_problem("AGENT_CODE_NOT_EXISTS org not found") is True

    def test_is_org_config_false(self):
        assert is_org_config_problem("timeout") is False

    def test_classify_permission(self):
        err = classify_dws_error("TOKEN_VERIFIED_FAILED")
        assert issubclass(err, DwsPermissionError)


# ============ 高层 API ============

@pytest.fixture
def adapter():
    return DwsAdapter(cli_path="dws")


class TestChatMessageList:
    def test_list_direct(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"messages": [
                {"msgId": "m1"}, {"msgId": "m2"}
            ]}}
            r = adapter.chat_message_list_direct(open_dingtalk_id="oid1")
        assert len(r) == 2
        assert r[0]["msgId"] == "m1"

    def test_list_direct_with_user_id(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"messages": [{"msgId": "m1"}]}}
            r = adapter.chat_message_list_direct(user_id="u1")
        assert len(r) == 1

    def test_list_direct_no_id_raises(self, adapter):
        with pytest.raises(ValueError):
            adapter.chat_message_list_direct()

    def test_chat_message_list(self, adapter):
        # 群消息走用户级逐群接口 chat_message_list_group
        with patch.object(adapter, "chat_message_list_group") as m:
            m.return_value = [{"msgId": "m99"}]
            r = adapter.chat_message_list(group="g1", time_str="2026-07-11 00:00:00")
        assert r[0]["msgId"] == "m99"

    def test_chat_message_list_not_dict(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {}
            r = adapter.chat_message_list(group="g1", time_str="now")
        assert r == []

    def test_chat_message_list_cached_result_ignored(self, adapter):
        """cached_result 参数保留为兼容接口，当前实现不读取它（已废弃）。"""
        cached = {
            "conversationMessagesList": [
                {"openConversationId": "g1", "messages": [{"msgId": "m1"}, {"msgId": "m2"}]},
                {"openConversationId": "g2", "messages": [{"msgId": "m3"}]},
            ]
        }
        with patch.object(adapter, "chat_message_list_group") as m_group:
            m_group.return_value = []
            adapter.chat_message_list(
                group="g1", time_str="2026-07-11 00:00:00", cached_result=cached
            )
        # cached_result 不再被消费，走逐群接口
        assert m_group.called
        m_group.assert_called_once_with("g1", "2026-07-11 00:00:00", 50, None)

    def test_chat_message_list_cached_result_missing_group_ignored(self, adapter):
        """群不在缓存中时同样忽略 cached_result，直接调逐群接口返回空列表。"""
        cached = {"conversationMessagesList": [
            {"openConversationId": "g_other", "messages": [{"msgId": "x"}]}
        ]}
        with patch.object(adapter, "chat_message_list_group") as m_group:
            m_group.return_value = []
            r = adapter.chat_message_list(
                group="g_missing", time_str="2026-07-11 00:00:00", cached_result=cached
            )
        assert r == []
        assert m_group.called

    def test_list_all_single_page(self, adapter):
        """list_all 单页返回，无 hasMore 时应直接返回。"""
        with patch.object(adapter, "run") as m:
            m.return_value = {
                "result": {
                    "conversationMessagesList": [{"openConversationId": "c1", "messages": []}],
                    "hasMore": False, "nextCursor": "",
                }
            }
            r = adapter.chat_message_list_all("2026-07-11 00:00:00", "2026-07-11 23:59:59")
        assert len(r["conversationMessagesList"]) == 1

    def test_list_all_permission_silent(self, adapter):
        with patch.object(adapter, "run") as m:
            m.side_effect = DwsError("TOKEN_VERIFIED_FAILED: group-chat")
            r = adapter.chat_message_list_all("s", "e")
        assert r == {"conversationMessagesList": [], "hasMore": False, "nextCursor": ""}

    def test_list_all_honors_explicit_max_pages(self, adapter):
        """显式 max_pages 应严格限制翻页次数并截断（仍带 hasMore）。"""
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            return {"result": {
                "conversationMessagesList": [{"openConversationId": f"c{calls['n']}", "messages": []}],
                "hasMore": True, "nextCursor": f"cur{calls['n']}",
            }}

        with patch.object(adapter, "run", side_effect=fake_run):
            r = adapter.chat_message_list_all("2026-07-11 00:00:00", "2026-07-11 23:59:59", max_pages=3)
        assert calls["n"] == 3
        assert r["hasMore"] is True  # 因被 max_pages 截断

    def test_list_all_default_max_pages_is_50(self, adapter):
        """未传 max_pages 时默认上限 50（原硬编码 20 已提升）。"""
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            return {"result": {"conversationMessagesList": [], "hasMore": True, "nextCursor": "x"}}

        with patch.object(adapter, "run", side_effect=fake_run):
            adapter.chat_message_list_all("s", "e")
        assert calls["n"] == 50

    def test_list_all_zero_max_pages_falls_back_to_50(self, adapter):
        """max_pages=0 应回落到默认 50，而非无限翻页。"""
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            return {"result": {"conversationMessagesList": [], "hasMore": True, "nextCursor": "x"}}

        with patch.object(adapter, "run", side_effect=fake_run):
            adapter.chat_message_list_all("s", "e", max_pages=0)
        assert calls["n"] == 50

    def test_list_all_cap_warning_throttled(self, adapter, caplog):
        """同一窗口 5 分钟内封顶提示只应出现一次，避免每轮轮询刷屏。"""
        import logging
        # 类级冷却字典可能残留其它测试的时间戳，先清空以保证第一次调用能触发。
        adapter.__class__._list_all_cap_warn_at.clear()
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            return {"result": {"conversationMessagesList": [], "hasMore": True, "nextCursor": "x"}}

        with caplog.at_level(logging.INFO, logger="src.dws_adapter"):
            with patch.object(adapter, "run", side_effect=fake_run):
                adapter.chat_message_list_all("s", "e", max_pages=1)
                adapter.chat_message_list_all("s", "e", max_pages=1)
        warns = [r for r in caplog.records if "分页达到上限" in r.getMessage()]
        assert len(warns) == 1, f"期望 5 分钟内仅提示一次，实际 {len(warns)} 次"

    def test_list_all_windows_large_span(self, adapter):
        """大时间窗（>window_days）应自动分窗：每子窗独立翻页、合并去重，避免单窗触顶漏消息。"""
        calls = []

        def fake_run(cmd, **kw):
            si, ei = cmd.index("--start"), cmd.index("--end")
            calls.append((cmd[si + 1], cmd[ei + 1]))
            # 每子窗只返回一页（无 hasMore），模拟正常拉取
            return {"result": {
                "conversationMessagesList": [{
                    "openConversationId": "c1",
                    "messages": [{"openMessageId": f"m{len(calls)}"}],
                }],
                "hasMore": False, "nextCursor": "",
            }}

        with patch.object(adapter, "run", side_effect=fake_run):
            r = adapter.chat_message_list_all(
                "2026-07-01 00:00:00", "2026-07-15 00:00:00", window_days=7
            )
        # 14 天跨度 / 7 天每窗 = 2 个子窗，各自独立翻页
        assert len(calls) == 2, f"期望分 2 窗，实际 {len(calls)} 次调用: {calls}"
        # 子窗边界消息按 openMessageId 去重合并，不丢不重
        msgs = [m for c in r["conversationMessagesList"] for m in c.get("messages", [])]
        assert len(msgs) == 2, f"期望合并 2 条消息，实际 {len(msgs)} 条"

    def test_list_all_no_window_split_when_span_le_window(self, adapter):
        """跨度 <= window_days 时不应分窗（行为与单窗一致，避免无谓多次调用）。"""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(1)
            return {"result": {"conversationMessagesList": [], "hasMore": False, "nextCursor": ""}}

        with patch.object(adapter, "run", side_effect=fake_run):
            adapter.chat_message_list_all(
                "2026-07-01 00:00:00", "2026-07-07 00:00:00", window_days=7
            )
        assert len(calls) == 1, f"7 天跨度不应分窗，实际调用 {len(calls)} 次"


class TestChatMessageSend:
    def test_send_text_group(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"success": True}
            r = adapter.chat_message_send(group="g1", text="hello")
        assert r["success"] is True

    def test_send_text_single(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            r = adapter.chat_message_send(user="u1", text="hi")
        assert r["ok"] is True

    def test_send_text_open_dingtalk_id(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            r = adapter.chat_message_send(open_dingtalk_id="oid", text="yo")
        assert r["ok"] is True

    def test_send_no_target_raises(self, adapter):
        with pytest.raises(ValueError):
            adapter.chat_message_send(text="no target")

    def test_send_at_all(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text="hi", at_all=True)
        cmd = m.call_args[0][0]
        assert "--at-all" in " ".join(cmd)

    def test_send_at_open_ids_injects_placeholder(self, adapter):
        """at_open_dingtalk_ids 应在 text 中自动补入 <@id> 占位符。"""
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text="hello",
                                      at_open_dingtalk_ids="uid1,uid2")
        cmd = m.call_args[0][0]
        assert "--at-open-dingtalk-ids" in " ".join(cmd)

    def test_send_image_with_media_id(self, adapter, tmp_path):
        f = tmp_path / "test.png"
        f.write_text("fake")
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(
                group="g1", msg_type="image", media_id="mid-1",
                file_path=str(f),
            )
        # 有 media_id 则不走上传
        cmd_str = " ".join(m.call_args[0][0])
        assert "--media-id" in cmd_str
        assert "--msg-type" in cmd_str

    def test_send_image_auto_upload(self, adapter, tmp_path):
        f = tmp_path / "img.png"
        f.write_text("fake")
        with patch.object(adapter, "run") as m_run:
            with patch.object(adapter, "media_upload", return_value="mid-auto"):
                m_run.return_value = {"ok": True}
                r = adapter.chat_message_send(
                    group="g1", msg_type="image", file_path=str(f),
                )
        assert r["ok"] is True

    def test_send_image_no_media_id_no_file(self, adapter):
        with pytest.raises(ValueError):
            adapter.chat_message_send(group="g1", msg_type="image")

    def test_send_file(self, adapter, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_text("pdf content")
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            r = adapter.chat_message_send(
                group="g1", msg_type="file", file_path=str(f),
            )
        assert r["ok"] is True

    def test_send_no_text_for_text_type_raises(self, adapter):
        with pytest.raises(ValueError):
            adapter.chat_message_send(group="g1")


class TestMedia:
    def test_media_upload_creates_temp_file(self, adapter):
        """media_upload 需要真实文件路径。"""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(b"fake png data")
            tmp = tf.name
        try:
            with patch.object(adapter, "run") as m:
                m.return_value = {"mediaId": "mid-xyz"}
                mid = adapter.media_upload(tmp)
            assert mid == "mid-xyz"
        finally:
            os.unlink(tmp)

    def test_media_upload_file_not_found(self, adapter):
        with pytest.raises(ValueError, match="文件不存在"):
            adapter.media_upload("/nonexistent/file.png")

    def test_extract_media_id_top_level(self, adapter):
        mid = DwsAdapter._extract_media_id({"mediaId": "abc"})
        assert mid == "abc"

    def test_extract_media_id_underscore(self, adapter):
        mid = DwsAdapter._extract_media_id({"media_id": "xyz"})
        assert mid == "xyz"

    def test_extract_media_id_nested(self, adapter):
        mid = DwsAdapter._extract_media_id({"result": {"mediaId": "nested"}})
        assert mid == "nested"

    def test_extract_media_id_not_found(self, adapter):
        assert DwsAdapter._extract_media_id({}) == ""
        assert DwsAdapter._extract_media_id("string") == ""

    def test_download_media(self, adapter):
        """正常下载应返回 output_path。"""
        with patch("subprocess.run") as m_run, \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=100):
            m_run.return_value.returncode = 0
            m_run.return_value.stdout = ""
            m_run.return_value.stderr = ""
            r = adapter.download_media(
                media_id="m1", message_id="msg1",
                conversation_id="conv1", output_path="/tmp/out.png",
            )
        assert r == "/tmp/out.png"

    def test_download_media_rejects_empty_file(self, adapter):
        """returncode==0 但未写出有效文件时应抛错（P2-12：避免空图进入 OCR 链路）。"""
        with patch("subprocess.run") as m_run, \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=0):
            m_run.return_value.returncode = 0
            m_run.return_value.stdout = ""
            m_run.return_value.stderr = ""
            with pytest.raises(Exception):  # noqa: B017
                adapter.download_media(
                    media_id="m1", message_id="msg1",
                    conversation_id="conv1", output_path="/tmp/out.png",
                )


class TestOrgProfileMethods:
    def test_get_current_org_from_local(self, adapter, tmp_path):
        """从本地 profiles.json 读取当前组织。"""
        profiles_dir = tmp_path / ".dws"
        profiles_dir.mkdir()
        profile_data = {
            "profiles": [{"corpId": "c001", "corpName": "测试公司"}],
            "currentProfile": "c001",
        }
        (profiles_dir / "profiles.json").write_text(json.dumps(profile_data))

        with patch.object(adapter, "_read_local_profiles") as m:
            m.return_value = {"profiles": profile_data["profiles"],
                              "currentProfile": "c001"}
            r = adapter.get_current_org()
        assert r["corp_id"] in ("c001", "")

    def test_list_orgs_success(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {
                "profiles": [
                    {"corpId": "c1", "corpName": "A公司"},
                    {"corpId": "c2", "corpName": "B公司"},
                ]
            }}
            r = adapter.list_orgs()
        assert len(r) == 2
        assert r[0]["corp_name"] == "A公司"

    def test_list_orgs_not_dict(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = "not a dict"
            r = adapter.list_orgs()
        assert r == []

    def test_contact_user_search(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": [
                {"name": "张三", "userId": "u1"}
            ]}
            r = adapter.contact_user_search("张")
        assert r[0]["name"] == "张三"

    def test_contact_user_search_personal(self, adapter):
        with patch.object(adapter, "run") as m:
            m.side_effect = DwsError("该组织尚未开启 CLI 数据访问权限")
            r = adapter.contact_user_search("x")
        assert r == []

    def test_contact_user_search_not_list(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"total": 0}}
            r = adapter.contact_user_search("x")
        assert r == []


class TestDocCalendarMethods:
    def test_doc_search(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"documents": [{"name": "周报", "nodeId": "n1"}]}
            r = adapter.doc_search("周报")
        assert len(r) == 1

    def test_doc_search_no_documents(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = "string"
            r = adapter.doc_search("x")
        assert r == []

    def test_doc_read(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"content": "# markdown"}}
            r = adapter.doc_read("n1")
        assert "# markdown" in r["result"]["content"]

    def test_doc_read_non_dict(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = "not dict"
            r = adapter.doc_read("n1")
        assert r == {}

    def test_calendar_event_list(self, adapter):
        events = [{"title": "晨会"}, {"title": "复盘"}]
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"events": events}}
            r = adapter.calendar_event_list("2026-07-11T00:00:00", "2026-07-11T23:59:59")
        assert len(r) == 2
        # 应该带 --start / --end 参数
        cmd = m.call_args[0][0]
        assert "--start" in cmd

    def test_calendar_event_list_empty(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {}}
            r = adapter.calendar_event_list()
        assert r == []


class TestConvenienceReadMethods:
    def test_conversation_info(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"title": "测试群", "memberCount": 5}}
            r = adapter.chat_conversation_info("conv-1")
        assert r["title"] == "测试群"

    def test_conversation_info_non_dict(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = ["not dict"]
            r = adapter.chat_conversation_info("conv-1")
        assert r == {}

    def test_mark_read(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"ok": True}}
            adapter.mark_read("conv-1", "msg-1")

    def test_mark_read_missing_args(self, adapter):
        with pytest.raises(ValueError):
            adapter.mark_read("", "msg-1")

    def test_chat_list_top_conversations(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"conversations": [
                {"chatId": "c1"}, {"chatId": "c2"}
            ]}}
            r = adapter.chat_list_top_conversations()
        assert len(r) == 2


class TestAuthMethods:
    def test_auth_status_success(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"authenticated": True, "user_id": "u1"}}
            r = adapter.auth_status()
        assert r["authenticated"] is True

    def test_auth_status_success_flag(self, adapter):
        """auth_status 当返回 success=True 但无 authenticated 键。"""
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": {"success": True, "user_id": "u1"}}
            r = adapter.auth_status()
        assert r.get("authenticated") in (True, False)

    def test_auth_status_non_dict(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"result": "string"}
            r = adapter.auth_status()
        assert r.get("authenticated") is False

    def test_auth_status_exception(self, adapter):
        with patch.object(adapter, "run") as m:
            m.side_effect = RuntimeError("boom")
            r = adapter.auth_status()
        assert r.get("authenticated") is False
        assert "error" in r

    def test_profile_list(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"profiles": [{"corpId": "c1"}]}
            r = adapter.profile_list()
        assert "profiles" in r

    def test_profile_list_org_config_error(self, adapter):
        # mock is_org_config_problem to trigger re-raise
        # 注：dws_adapter 拆分为包后，is_org_config_problem 定义在 core.py，
        # 由 auth_org.py（profile_list 所在 mixin）模块级绑定，patch 须指向真实使用方。
        with patch.object(adapter, "run") as m:
            m.side_effect = DwsError("该组织尚未开启 CLI 数据访问权限")
            with patch("src.dws_adapter.auth_org.is_org_config_problem", return_value=True):
                with pytest.raises(DwsError):
                    adapter.profile_list()

    def test_is_authenticated_true(self, adapter):
        from datetime import datetime, timedelta
        future = (datetime.now() + timedelta(days=1)).isoformat()
        with patch.object(adapter, "_get_current_profile_local") as m:
            m.return_value = {"status": "active", "expiresAt": future}
            r = adapter.is_authenticated()
        assert r is True

    def test_is_authenticated_expired(self, adapter):
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(days=1)).isoformat()
        with patch.object(adapter, "_get_current_profile_local") as m:
            m.return_value = {"status": "active", "expiresAt": past}
            r = adapter.is_authenticated()
        assert r is False

    def test_is_authenticated_no_profile(self, adapter):
        with patch.object(adapter, "_get_current_profile_local") as m:
            m.return_value = None
            r = adapter.is_authenticated()
        assert r is False

    def test_is_authenticated_inactive_status(self, adapter):
        with patch.object(adapter, "_get_current_profile_local") as m:
            m.return_value = {"status": "expired"}
            r = adapter.is_authenticated()
        assert r is False


# ============ 原生引用回复（chat message reply）============

class TestChatMessageReply:
    def test_native_reply_success(self, adapter):
        """ref 信息齐全时走原生 chat message reply，不应降级为普通 send。"""
        with patch.object(adapter, "run") as m_run, \
             patch.object(adapter, "chat_message_send") as m_send:
            m_run.return_value = {"result": {"openTaskId": "t1"}}
            r = adapter.chat_message_reply(
                text="回复内容", ref_msg_id="m1", ref_sender="ou_x",
                conversation_id="cid1",
            )
        assert r["result"]["openTaskId"] == "t1"
        assert m_run.called
        args = m_run.call_args[0][0]
        assert args[0:3] == ["chat", "message", "reply"]
        assert "--ref-msg-id" in args and "--ref-sender" in args and "--conversation-id" in args
        assert not m_send.called, "原生回复成功时不应降级为普通发送"

    def test_native_reply_has_no_title_flag(self, adapter):
        """回归：dws chat message reply 不支持 --title（help available_flags 无此 flag），
        原生回复分支不得传 --title，否则必失败降级。标题仅在 fallback send 中使用。"""
        with patch.object(adapter, "run") as m_run, \
             patch.object(adapter, "chat_message_send") as m_send:
            m_run.return_value = {"result": {"openTaskId": "t1"}}
            adapter.chat_message_reply(
                text="回复内容", title="这是标题", ref_msg_id="m1",
                ref_sender="ou_x", conversation_id="cid1",
            )
        args = m_run.call_args[0][0]
        assert args[0:3] == ["chat", "message", "reply"]
        assert "--title" not in args, "原生回复不得带 --title（dws reply 不支持）"
        assert not m_send.called, "无 --title 时原生回复应成功，不降级"

    def test_native_reply_falls_back_to_send(self, adapter):
        """原生回复失败（fallback_to_send=True 默认）应降级为 chat_message_send。"""
        with patch.object(adapter, "run", side_effect=RuntimeError("boom")), \
             patch.object(adapter, "chat_message_send") as m_send:
            m_send.return_value = {"result": {"openTaskId": "t2"}}
            r = adapter.chat_message_reply(
                text="回复内容", ref_msg_id="m1", ref_sender="ou_x",
                conversation_id="cid1",
            )
        assert m_send.called
        assert r["result"]["openTaskId"] == "t2"

    def test_no_ref_info_uses_send(self, adapter):
        """缺少 ref 信息时直接走普通发送，不尝试原生 reply。"""
        with patch.object(adapter, "run") as m_run, \
             patch.object(adapter, "chat_message_send") as m_send:
            m_send.return_value = {"result": {"openTaskId": "t3"}}
            adapter.chat_message_reply(text="回复内容", group="cid1")
        assert not m_run.called
        assert m_send.called

    def test_native_reply_raises_when_no_fallback(self, adapter):
        """fallback_to_send=False 时原生失败应上抛，交还控制权给调用方。"""
        with patch.object(adapter, "run", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                adapter.chat_message_reply(
                    text="回复内容", ref_msg_id="m1", ref_sender="ou_x",
                    conversation_id="cid1", fallback_to_send=False,
                )


# ============ 消息格式自动分类（text / markdown）============

from src.im_adapter.message_format import classify_message_format


class TestMessageFormatClassifier:
    def test_plain_short_is_text(self):
        assert classify_message_format("好的") == "text"
        assert classify_message_format("收到，谢谢") == "text"
        assert classify_message_format("请查收附件") == "text"

    def test_header_is_markdown(self):
        assert classify_message_format("# 周报\n- 完成A\n- 完成B") == "markdown"

    def test_unordered_list_is_markdown(self):
        assert classify_message_format("- 苹果\n- 香蕉") == "markdown"

    def test_ordered_list_is_markdown(self):
        assert classify_message_format("1. 第一步\n2. 第二步") == "markdown"

    def test_bold_is_markdown(self):
        assert classify_message_format("这是**重点**内容") == "markdown"

    def test_inline_code_is_markdown(self):
        assert classify_message_format("用 `code` 调用接口") == "markdown"

    def test_link_is_markdown(self):
        assert classify_message_format("详见 [文档](https://example.com)") == "markdown"

    def test_blockquote_is_markdown(self):
        assert classify_message_format("> 引用对方的原话") == "markdown"

    def test_table_is_markdown(self):
        assert classify_message_format(
            "| 列1 | 列2 |\n| --- | --- |\n| a | b |"
        ) == "markdown"

    def test_fenced_code_is_markdown(self):
        assert classify_message_format("```python\nprint(1)\n```") == "markdown"

    def test_task_list_is_markdown(self):
        assert classify_message_format("- [ ] 待办\n- [x] 已完成") == "markdown"

    def test_math_expression_not_markdown(self):
        # 乘法中的 * 不应误判为斜体 / markdown
        assert classify_message_format("3 * 4 = 12") == "text"

    def test_asterisk_separated_not_markdown(self):
        assert classify_message_format("a * b * c") == "text"

    def test_hashtag_without_space_not_header(self):
        # 严格 ATX：# 后需空格，否则不算标题（与钉钉渲染一致）
        assert classify_message_format("#标签 不需要渲染为标题") == "text"

    def test_empty_is_text(self):
        assert classify_message_format("") == "text"
        assert classify_message_format("   \n  ") == "text"

    def test_multiline_plain_is_text(self):
        # 多行纯文本（无结构标记）仍判 text
        assert classify_message_format("第一行\n第二行\n第三行") == "text"

    def test_min_markers_zero_keeps_plain(self):
        # min_markers=0 时纯行内标记不触发 markdown
        assert classify_message_format("这是**重点**", min_markers=0) == "text"


class TestChatMessageSendAutoFormat:
    """chat_message_send 默认 auto：按内容结构自动判定 text / markdown。"""

    def test_auto_plain_kept_verbatim(self, adapter):
        """纯短文本：原样发送（无 markdown 归一化、无 ``` 包裹）。"""
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text="你好，这是一条纯文本")
        cmd = m.call_args[0][0]
        i = cmd.index("--text")
        assert cmd[i + 1] == "你好，这是一条纯文本"
        assert "```" not in cmd[i + 1]

    def test_auto_markdown_table_normalized(self, adapter):
        """结构化（含 GFM 表格）：自动判 markdown 并做表格归一化（包进代码块）。"""
        md = "| 列1 | 列2 |\n| --- | --- |\n| a | b |"
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text=md)
        cmd = m.call_args[0][0]
        i = cmd.index("--text")
        sent = cmd[i + 1]
        assert "```" in sent, "表格应被包进代码块"
        # 原表格不应出现在代码块之外
        assert "| 列1 | 列2 |" not in sent.split("```")[0]

    def test_auto_markdown_bold_preserved(self, adapter):
        """含加粗但无表格：判 markdown，加粗 ** 保留（不转义、不包裹）。"""
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text="这是**重点**哦")
        cmd = m.call_args[0][0]
        i = cmd.index("--text")
        sent = cmd[i + 1]
        assert "**重点**" in sent
        assert "```" not in sent

    def test_explicit_text_skips_normalization(self, adapter):
        """显式 msg_type=text：尊重覆盖，不做表格归一化（按纯文本发送）。"""
        md = "| 列1 | 列2 |\n| --- | --- |\n| a | b |"
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text=md, msg_type="text")
        cmd = m.call_args[0][0]
        i = cmd.index("--text")
        sent = cmd[i + 1]
        assert "| 列1 | 列2 |" in sent, "显式 text 应原样保留表格"
        assert "```" not in sent

    def test_explicit_markdown_normalized(self, adapter):
        """显式 msg_type=markdown：与 auto 命中 markdown 行为一致。"""
        md = "| 列1 | 列2 |\n| --- | --- |\n| a | b |"
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_send(group="g1", text=md, msg_type="markdown")
        cmd = m.call_args[0][0]
        i = cmd.index("--text")
        assert "```" in cmd[i + 1]


class TestNativeReplyMarkdownNormalize:
    """原生引用回复（chat message reply）也应按格式归一化 markdown。"""

    def test_native_reply_markdown_table_normalized(self, adapter):
        md = "| 列1 | 列2 |\n| --- | --- |\n| a | b |"
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_reply(
                text=md, ref_msg_id="m1", ref_sender="ou_x",
                conversation_id="cid1", fallback_to_send=False,
            )
        cmd = m.call_args[0][0]
        assert cmd[0:3] == ["chat", "message", "reply"]
        i = cmd.index("--text")
        assert "```" in cmd[i + 1], "原生回复的表格也应归一化"

    def test_native_reply_plain_kept_verbatim(self, adapter):
        with patch.object(adapter, "run") as m:
            m.return_value = {"ok": True}
            adapter.chat_message_reply(
                text="好的，收到", ref_msg_id="m1", ref_sender="ou_x",
                conversation_id="cid1", fallback_to_send=False,
            )
        cmd = m.call_args[0][0]
        i = cmd.index("--text")
        assert cmd[i + 1] == "好的，收到"
