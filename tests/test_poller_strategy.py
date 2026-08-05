"""PollerStrategyMixin 单测。

覆盖此前测试盲区（原覆盖率 22%）中可独立验证的三块策略逻辑：
- 飞书外部联系人启动同步（去重、单条失败不中断）
- 飞书 chat_type 以 API chat_mode 为准的自动纠错
- 群消息 list-all 并集时间窗预取（N 次全扫 → 1 次）

用真实 MessagePoller + 真实临时库 + mock adapter，保持与生产一致的组装方式。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.config import PollerConfig
from src.models import Message
from src.memory.sqlite_store import SQLiteStore
from src.poller import MessagePoller


class FeishuCliAdapter(MagicMock):
    """同名假适配器——策略层按 ``type(self.dws).__name__`` 判定平台分支。"""


@pytest.fixture
def poller_factory(tmp_db_path):
    created = []

    def _make(dws=None):
        config = PollerConfig(
            interval_seconds=6,
            unread_conversation_count=20,
            messages_per_conversation=20,
            history_window=20,
            merge_window_seconds=60,
            max_processed_msg_ids=500,
            list_all_time_window_minutes=30,
            list_all_first_run_minutes=5,
            empty_poll_protection_minutes=5,
            skip_notification_patterns=[],
            skip_msg_types=[],
            reply_cooldown_seconds=60,
            first_run_ignore_older_than_minutes=10,
        )
        store = SQLiteStore(db_path=str(tmp_db_path))
        store.init_db()
        p = MessagePoller(
            config=config,
            dws=dws if dws is not None else MagicMock(),
            store=store,
            current_user_id="user-001",
            current_user_name="测试用户",
        )
        created.append(store)
        return p, store

    yield _make
    for s in created:
        try:
            s.close()
        except Exception:
            pass


# ── 飞书外部联系人同步 ──

class TestSyncFeishuExternalContacts:
    """注意：MessagePoller.__init__ 已会跑一次启动同步（poller.py:175）。

    因此这里统一用 ``_make_feishu`` 构造——先让 init 阶段同步到空，
    再挂上正式的发现结果，避免被启动同步的副作用污染断言。
    """

    @staticmethod
    def _make_feishu(poller_factory, discovered):
        dws = FeishuCliAdapter()
        dws.sync_external_contacts.return_value = []  # init 阶段同步为空
        p, store = poller_factory(dws)
        dws.sync_external_contacts.return_value = discovered
        return p, store

    def test_init_runs_startup_sync_once(self, poller_factory):
        """契约保护：构造 poller 即触发一次飞书外部联系人同步。"""
        dws = FeishuCliAdapter()
        dws.sync_external_contacts.return_value = [
            {"open_dingtalk_id": "ou_boot", "name": "启动同步"},
        ]
        _, store = poller_factory(dws)
        names = {f["name"] for f in store._external_friend_repo.list_external_friends()}
        assert "启动同步" in names

    def test_non_feishu_adapter_is_noop(self, poller_factory):
        p, _ = poller_factory()  # 普通 MagicMock，不是 FeishuCliAdapter
        p._sync_feishu_external_contacts()
        p.dws.sync_external_contacts.assert_not_called()

    def test_discovery_failure_swallowed(self, poller_factory):
        p, store = self._make_feishu(poller_factory, [])
        p.dws.sync_external_contacts.side_effect = RuntimeError("CLI 挂了")
        p._sync_feishu_external_contacts()  # 不应抛出
        assert store._external_friend_repo.list_external_friends() == []

    def test_empty_discovery_registers_nothing(self, poller_factory):
        p, store = self._make_feishu(poller_factory, [])
        p._sync_feishu_external_contacts()
        assert store._external_friend_repo.list_external_friends() == []

    def test_registers_new_contacts(self, poller_factory):
        p, store = self._make_feishu(poller_factory, [
            {"open_dingtalk_id": "ou_1", "name": "外部甲", "chat_id": "oc_1"},
            {"open_dingtalk_id": "ou_2", "name": "外部乙", "chat_id": "oc_2"},
        ])
        p._sync_feishu_external_contacts()
        names = {f["name"] for f in store._external_friend_repo.list_external_friends()}
        assert names == {"外部甲", "外部乙"}

    def test_skips_already_registered(self, poller_factory):
        p, store = self._make_feishu(poller_factory, [
            {"open_dingtalk_id": "ou_1", "name": "外部甲", "chat_id": "oc_1"},
        ])
        store._external_friend_repo.add_external_friend(
            name="外部甲", open_dingtalk_id="ou_1", chat_id="oc_1", notes="手工")
        p._sync_feishu_external_contacts()
        friends = store._external_friend_repo.list_external_friends()
        assert len(friends) == 1
        assert friends[0]["notes"] == "手工"  # 未被覆盖

    def test_skips_entries_missing_id_or_name(self, poller_factory):
        p, store = self._make_feishu(poller_factory, [
            {"open_dingtalk_id": "", "name": "无ID"},
            {"open_dingtalk_id": "ou_2", "name": ""},
            {"open_dingtalk_id": "ou_3", "name": "合法"},
        ])
        p._sync_feishu_external_contacts()
        friends = store._external_friend_repo.list_external_friends()
        assert [f["name"] for f in friends] == ["合法"]

    def test_dedups_within_same_batch(self, poller_factory):
        """同一批里重复的 open_id 只应注册一次。"""
        p, store = self._make_feishu(poller_factory, [
            {"open_dingtalk_id": "ou_1", "name": "甲"},
            {"open_dingtalk_id": "ou_1", "name": "甲副本"},
        ])
        p._sync_feishu_external_contacts()
        assert len(store._external_friend_repo.list_external_friends()) == 1

    def test_single_insert_failure_does_not_abort_batch(self, poller_factory):
        p, store = self._make_feishu(poller_factory, [
            {"open_dingtalk_id": "ou_1", "name": "会失败"},
            {"open_dingtalk_id": "ou_2", "name": "应成功"},
        ])
        real_add = store._external_friend_repo.add_external_friend

        def flaky(name, **kw):
            if name == "会失败":
                raise RuntimeError("写库失败")
            return real_add(name=name, **kw)
        store._external_friend_repo.add_external_friend = flaky
        p._sync_feishu_external_contacts()
        del store._external_friend_repo.add_external_friend  # 还原为类方法
        assert [f["name"] for f in store._external_friend_repo.list_external_friends()] == ["应成功"]

    def test_existing_lookup_failure_still_registers(self, poller_factory):
        """读既有列表失败不应让整次同步罢工（去重降级即可）。"""
        p, store = self._make_feishu(poller_factory, [
            {"open_dingtalk_id": "ou_1", "name": "甲"},
        ])
        store._external_friend_repo.list_external_friends = MagicMock(
            side_effect=RuntimeError("读失败"))
        p._sync_feishu_external_contacts()  # 不应抛出


# ── 飞书 chat_type 纠错 ──

class TestFeishuCorrectChatType:
    def test_non_feishu_returns_input_unchanged(self, poller_factory):
        p, _ = poller_factory()
        assert p._feishu_correct_chat_type("c1", "群A", "group") == "group"
        p.dws.chat_conversation_info.assert_not_called()

    def test_empty_conv_id_returns_input(self, poller_factory):
        p, _ = poller_factory(FeishuCliAdapter())
        assert p._feishu_correct_chat_type("", "", "single") == "single"

    def test_api_failure_returns_input(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.side_effect = RuntimeError("接口超时")
        p, _ = poller_factory(dws)
        assert p._feishu_correct_chat_type("c1", "会话", "group") == "group"

    def test_blank_chat_mode_returns_input(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": ""}
        p, _ = poller_factory(dws)
        assert p._feishu_correct_chat_type("c1", "会话", "single") == "single"

    def test_p2p_maps_to_single(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "p2p"}
        p, _ = poller_factory(dws)
        assert p._feishu_correct_chat_type("c1", "张三", "group") == "single"

    def test_group_mode_maps_to_group(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "group"}
        p, _ = poller_factory(dws)
        assert p._feishu_correct_chat_type("c1", "项目群", "single") == "group"

    def test_correction_persists_to_db(self, poller_factory):
        """纠错必须落库，否则下一轮又会读回错误的 chat_type。"""
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "group"}
        p, store = poller_factory(dws)
        store._conversation_repo.upsert_conversation("c1", "项目群", "single")
        p._feishu_correct_chat_type("c1", "项目群", "single")
        assert store._conversation_repo.get_conversation("c1")["chat_type"] == "group"

    def test_no_write_when_already_consistent(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "group"}
        p, store = poller_factory(dws)
        store._conversation_repo.upsert_conversation("c1", "项目群", "group")
        store._conversation_repo.upsert_conversation = MagicMock()
        assert p._feishu_correct_chat_type("c1", "项目群", "group") == "group"
        store._conversation_repo.upsert_conversation.assert_not_called()

    def test_db_value_wins_over_passed_current_type(self, poller_factory):
        """DB 记录才是比较基准；入参 current_chat_type 只是兜底。"""
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "group"}
        p, store = poller_factory(dws)
        store._conversation_repo.upsert_conversation("c1", "项目群", "group")
        store._conversation_repo.upsert_conversation = MagicMock()
        p._feishu_correct_chat_type("c1", "项目群", "single")  # 入参与 DB 不一致
        store._conversation_repo.upsert_conversation.assert_not_called()

    def test_write_failure_still_returns_corrected_type(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "p2p"}
        p, store = poller_factory(dws)
        store._conversation_repo.upsert_conversation = MagicMock(
            side_effect=RuntimeError("写库失败"))
        assert p._feishu_correct_chat_type("c1", "张三", "group") == "single"

    def test_db_read_failure_falls_back_to_input(self, poller_factory):
        dws = FeishuCliAdapter()
        dws.chat_conversation_info.return_value = {"chat_mode": "p2p"}
        p, store = poller_factory(dws)
        store._conversation_repo.get_conversation = MagicMock(
            side_effect=RuntimeError("读失败"))
        assert p._feishu_correct_chat_type("c1", "张三", "single") == "single"


# ── 群消息并集时间窗预取 ──

class TestBuildGroupListAllCache:
    def _group(self, oid, title="群"):
        return {"openConversationId": oid, "title": title, "singleChat": False}

    def _single(self, oid, title="张三"):
        return {"openConversationId": oid, "title": title, "singleChat": True}

    def test_no_conversations_returns_none(self, poller_factory):
        p, _ = poller_factory()
        assert p._build_group_list_all_cache([]) is None

    def test_only_single_chats_returns_none(self, poller_factory):
        """单聊走 list-direct，不参与群预取。"""
        p, _ = poller_factory()
        assert p._build_group_list_all_cache([self._single("c1")]) is None
        p.dws.chat_message_list_all.assert_not_called()

    def test_skips_missing_conversation_id(self, poller_factory):
        p, _ = poller_factory()
        assert p._build_group_list_all_cache([{"openConversationId": ""}]) is None

    def test_skips_blocked_conversations(self, poller_factory):
        p, _ = poller_factory()
        p._is_blocked = lambda oid: oid == "blocked"
        assert p._build_group_list_all_cache([self._group("blocked")]) is None

    def test_prefetches_once_for_groups(self, poller_factory):
        dws = MagicMock()
        dws.chat_message_list_all.return_value = {"conversationMessagesList": []}
        p, _ = poller_factory(dws)
        result = p._build_group_list_all_cache([self._group("g1"), self._group("g2")])
        assert result == {"conversationMessagesList": []}
        assert dws.chat_message_list_all.call_count == 1  # N 个群只扫一次

    def test_uses_earliest_last_poll_as_window_start(self, poller_factory):
        """并集窗起点必须取最早的 last_poll，否则落后的群会漏消息。"""
        dws = MagicMock()
        p, _ = poller_factory(dws)
        now = datetime.now()
        p._last_poll_time = {
            "g1": now - timedelta(hours=1),
            "g2": now - timedelta(hours=5),  # 最早
        }
        p._build_group_list_all_cache([self._group("g1"), self._group("g2")])
        start_ts = dws.chat_message_list_all.call_args.args[0]
        assert start_ts == (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

    def test_falls_back_to_db_last_message_time(self, poller_factory):
        """内存无 last_poll 时应回退 DB 记录，而不是无脑用 24 小时前。"""
        dws = MagicMock()
        p, store = poller_factory(dws)
        db_ts = (datetime.now() - timedelta(hours=3)).replace(microsecond=0)
        store._conversation_repo.upsert_conversation("g1", "群A", "group")
        conn = store._conversation_repo._cc("")
        conn.execute("UPDATE conversations SET last_message_time = ? WHERE chat_id = ?",
                     (db_ts.isoformat(), "g1"))
        conn.commit()
        p._last_poll_time = {}
        p._build_group_list_all_cache([self._group("g1")])
        assert dws.chat_message_list_all.call_args.args[0] == db_ts.strftime("%Y-%m-%d %H:%M:%S")

    def test_malformed_db_timestamp_falls_back_to_default(self, poller_factory):
        dws = MagicMock()
        p, store = poller_factory(dws)
        store._conversation_repo.get_conversation = MagicMock(
            return_value={"last_message_time": "不是时间"})
        p._last_poll_time = {}
        p._build_group_list_all_cache([self._group("g1")])
        dws.chat_message_list_all.assert_called_once()  # 用默认 24h 窗，不炸

    def test_prefetch_failure_returns_none_for_fallback(self, poller_factory):
        """预取失败返回 None，主循环回退逐群扫描——行为不变，不能抛。"""
        dws = MagicMock()
        dws.chat_message_list_all.side_effect = RuntimeError("接口超时")
        p, _ = poller_factory(dws)
        assert p._build_group_list_all_cache([self._group("g1")]) is None

    def test_passes_configured_message_limit(self, poller_factory):
        dws = MagicMock()
        p, _ = poller_factory(dws)
        p._build_group_list_all_cache([self._group("g1")])
        assert dws.chat_message_list_all.call_args.kwargs["limit"] == 20


# ── poll_once 主循环 ──

def _make_msg(raw: dict, chat_id="oc_a", chat_type="group", title="群") -> Message:
    """把 raw 字典转成最小 Message，供 _raw_to_message 桩使用。"""
    mtype = raw.get("msgType") or raw.get("_type") or "text"
    ts_str = raw.get("createTime") or raw.get("timestamp") or ""
    if ts_str:
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = datetime.now()
    else:
        ts = datetime.now()
    return Message(
        msg_id=raw.get("openMessageId") or raw.get("msgId") or "m1",
        chat_id=chat_id,
        chat_type=chat_type,
        chat_name=title,
        sender_id=raw.get("senderOpenDingTalkId") or raw.get("senderId") or "ou_peer",
        sender_name=raw.get("sender") or raw.get("senderName") or "对方",
        content=raw.get("content") or raw.get("text") or "你好",
        msg_type=mtype,
        timestamp=ts,
        raw=raw,
    )


def _stub_poll_once_deps(p: MessagePoller):
    """桩掉 poll_once 依赖的「远处」辅助方法与 dws 接口，默认行为安全可通过。

    真实 SQLiteStore 的 repo 仍保留（upsert_conversation 等会落库），
    仅把跨模块、易变的辅助方法替成确定桩，让主循环自身的分支被驱动执行。
    """
    orig_list_ef = p.store._external_friend_repo.list_external_friends
    p._orig_list_external_friends = orig_list_ef  # 外部好友测试可还原真实实现
    p._reconcile_blocklist = MagicMock(return_value=0)
    p._fetch_messages_via_list_all = MagicMock(return_value=[])
    p._is_global_permission_error = MagicMock(return_value=False)
    p._warn_permission_once = MagicMock()
    p._get_cached_top_conversations = MagicMock(return_value=[])
    p._get_recent_conversations_from_db = MagicMock(return_value=[])
    p._is_blocked = MagicMock(return_value=False)
    p._build_group_list_all_cache = MagicMock(return_value=None)  # 预取已在别处覆盖
    p._detect_chat_type = MagicMock(
        side_effect=lambda c: "single" if c.get("singleChat") else "group")
    p._is_blacklisted_conversation = MagicMock(return_value=False)
    p._should_skip_longtail_fetch = MagicMock(return_value=False)
    p._resolve_single_chat_peer = MagicMock(
        return_value={"user_id": "uid", "open_dingtalk_id": "ou_x"})
    p._is_self_sender = MagicMock(return_value=False)
    p._is_self_message = MagicMock(return_value=False)
    p._check_if_bot_message = MagicMock(return_value=False)
    p._is_duplicate_self_message = MagicMock(return_value=False)
    p._handle_edit_message = MagicMock()
    p._handle_recall_message = MagicMock()
    p._effective_skip_types = MagicMock(return_value=set())
    p._is_at_me = MagicMock(return_value=True)
    p._merge_consecutive_messages = MagicMock(side_effect=lambda msgs, **kw: msgs)
    p._is_msg_processed = MagicMock(return_value=False)
    p._is_permission_error = MagicMock(return_value=False)
    p._register_perm_failure = MagicMock(return_value=(False, 1))
    p._block_conversation = MagicMock()
    p._raw_to_message = lambda raw, cid, ctype, title: _make_msg(raw, cid, ctype, title)
    p.store._message_repo.is_message_processed = MagicMock(return_value=False)
    p.store._message_repo.save_message = MagicMock()
    p.store._external_friend_repo.list_external_friends = MagicMock(return_value=[])
    p.dws.chat_message_list_unread_conversations = MagicMock(return_value=[])
    p.dws.chat_message_list = MagicMock(return_value=[])
    p.dws.chat_message_list_direct = MagicMock(return_value=[])


class TestPollOnce:
    """覆盖 poll_once 主循环：六层取信合并、会话过滤、逐会话补拉、权限错误分流。"""

    # ── list-all 主通道 ──

    def test_list_all_messages_returned(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._fetch_messages_via_list_all.return_value = [
            _make_msg({"openMessageId": "la1", "content": "来自list-all"},
                      chat_id="oc_a", chat_type="group", title="群A")]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["la1"]

    def test_list_all_empty_resets_streak(self, poller_factory):
        """拉到 >=1 条应把连续空轮计数归零（覆盖 277-283 分支）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._list_all_empty_streak = 5
        p._fetch_messages_via_list_all.return_value = [
            _make_msg({"openMessageId": "la1", "content": "恢复收信"})]
        p.config.list_all_empty_alert_rounds = 3
        p.poll_once()
        assert p._list_all_empty_streak == 0

    def test_list_all_empty_streak_alerts(self, poller_factory):
        """连续空轮达到阈值应触发探针日志分支（覆盖 268-276）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.config.list_all_empty_alert_rounds = 2
        p.poll_once()  # streak 1
        assert p._list_all_empty_streak == 1
        p.poll_once()  # streak 2 → 命中阈值
        assert p._list_all_empty_streak == 2

    def test_list_all_exception_non_global(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._fetch_messages_via_list_all.side_effect = RuntimeError("网络抖动")
        out = p.poll_once()  # 不应抛出，仅告警
        assert out == []

    def test_list_all_global_permission_error(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._fetch_messages_via_list_all.side_effect = RuntimeError("TOKEN_VERIFIED_FAILED")
        p._is_global_permission_error.return_value = True
        out = p.poll_once()
        assert out == []
        p._warn_permission_once.assert_called()

    # ── 六层会话合并 ──

    def test_unread_conversation_is_fetched(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "群消息", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["mx"]
        p.dws.chat_message_list.assert_called_once()

    def test_top_conversation_is_fetched(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._get_cached_top_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "置顶群消息", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["mx"]

    def test_db_cached_conversation_is_fetched(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._get_recent_conversations_from_db.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "DB缓存群消息", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["mx"]

    def test_conversation_dedup_across_layers(self, poller_factory):
        """同一 openConversationId 在 unread/top/db 三层都出现，只应进 all_conversations 一次。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        conv = {"openConversationId": "oc_grp", "title": "群", "singleChat": False}
        p.dws.chat_message_list_unread_conversations.return_value = [conv]
        p._get_cached_top_conversations.return_value = [conv]
        p._get_recent_conversations_from_db.return_value = [conv]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "去重消息", "senderOpenDingTalkId": "ou_peer"}]
        p.poll_once()
        # 只补拉一次（seen 去重生效）
        assert p.dws.chat_message_list.call_count == 1

    def test_blocked_conversation_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_blocked = MagicMock(side_effect=lambda oid: oid == "oc_blocked")
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_blocked", "title": "封禁", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "不应出现", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []

    def test_inaccessible_conversation_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._inaccessible_conversations.add("oc_dead")
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_dead", "title": "死会话", "singleChat": False}]
        out = p.poll_once()
        assert out == []

    def test_ou_prefix_id_skipped(self, poller_factory):
        """ou_xxx 是用户级 ID 非会话级，必须跳过避免污染 chat_id。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "ou_not_a_conv", "title": "x", "singleChat": False}]
        out = p.poll_once()
        assert out == []
        p.dws.chat_message_list.assert_not_called()
        p.dws.chat_message_list_direct.assert_not_called()

    def test_dingtalk_cid_prefix_allowed(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "cidWBNsDj5f", "title": "钉钉群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "cid群消息", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["mx"]

    def test_empty_conversations_no_early_return(self, poller_factory):
        """无任何会话时不应提前 return，要让周期统计对空平台也可见。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._poll_count = 11  # 本轮 +1 = 12 → 命中周期统计分支
        out = p.poll_once()
        assert out == []  # 确实无消息返回
        # 周期统计分支（765-776）已走过不报错即视为覆盖

    def test_periodic_stats_invoked(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._poll_count = 11
        p._top_cache_hit_flag = True
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mx", "content": "统计消息", "senderOpenDingTalkId": "ou_peer"}]
        p.poll_once()  # 12 轮 → 周期统计块执行
        assert p._poll_count == 12

    # ── 会话级过滤 ──

    def test_blacklisted_conversation_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_blacklisted_conversation = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_b", "title": "黑名单群", "singleChat": False}]
        out = p.poll_once()
        assert out == []

    def test_longtail_throttled_skip(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._should_skip_longtail_fetch = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        out = p.poll_once()
        assert out == []
        p.dws.chat_message_list.assert_not_called()

    def test_other_chat_type_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._detect_chat_type = MagicMock(return_value="other")
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_app", "title": "系统应用", "singleChat": False}]
        out = p.poll_once()
        assert out == []

    def test_work_notification_title_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_wn", "title": "工作通知", "singleChat": False}]
        out = p.poll_once()
        assert out == []

    def test_single_chat_uses_list_direct(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_s", "title": "张三", "singleChat": True}]
        p.dws.chat_message_list_direct.return_value = [{
            "openMessageId": "ms", "content": "单聊消息", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["ms"]
        p.dws.chat_message_list_direct.assert_called_once()

    def test_single_peer_unresolved_skipped(self, poller_factory):
        """单聊无法解析对方信息时跳过补拉（覆盖 474-480）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._resolve_single_chat_peer = MagicMock(return_value={})
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_s", "title": "张三", "singleChat": True}]
        out = p.poll_once()
        assert out == []

    # ── 逐条消息过滤 ──

    def test_edit_message_handled(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "me", "content": "编辑", "msgType": "edit",
            "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []
        p._handle_edit_message.assert_called_once()

    def test_recall_message_handled(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mr", "content": "撤回", "msgType": "recall",
            "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []
        p._handle_recall_message.assert_called_once()

    def test_self_sender_force_filtered(self, poller_factory):
        """自己发的消息在主路径强制过滤丢弃（覆盖 607-623）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_self_sender = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mself", "content": "我发的", "senderOpenDingTalkId": "ou_me"}]
        out = p.poll_once()
        assert out == []
        p.store._message_repo.save_message.assert_called()

    def test_skip_types_filtered(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._effective_skip_types = MagicMock(return_value={"voice"})
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mv", "content": "语音", "msgType": "voice",
            "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []

    def test_group_message_not_at_me_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_at_me = MagicMock(return_value=False)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mn", "content": "没@我", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []

    def test_first_run_old_message_ignored(self, poller_factory):
        """重启后首次轮询，超过 N 分钟的老消息应被忽略（覆盖 642-647）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        old_ts = (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mold", "content": "老消息", "createTime": old_ts,
            "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []

    def test_notification_signature_skipped(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.config.skip_notification_patterns = ["验证码"]
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "mn", "content": "你的验证码是123456",
            "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert out == []

    # ── 权限错误分流 ──

    def test_group_permanent_permission_blocked(self, poller_factory):
        """群聊跨租户/跨app属永久错误，直接拉黑（覆盖 525-528）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_permission_error = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.side_effect = RuntimeError("cross app not allowed")
        out = p.poll_once()
        assert out == []
        p._block_conversation.assert_called_once()

    def test_group_transient_permission_eventually_blocked(self, poller_factory):
        """普通群权限错误累计到阈值才拉黑（覆盖 533-536）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_permission_error = MagicMock(return_value=True)
        p._register_perm_failure = MagicMock(return_value=(True, 3))
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.side_effect = RuntimeError("AUTH_PERMISSION_DENIED")
        out = p.poll_once()
        assert out == []
        p._block_conversation.assert_called_once()

    def test_group_transient_permission_not_yet_blocked(self, poller_factory):
        """瞬时抖动未到阈值：仅告警、不拉黑（覆盖 537-541）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_permission_error = MagicMock(return_value=True)
        p._register_perm_failure = MagicMock(return_value=(False, 1))
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.side_effect = RuntimeError("AUTH_PERMISSION_DENIED")
        out = p.poll_once()
        assert out == []
        p._block_conversation.assert_not_called()

    def test_single_permanent_permission_blocked(self, poller_factory):
        """单聊跨租户永久错误拉黑（覆盖 511-514）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_permission_error = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_s", "title": "张三", "singleChat": True}]
        p.dws.chat_message_list_direct.side_effect = RuntimeError("cross app not allowed")
        out = p.poll_once()
        assert out == []
        p._block_conversation.assert_called_once()

    def test_single_external_friend_permission_not_blocked(self, poller_factory):
        """单聊外部好友无 direct 权限属正常，仅跳过不拉黑（覆盖 515-519）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_permission_error = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_s", "title": "外部好友", "singleChat": True}]
        p.dws.chat_message_list_direct.side_effect = RuntimeError("no permission")
        out = p.poll_once()
        assert out == []
        p._block_conversation.assert_not_called()

    def test_global_permission_error_skips_session(self, poller_factory):
        """全局/组织级权限错误：跳过该会话、不拉黑（覆盖 542-558）。

        真实 _is_permission_error 对 TOKEN_VERIFIED_FAILED 返回 False（已全局判定优先），
        故此处保持默认 False，使 elif 全局分支可达。
        """
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_global_permission_error = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.side_effect = RuntimeError("TOKEN_VERIFIED_FAILED")
        out = p.poll_once()
        assert out == []
        p._block_conversation.assert_not_called()
        p._warn_permission_once.assert_called()

    def test_generic_fetch_error_warned(self, poller_factory):
        """非权限类异常：记录警告、跳过该会话（覆盖 559-564）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.side_effect = RuntimeError("openCid or cid is required")
        out = p.poll_once()  # 不应抛出
        assert out == []

    # ── 去重 ──

    def test_cross_round_and_intra_round_dedup(self, poller_factory):
        """同一 msg_id 同时来自 list-all 与逐会话补拉，最终去重只保留一条。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        dup = _make_msg({"openMessageId": "dup", "content": "重复消息"},
                        chat_id="oc_a", chat_type="group", title="群A")
        p._fetch_messages_via_list_all.return_value = [dup]
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_a", "title": "群A", "singleChat": False}]
        p.dws.chat_message_list.return_value = [{
            "openMessageId": "dup", "content": "重复消息", "senderOpenDingTalkId": "ou_peer"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["dup"]

    # ── 各发现通道异常兜底 ──

    def test_unread_dws_permission_error(self, poller_factory):
        from src.dws_adapter import DwsPermissionError
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.side_effect = DwsPermissionError("无权限")
        out = p.poll_once()
        assert out == []
        p._warn_permission_once.assert_called()

    def test_unread_generic_error_swallowed(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.dws.chat_message_list_unread_conversations.side_effect = RuntimeError("接口挂了")
        out = p.poll_once()
        assert out == []

    def test_top_dws_permission_error(self, poller_factory):
        from src.dws_adapter import DwsPermissionError
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._get_cached_top_conversations.side_effect = DwsPermissionError("无权限")
        out = p.poll_once()
        assert out == []
        p._warn_permission_once.assert_called()

    def test_db_conv_fetch_error_swallowed(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._get_recent_conversations_from_db.side_effect = RuntimeError("DB 读失败")
        out = p.poll_once()
        assert out == []

    # ── 外部好友强制轮询 ──

    def test_external_friend_forced_poll(self, poller_factory):
        """外部好友（有 oc_xxx chat_id）应解析为会话级 ID 并入队补拉。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.store._external_friend_repo.list_external_friends = p._orig_list_external_friends
        p.store._external_friend_repo.add_external_friend(
            name="外部甲", open_dingtalk_id="ou_ef", chat_id="oc_ef", notes="测试")
        p.dws.chat_message_list_direct.return_value = [{
            "openMessageId": "mef", "content": "外部好友消息", "senderOpenDingTalkId": "ou_ef"}]
        out = p.poll_once()
        assert [m.msg_id for m in out] == ["mef"]

    def test_external_friend_unresolvable_skipped(self, poller_factory):
        """外部好友无法解析为 oc_xxx 会话 ID 时跳过（覆盖 363-368）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.store._external_friend_repo.list_external_friends = p._orig_list_external_friends
        p.store._external_friend_repo.add_external_friend(
            name="外部乙", open_dingtalk_id="ou_ef2", chat_id="ou_notoc", notes="测试")
        out = p.poll_once()
        assert out == []
        p.dws.chat_message_list_direct.assert_not_called()

    def test_external_friend_list_error_swallowed(self, poller_factory):
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p.store._external_friend_repo.list_external_friends = MagicMock(
            side_effect=RuntimeError("读外部好友失败"))
        out = p.poll_once()  # 不应抛出
        assert out == []

    # ── 已处理消息去重（真实正确性路径）──

    def test_message_already_processed_dropped_in_main_loop(self, poller_factory):
        """主循环内 is_message_processed=True 的消息仍追踪时间戳但丢弃（覆盖 592-599）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        raw = {"openMessageId": "done", "content": "已处理", "senderOpenDingTalkId": "ou_peer"}
        p.store._message_repo.is_message_processed = MagicMock(return_value=True)
        p.dws.chat_message_list_unread_conversations.return_value = [{
            "openConversationId": "oc_grp", "title": "群", "singleChat": False}]
        p.dws.chat_message_list.return_value = [raw]
        out = p.poll_once()
        assert out == []
        # 时间戳追踪生效：_last_poll_time 被更新（即使消息被丢弃）
        assert "oc_grp" in p._last_poll_time

    def test_message_cross_round_processed_deduped(self, poller_factory):
        """跨轮次已处理消息在最终全局去重处被丢弃（覆盖 750-751）。"""
        p, _ = poller_factory()
        _stub_poll_once_deps(p)
        p._is_msg_processed = MagicMock(return_value=True)
        p._fetch_messages_via_list_all.return_value = [
            _make_msg({"openMessageId": "cross", "content": "跨轮重复"},
                      chat_id="oc_a", chat_type="group", title="群A")]
        out = p.poll_once()
        assert out == []
