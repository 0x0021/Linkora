"""钉钉群枚举补全回归测试。

验证根因修复：钉钉群聊不在「消息搜索权益」覆盖范围内，原 list-all 主通道只回单聊，
导致群消息/系统通知会话长期拉不到。修复 = 增加「群枚举源」（dws chat +chat-list-all /
+chat-list-mine），把群 openConversationId 纳入轮询会话集，由 poller 走 list-all 按群过滤拉取。

覆盖：
- 适配器层 _chat_list_groups 分页合并去重
- poller 层 _fetch_joined_groups 合并去重（钉钉 / 非钉钉）
- _gather_conversations 把群纳入枚举（带 singleChat=False）、尊重黑名单
"""
from __future__ import annotations

import types

from tests.test_poller import _make_poller

from src.dws_adapter.chat import DwsAdapterChatMixin


class TestAdapterChatListGroups:
    """适配器 _chat_list_groups：分页合并去重。"""

    def test_paginates_and_dedups(self):
        pages = [
            {"complete": False, "nextCursor": "C2",
             "groups": [{"openConversationId": "cid1", "name": "A"},
                        {"openConversationId": "cid2", "name": "B"}]},
            {"complete": True, "nextCursor": "",
             "groups": [{"openConversationId": "cid2", "name": "B"},
                        {"openConversationId": "cid3", "name": "C"}]},
        ]
        sent = {"i": 0}
        fake = types.SimpleNamespace()
        fake._get_result = lambda d: d

        def fake_run(args, **kw):
            p = pages[sent["i"]]
            sent["i"] += 1
            return p

        fake.run = fake_run
        fn = DwsAdapterChatMixin._chat_list_groups.__get__(fake)
        out = fn(["chat", "+chat-list-all"], limit=10)
        assert [g["openConversationId"] for g in out] == ["cid1", "cid2", "cid3"]
        assert sent["i"] == 2  # 两页都被请求

    def test_stops_on_complete(self):
        pages = [{"complete": True, "nextCursor": "C9",
                  "groups": [{"openConversationId": "cid1", "name": "A"}]}]
        sent = {"i": 0}
        fake = types.SimpleNamespace()
        fake._get_result = lambda d: d

        def fake_run(args, **kw):
            p = pages[sent["i"]]
            sent["i"] += 1
            return p

        fake.run = fake_run
        fn = DwsAdapterChatMixin._chat_list_groups.__get__(fake)
        out = fn(["chat", "+chat-list-all"], limit=10)
        assert len(out) == 1
        assert sent["i"] == 1  # 只有一页


class TestFetchJoinedGroups:
    """poller 层 _fetch_joined_groups：合并「我加入 + 我创建」群并去重。"""

    def test_merges_dedup_dingtalk(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        poller.adapter_type = "dingtalk"
        poller.dws.chat_list_groups_joined = lambda: [  # type: ignore[assignment]
            {"openConversationId": "cid1", "name": "群A"},
            {"openConversationId": "cid2", "name": "群B"}]
        poller.dws.chat_list_groups_mine = lambda: [  # type: ignore[assignment]
            {"openConversationId": "cid2", "name": "群B"},
            {"openConversationId": "cid3", "name": "群C"}]
        out = poller._fetch_joined_groups()
        assert [g["openConversationId"] for g in out] == ["cid1", "cid2", "cid3"]

    def test_empty_when_non_dingtalk(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        poller.adapter_type = "feishu"
        out = poller._fetch_joined_groups()
        assert out == []


class TestGatherConversationsGroupEnum:
    """_gather_conversations 把群纳入枚举。"""

    def _stub_other_sources(self, poller):
        """把群枚举以外的会话来源 stub 掉，聚焦群枚举。"""
        poller._get_cached_top_conversations = lambda: []      # type: ignore[method-assign]
        poller._get_recent_conversations_from_db = lambda: []  # type: ignore[method-assign]
        poller.store._external_friend_repo.list_external_friends.return_value = []

    def test_groups_entered_with_singlechat_false(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        self._stub_other_sources(poller)
        groups = [{"openConversationId": "cidX", "name": "研发群"}]
        poller._get_cached_joined_groups = lambda: groups      # type: ignore[method-assign]
        all_conv, _ = poller._gather_conversations([])
        matched = [c for c in all_conv if c.get("openConversationId") == "cidX"]
        assert matched, "群未进入轮询枚举"
        assert matched[0]["singleChat"] is False
        assert matched[0]["title"] == "研发群"

    def test_blocked_groups_skipped(self, tmp_db_path):
        poller, _ = _make_poller(tmp_db_path)
        self._stub_other_sources(poller)
        poller._inaccessible_conversations.add("cidBlocked")
        poller._get_cached_joined_groups = lambda: [          # type: ignore[method-assign]
            {"openConversationId": "cidBlocked", "name": "保密群"}]
        all_conv, _ = poller._gather_conversations([])
        assert not any(c.get("openConversationId") == "cidBlocked" for c in all_conv)
