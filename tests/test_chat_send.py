"""send_message 多格式支持单元测试。

分层覆盖：
- 适配器 DwsAdapter.chat_message_send（mock 其底层 run，捕获最终命令行参数）：
  * 各 msg_type 的 flag 映射（text/image/file/audio/video）
  * @ 占位符注入：at_open_dingtalk_ids 自动补 <@id>；at_all 交由 dws（不重复注入）
- 工具 SendMessageTool.execute（mock 整个 dws）：
  * 参数校验：各 msg_type 必填项（text / media_id / file_path）
  * 单聊目标解析优先级：peer_open_dingtalk_id > peer_user_id > chat_id 本身
  * msg_type 透传
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

from src.dws_adapter import DwsAdapter
from src.tools.chat import SendMessageTool


# ---------- 适配器层：flag 映射 + @ 占位符注入 ----------

def _adapter():
    dws = DwsAdapter.__new__(DwsAdapter)
    dws.dry_run = False
    dws.ai_tag_default = True
    dws.run = MagicMock(return_value={"success": True})
    return dws


def _sent_args(dws):
    return dws.run.call_args[0][0]


def test_text_flag_mapping():
    dws = _adapter()
    dws.chat_message_send(group="G", title="T", text="hi")
    a = _sent_args(dws)
    assert "--text" in a and a[a.index("--text") + 1] == "hi"
    assert "--title" in a and a[a.index("--title") + 1] == "T"
    assert "--group" in a


def test_image_flag_mapping():
    dws = _adapter()
    dws.chat_message_send(group="G", msg_type="image", media_id="MID", text="看图")
    a = _sent_args(dws)
    assert "--msg-type" in a and a[a.index("--msg-type") + 1] == "image"
    assert "--media-id" in a and a[a.index("--media-id") + 1] == "MID"


def test_file_audio_video_flag_mapping():
    dws = _adapter()
    for mt in ("file", "audio", "video"):
        dws.chat_message_send(group="G", msg_type=mt, file_path="/tmp/x")
        a = _sent_args(dws)
        assert a[a.index("--msg-type") + 1] == mt
        assert a[a.index("--file-path") + 1] == "/tmp/x"


def test_at_open_dingtalk_ids_injects_placeholder():
    dws = _adapter()
    dws.chat_message_send(group="G", text="hi", at_open_dingtalk_ids="oidA,oidB")
    a = _sent_args(dws)
    sent_text = a[a.index("--text") + 1]
    assert "<@oidA>" in sent_text and "<@oidB>" in sent_text
    assert a[a.index("--at-open-dingtalk-ids") + 1] == "oidA,oidB"


def test_at_all_not_double_injected():
    dws = _adapter()
    dws.chat_message_send(group="G", text="通知", at_all=True)
    a = _sent_args(dws)
    sent_text = a[a.index("--text") + 1]
    # at_all 由 dws 自行注入，适配器不应重复加 <@all>
    assert "<@all>" not in sent_text
    assert "--at-all" in a


def test_image_requires_media_id():
    dws = _adapter()
    try:
        dws.chat_message_send(group="G", msg_type="image", text="x")
        raise AssertionError("应抛 ValueError")
    except ValueError as e:
        assert "media_id" in str(e)


def test_file_requires_file_path():
    dws = _adapter()
    try:
        dws.chat_message_send(group="G", msg_type="file", text="x")
        raise AssertionError("应抛 ValueError")
    except ValueError as e:
        assert "file_path" in str(e)


# ---------- 工具层：校验 + 单聊目标解析 + 透传 ----------

def _make_tool(chat_type="group", peer_oid="", peer_user_id=""):
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    store = MagicMock()
    # 默认该 chat_id 不在黑名单（走正常发送路径）；个别用例可单独覆盖。
    store._blacklist_repo.is_conversation_blocked.return_value = False
    store._blacklist_repo.list_blocked_conversations.return_value = []
    if chat_type == "single":
        store._conversation_repo.get_conversation.return_value = {
            "peer_open_dingtalk_id": peer_oid,
            "peer_user_id": peer_user_id,
        }
    else:
        store._conversation_repo.get_conversation.return_value = {"chat_name": "测试群"}
    return SendMessageTool(dws=dws, store=store), dws


def test_text_requires_text():
    tool, dws = _make_tool()
    r = tool.execute({"chat_id": "G1", "chat_type": "group", "msg_type": "text", "text": ""})
    assert "error" in r
    r = tool.execute({"chat_id": "G1", "chat_type": "group", "text": "你好"})
    assert r.get("success") is True
    _, kw = dws.chat_message_send.call_args
    # 默认（不显式传 msg_type）走 auto：由适配器按内容结构自动判定 text/markdown
    assert kw["msg_type"] is None or kw["msg_type"] in ("text", "auto")


def test_image_tool_requires_media_or_file():
    tool, dws = _make_tool()
    r = tool.execute({"chat_id": "G1", "chat_type": "group", "msg_type": "image", "text": "x"})
    assert "error" in r
    r = tool.execute({"chat_id": "G1", "chat_type": "group", "msg_type": "image",
                      "media_id": "MID", "text": "看图"})
    assert r.get("success") is True
    _, kw = dws.chat_message_send.call_args
    assert kw["msg_type"] == "image" and kw["media_id"] == "MID"


def test_file_audio_video_tool_require_file_path():
    # 【P0-3 护栏】不同 msg_type 用不同 chat_id 隔离，防 10s 频次护栏串扰。
    # 校验层要求 file_path 真实存在（os.path.isfile），故先建一个真实探针文件。
    probe = "/tmp/_probe_chat_send.png"
    try:
        with open(probe, "w") as _f:
            _f.write("probe")
        for mt in ("file", "audio", "video"):
            chat_id = f"G_{mt}"
            tool, dws = _make_tool()
            r = tool.execute({"chat_id": chat_id, "chat_type": "group", "msg_type": mt, "text": "x"})
            assert "error" in r, mt
            r = tool.execute({"chat_id": chat_id, "chat_type": "group", "msg_type": mt,
                              "file_path": probe})
            assert r.get("success") is True, mt
            _, kw = dws.chat_message_send.call_args
            assert kw["msg_type"] == mt and kw["file_path"] == probe
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def test_single_resolves_target_priority():
    tool, dws = _make_tool(chat_type="single", peer_oid="O1", peer_user_id="U1")
    tool.execute({"chat_id": "C1", "chat_type": "single", "text": "hi"})
    _, kw = dws.chat_message_send.call_args
    assert kw.get("open_dingtalk_id") == "O1" and kw.get("user") is None

    tool2, dws2 = _make_tool(chat_type="single", peer_oid="", peer_user_id="U1")
    tool2.execute({"chat_id": "C1", "chat_type": "single", "text": "hi"})
    _, kw2 = dws2.chat_message_send.call_args
    assert kw2.get("user") == "U1"

    tool3, dws3 = _make_tool(chat_type="single", peer_oid="", peer_user_id="")
    tool3.execute({"chat_id": "C1", "chat_type": "single", "text": "hi"})
    _, kw3 = dws3.chat_message_send.call_args
    assert kw3.get("open_dingtalk_id") == "C1"


# ---------- 护栏 P0-3：防自发自 + 防短时重复 ----------

def test_self_user_id_blocks_sending_to_self():
    """【P0-3 护栏】chat_id == self_user_id 时必须拒绝。"""
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    tool = SendMessageTool(dws=dws, store=None, self_user_id="robot_oid_123")
    r = tool.execute({"chat_id": "robot_oid_123", "chat_type": "single", "text": "发给自己？"})
    assert "error" in r
    assert "自" in r["error"] or "self" in r["error"].lower()
    dws.chat_message_send.assert_not_called()


def test_self_user_id_empty_disables_guard():
    """【P0-3 护栏】self_user_id 为空时不启用护栏（优雅降级）。"""
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    tool = SendMessageTool(dws=dws, store=None, self_user_id="")
    r = tool.execute({"chat_id": "any_oid", "chat_type": "single", "text": "hi"})
    assert r.get("success") is True
    dws.chat_message_send.assert_called_once()


def test_repeated_send_to_same_chat_within_10s_blocked():
    """【P0-3 护栏】同一 chat_id 10 秒内发 ≥3 次 → 拒绝。"""
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    tool = SendMessageTool(dws=dws, store=None, self_user_id="")
    # 第 1、2 次应成功
    r1 = tool.execute({"chat_id": "C1", "chat_type": "group", "text": "msg1"})
    r2 = tool.execute({"chat_id": "C1", "chat_type": "group", "text": "msg2"})
    assert r1.get("success") is True and r2.get("success") is True
    # 第 3 次应被护栏拒绝
    r3 = tool.execute({"chat_id": "C1", "chat_type": "group", "text": "msg3"})
    assert "error" in r3
    assert "频次" in r3["error"] or "重复" in r3["error"] or "回声" in r3["error"]
    assert dws.chat_message_send.call_count == 2  # 仅前 2 次实际调用 dws


def test_different_chat_ids_not_blocked():
    """【P0-3 护栏】不同 chat_id 各自有计数，互不影响。"""
    dws = MagicMock()
    dws.chat_message_send.return_value = {"success": True}
    tool = SendMessageTool(dws=dws, store=None, self_user_id="")
    for i in range(5):
        r = tool.execute({"chat_id": f"C{i}", "chat_type": "group", "text": f"msg{i}"})
        assert r.get("success") is True
    assert dws.chat_message_send.call_count == 5


# ============================================================
# 飞书跨租户 230027/230002 黑名单降级 (含 is_external 门限)
# ============================================================

def _store_with(conv_map: dict, ext_oids: set):
    """构造 SQLiteStore，预填 conversations + external_friends。

    conv_map: {chat_id: (peer_oid, peer_uid)}
    ext_oids: ou_xxx 集合，属于 external_friends 表
    """
    from src.memory.sqlite_store import SQLiteStore
    s = SQLiteStore(":memory:")
    for oid in ext_oids:
        s._external_friend_repo.add_external_friend(f"外-{oid}", oid, f"oc_for_{oid}")
    for cid, (oid, uid) in conv_map.items():
        s._conversation_repo.upsert_conversation(chat_id=cid, chat_name="c", chat_type="single",
                              peer_open_dingtalk_id=oid, peer_user_id=uid)
    # 清除黑名单缓存和文件级会话库中的残留黑名单数据（BlacklistRepo._cc()
    # 走 conv_conn 打开文件级会话库，可能残留之前测试写入的数据）。
    s._blacklist_repo._cache.clear()
    s._blacklist_repo._cache_loaded = False
    try:
        c = s.conv_conn("dingtalk")
        c.execute("DELETE FROM blocked_conversations")
        c.commit()
        # 不关闭 c，供后续测试使用
    except Exception:
        pass
    return s


def test_external_230027_blocked_and_persisted():
    """external chat 抛 230027 → degraded=True + 写入黑名单。"""
    from src.tools.chat import SendMessageTool
    store = _store_with({"oc_outer": ("ou_outer_1", "u1")}, ext_oids={"ou_outer_1"})
    dws = MagicMock()
    dws.chat_message_send.side_effect = RuntimeError(
        'lark-cli exit 3: code 230027 access denied external-chat policy')
    tool = SendMessageTool(dws=dws, store=store)
    r = tool.execute({"chat_id": "oc_outer", "chat_type": "single", "text": "hi"})
    assert r.get("degraded") is True
    assert "230027" in r["error"] or "黑名单" in r["error"]
    blocked = store._blacklist_repo.list_blocked_conversations()
    assert len(blocked) == 1
    assert blocked[0]["chat_id"] == "oc_outer"
    assert blocked[0]["source"] == "feishu_external_chat_unsendable"


def test_blocked_chat_skips_dws_on_retry():
    """已在黑名单的 chat_id 二次调用 → dws 不被调, 直接返回 degraded 提示。"""
    from src.tools.chat import SendMessageTool
    store = _store_with({"oc_outer": ("ou_outer_1", "u1")}, ext_oids={"ou_outer_1"})
    dws = MagicMock()
    dws.chat_message_send.side_effect = RuntimeError("code 230027 access denied")
    tool = SendMessageTool(dws=dws, store=store)
    tool.execute({"chat_id": "oc_outer", "chat_type": "single", "text": "first"})
    dws.chat_message_send.reset_mock()
    dws.chat_message_send.return_value = {"ok": True}
    r = tool.execute({"chat_id": "oc_outer", "chat_type": "single", "text": "second"})
    assert r.get("degraded") is True
    assert dws.chat_message_send.called is False, "已黑名单的会话不应再调 dws"
    # 新逻辑：1/2 次失败仅临时冷却（不再一律永久黑名单）
    assert "本会话发送失败 1 次（最大 3 次）" in r["reason"]
    assert "分身暂不代发" in r["reason"]


def test_non_external_230027_not_blocked():
    """非 external chat 即使抛 230027 也不进黑名单（普通错误, 由 LLM 重试）"""
    from src.tools.chat import SendMessageTool
    store = _store_with({"oc_inner": ("ou_inner_x", "u2")}, ext_oids=set())  # 内部人
    dws = MagicMock()
    dws.chat_message_send.side_effect = RuntimeError("code 230027 access denied")
    tool = SendMessageTool(dws=dws, store=store)
    r = tool.execute({"chat_id": "oc_inner", "chat_type": "single", "text": "hi"})
    assert r.get("degraded") is None or r.get("degraded") is False
    assert "error" in r
    assert "黑名单" not in r.get("error", "")
    assert store._blacklist_repo.list_blocked_conversations() == []


def test_230002_with_external_blocked():
    """230002 (bot 不在 p2p) + external → 同样降级（与 230027 一样判定）"""
    from src.tools.chat import SendMessageTool
    store = _store_with({"oc_outer2": ("ou_outer_2", "u3")}, ext_oids={"ou_outer_2"})
    dws = MagicMock()
    dws.chat_message_send.side_effect = RuntimeError(
        "code 230002 Bot/User can NOT be out of the chat")
    tool = SendMessageTool(dws=dws, store=store)
    r = tool.execute({"chat_id": "oc_outer2", "chat_type": "single", "text": "hi"})
    assert r.get("degraded") is True
    assert len(store._blacklist_repo.list_blocked_conversations()) == 1


# ============================================================
# 飞书身份降级：user 失败 → 自动用 bot 身份重试
# ============================================================

class _FakeAdapter:
    """模拟 FeishuCliAdapter：记录每次 chat_message_send 的 args 和结果。

    默认 user 身份抛 230027；可配置第二次（bot）成功。
    """
    def __init__(self, *, second_call_ok=True, second_call_code=0):
        self._disable_bot_fallback = False
        self._in_fallback = False
        self.calls = []  # [(args副本, 异常或返回值)]
        self._second_call_ok = second_call_ok
        self._second_call_code = second_call_code

    def chat_message_send(self, **kwargs):
        # 复制 args 以便断言
        self.calls.append(dict(kwargs))
        n = len(self.calls)
        if n == 1:
            # 第一次（user）— 抛 230027
            raise RuntimeError(
                'lark-cli exit 3: {"ok":false,"identity":"user","error":'
                '{"code":230027,"message":"access denied... user_unauthorized"}}'
            ) from None
        # 第二次（bot 身份）— 按配置
        if self._second_call_ok:
            return {"ok": True, "identity": "bot", "data": {"message_id": "om_bot_ok"}}
        raise RuntimeError(f"second call failed with code {self._second_call_code}")


def test_user_fails_bot_succeeds_degrades_silently():
    """feishu adapter: user 身份失败 → 自动用 bot 身份重试 → 拿到 bot 成功结果。"""
    from src.im_adapter.feishu import FeishuCliAdapter
    a = FeishuCliAdapter(cli_path="lark-cli", dry_run=False)
    a._disable_bot_fallback = False
    a._in_fallback = False

    calls = []
    def fake_run(args, **kw):
        calls.append(list(args))
        if len(calls) == 1:
            raise RuntimeError(
                '{"ok":false,"identity":"user","error":{"code":230027,'
                '"message":"user_unauthorized... access denied"}}'
            ) from None
        # 第二次 --as bot 调用，返成功
        return {"ok": True, "identity": "bot",
                "data": {"chat_id": "oc_x", "message_id": "om_bot_ok"}}
    a.run = fake_run
    r = a.chat_message_send(group="oc_x", text="hi", uuid="u1")
    assert r["data"]["message_id"] == "om_bot_ok"
    # user 调了一次（无 --as）, bot 调了一次（带 --as bot）
    assert len(calls) == 2
    assert "--as" not in calls[0] or "bot" not in calls[0]
    assert "--as" in calls[1] and "bot" in calls[1]


def test_both_fail_returns_error_no_retry_loop():
    """feishu adapter: user + bot 都失败 → 只调 2 次（不无限重试）→ 再次异常冒泡。"""
    from src.im_adapter.feishu import FeishuCliAdapter
    a = FeishuCliAdapter(cli_path="lark-cli", dry_run=False)
    a._disable_bot_fallback = False
    a._in_fallback = False
    calls = []
    def fake_run(args, **kw):
        calls.append(list(args))
        raise RuntimeError(
            '{"ok":false,"error":{"code":230027,"message":"user_unauthorized"}}'
        ) from None
    a.run = fake_run
    with __import__("pytest").raises(RuntimeError):
        a.chat_message_send(group="oc_x", text="hi", uuid="u2")
    assert len(calls) == 2  # user + bot 一次，总共 2 次


def test_disable_flag_skips_bot_fallback():
    """is_external=True 时 chat.py 应设置 _disable_bot_fallback=True，feishu adapter 不 fallback。"""
    from src.tools.chat import SendMessageTool
    from src.memory.sqlite_store import SQLiteStore

    class _CountAdapter(_FakeAdapter):
        pass

    dws = _CountAdapter(second_call_ok=True)
    store = SQLiteStore(":memory:")
    store._external_friend_repo.add_external_friend("周强强", "ou_ext_1", "oc_99e7cb82efc36abffcd4e8b46eb80728")
    store._conversation_repo.upsert_conversation(chat_id="oc_99e7cb82efc36abffcd4e8b46eb80728",
                              chat_name="周强强", chat_type="single",
                              peer_open_dingtalk_id="ou_ext_1", peer_user_id="uid-1")
    # 清除文件级会话库中的残留黑名单数据
    store._blacklist_repo._cache.clear()
    store._blacklist_repo._cache_loaded = False
    try:
        c = store.conv_conn("dingtalk")
        c.execute("DELETE FROM blocked_conversations")
        c.commit()
    except Exception:
        pass
    tool = SendMessageTool(dws=dws, store=store)
    r = tool.execute({"chat_id": "oc_99e7cb82efc36abffcd4e8b46eb80728",
                      "chat_type": "single", "text": "hi"})
    # 跨租户场景: 即使 user 失败 + bot 成功 也不应 fallback（避免对外发消息）
    # 实际只调了 1 次（user 失败），fallback 被禁用，结果是 error+degraded
    assert len(dws.calls) == 1
    assert r.get("degraded") is True
    # 写入了黑名单
    assert len(store._blacklist_repo.list_blocked_conversations()) == 1
    # _disable_bot_fallback 已被还原
    assert dws._disable_bot_fallback is False
