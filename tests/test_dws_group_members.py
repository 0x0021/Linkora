"""群成员枚举（``chat +chat-members-list``）与会话信息嵌套取值修复的回归测试。

背景（2026-10-22）：dws v1.0.63 新增 Shortcut ``chat +chat-members-list``，
提供群成员枚举，真人 / 机器人分桶、自带翻页去重与完整性证据
（``complete`` / ``partial`` / ``failures``）。

同时发现两个既有缺陷（本测试一并锁定）：

1. ``DwsAdapter.chat_conversation_info`` 返回的是**嵌套**结构
   ``{"conversationInfo": {...}}``，而 ``GetConversationInfoTool`` 按扁平 key 取值
   （``info.get("title")`` / ``info.get("singleChat")`` / ``info.get("memberCount")``），
   全部取不到 → 工具误报「标题为空、单聊、人数 0」。
2. ``GetConversationInfoTool`` 原先从 ``conversation-info`` 里读 ``members`` 字段
   试图拼成员名单，但该接口只返回 ``memberCount``、**不含成员明细**，
   即工具声称的「成员列表」从来没真正返回过。
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from src.dws_adapter import DwsAdapter
from src.im_adapter.errors import IMAdapterError
from src.tools.conversation import GetConversationInfoTool


class _FakeDws(DwsAdapter):
    """内存 mock：记录命令，返回可配置的 shortcut 返回体。"""

    def __init__(self, result: dict | None = None, fail: bool = False):
        self.cli_path = "dws"
        self.dry_run = False
        self.profile = ""
        self.timeout = 30
        self.ai_tag_default = True
        self._calls: list[list[str]] = []
        self._result = result or {}
        self._fail = fail

    def run(self, args, *a, **k):
        self._calls.append(list(args))
        if self._fail:
            raise IMAdapterError("dws exit 1: members list failed")
        return {"success": True, "result": self._result}


# dws 实测返回体（2026-10-22 真实群，字段名照抄）
_MEMBERS_RESULT = {
    "bots": [
        {
            "isCreator": True,
            "manageable": True,
            "name": "小钉",
            "openBotId": "REwO84KtLYF6qUufAZxzig==",
            "openDingtalkId": "DDn1bYIeDbcHzDxkdnCk72ufFuEElVQWS",
            "robotCode": "intelligent",
        }
    ],
    "buckets": {
        "bots": {"complete": True, "count": 1},
        "users": {
            "complete": True, "count": 2, "failedCount": 0,
            "failures": [], "hasMore": False, "pagesFetched": 1,
            "partial": False, "stopReason": "source_complete",
        },
    },
    "complete": True,
    "contractVersion": "im.group-members.v1",
    "conversationId": "cid_abc",
    "counts": {"bots": 1, "total": 3, "users": 2},
    "failedCount": 0,
    "failures": [],
    "hasMore": False,
    "partial": False,
    "users": [
        {
            "memberDingtalkId": "$:LWCP_v1:$HKA==",
            "memberEmpName": "群主",
            "memberGroupNick": "",
            "memberNick": "群主",
            "memberRoleDesc": "群主",
            "name": "群主",
            "nick": "群主",
            "openDingtalkId": "DDn1bYIeDbcGDY6HwqBjreyVz776bLniS4",
            "role": "群主",
        },
        {
            "memberDingtalkId": "$:LWCP_v1:$seN==",
            "memberEmpName": "成员甲",
            "memberGroupNick": "",
            "memberNick": "成员甲",
            "memberRoleDesc": "普通成员",
            "name": "成员甲",
            "nick": "成员甲",
            "openDingtalkId": "DDn1bYIeDbcHXCiPq5iS4a6iiwnlQdNnXFyM",
            "role": "普通成员",
        },
    ],
}


# ============ 1. 适配器层：命令拼装与归一化 ============

def test_stable_id_uses_conversation_id_flag():
    """稳定 ID（oc_/cid_ 前缀）走 --conversation-id 直通，避免语义解析失败。"""
    a = _FakeDws(_MEMBERS_RESULT)
    a.chat_group_members("cid_abc")
    cmd = a._calls[0]
    assert cmd[:2] == ["chat", "+chat-members-list"]
    assert cmd[cmd.index("--conversation-id") + 1] == "cid_abc"
    # 稳定 ID 不应走 --group（那会触发语义解析，对已存 ID 可能报 resolution_not_found）
    assert "--group" not in cmd


def test_group_name_uses_group_flag():
    """群名（非 ID 形态）走 --group 语义解析。"""
    a = _FakeDws(_MEMBERS_RESULT)
    a.chat_group_members("项目冲刺群")
    cmd = a._calls[0]
    assert cmd[cmd.index("--group") + 1] == "项目冲刺群"
    assert "--conversation-id" not in cmd


def test_page_limit_clamped_to_valid_range():
    """page_limit 越界会被夹到 dws 允许的 1-500，避免 CLI 侧校验失败。"""
    a = _FakeDws(_MEMBERS_RESULT)
    a.chat_group_members("cid_abc", page_limit=9999)
    assert a._calls[0][a._calls[0].index("--page-limit") + 1] == "500"
    a2 = _FakeDws(_MEMBERS_RESULT)
    a2.chat_group_members("cid_abc", page_limit=0)
    assert a2._calls[0][a2._calls[0].index("--page-limit") + 1] == "1"


def test_member_types_forwarded():
    """member_types 透传给 CLI（只取真人 / 只取机器人）。"""
    a = _FakeDws(_MEMBERS_RESULT)
    a.chat_group_members("cid_abc", member_types="user")
    assert a._calls[0][a._calls[0].index("--member-types") + 1] == "user"


def test_normalized_result_has_buckets_and_counts():
    """归一化结果：真人 / 机器人分桶 + 计数 + 完整性证据。"""
    a = _FakeDws(_MEMBERS_RESULT)
    d = a.chat_group_members("cid_abc")
    assert d["conversation_id"] == "cid_abc"
    assert d["counts"] == {"users": 2, "bots": 1, "total": 3}
    assert d["complete"] is True
    assert d["partial"] is False
    assert [u["name"] for u in d["users"]] == ["群主", "成员甲"]
    # 群角色保留
    assert d["users"][0]["role"] == "群主"
    assert d["users"][0]["open_dingtalk_id"] == "DDn1bYIeDbcGDY6HwqBjreyVz776bLniS4"
    # 机器人单独成桶，带 robotCode
    assert d["bots"][0]["name"] == "小钉"
    assert d["bots"][0]["robot_code"] == "intelligent"


def test_partial_result_flagged():
    """被截断 / 有分页失败时 complete=False，调用方不会误当完整名单。"""
    a = _FakeDws({
        "conversationId": "cid_abc",
        "users": [{"name": "甲", "openDingtalkId": "DDn1"}],
        "bots": [],
        "counts": {"users": 1, "bots": 0, "total": 1},
        "complete": False,
        "partial": True,
        "failures": [{"bucket": "users", "error": "pagination_error"}],
    })
    d = a.chat_group_members("cid_abc")
    assert d["complete"] is False
    assert d["partial"] is True
    assert d["failures"]


def test_empty_group_requires_argument():
    """空 group 直接报错，不打无意义的 CLI 调用。"""
    a = _FakeDws(_MEMBERS_RESULT)
    try:
        a.chat_group_members("")
    except ValueError:
        pass
    else:
        raise AssertionError("应当抛出 ValueError")
    assert a._calls == []


# ============ 2. 工具层：get_conversation_info 修复 ============

class _FakeInfoDws:
    """模拟适配器：conversation-info 返回**已摊平**的结果（摊平由适配器层负责）。

    摊平逻辑在 ``DwsAdapter.chat_conversation_info`` 内完成（见
    ``test_dws_conversation_info_flattens_nested_result``），工具层只消费扁平 dict。
    """

    def __init__(self, members_result=None, members_fail=False):
        self._members_result = members_result if members_result is not None else _MEMBERS_RESULT
        self._members_fail = members_fail
        self.member_calls = 0

    def chat_conversation_info(self, chat_id):
        return {
            "corpId": "ding_x",
            "corpName": "某公司",
            "createAt": "2020-11-09 18:04:39",
            "memberCount": 2,
            "openConversationId": chat_id,
            "ownerNick": "群主",
            "singleChat": False,
            "title": "PMOO",
        }

    def chat_group_members(self, group, **kw):
        self.member_calls += 1
        if self._members_fail:
            raise IMAdapterError("dws exit 1: members failed")
        return {
            "conversation_id": group,
            "users": self._members_result.get("users", []),
            "bots": self._members_result.get("bots", []),
            "counts": self._members_result.get("counts", {}),
            "complete": self._members_result.get("complete", False),
            "partial": self._members_result.get("partial", False),
            "failures": self._members_result.get("failures", []),
        }


def test_dws_conversation_info_flattens_nested_result():
    """适配器层把 dws 的嵌套 conversationInfo 摊平（工具层依赖扁平 key 取值）。

    修复前：``chat_conversation_info`` 直接返回 ``{"conversationInfo": {...}}``，
    工具读 ``info.get("title")`` 恒为 None → 误报「标题为空 / 单聊 / 人数 0」。
    """
    a = _FakeDws({
        "conversationInfo": {
            "memberCount": 2,
            "openConversationId": "cid_abc",
            "ownerNick": "群主",
            "singleChat": False,
            "title": "PMOO",
        }
    })
    info = a.chat_conversation_info("cid_abc")
    assert info["title"] == "PMOO"
    assert info["memberCount"] == 2
    assert info["singleChat"] is False
    assert info["ownerNick"] == "群主"
    # 不应残留外层包装
    assert "conversationInfo" not in info


def test_dws_conversation_info_passthrough_when_already_flat():
    """已摊平的返回（原样透传，不做二次包装）。"""
    a = _FakeDws({"title": "扁平群", "memberCount": 7, "singleChat": True})
    info = a.chat_conversation_info("cid_abc")
    assert info["title"] == "扁平群"
    assert info["memberCount"] == 7


def test_tool_reads_nested_conversation_info():
    """修复核心：嵌套 conversationInfo 被正确摊平，标题/类型/人数不再为空。"""
    t = GetConversationInfoTool(_FakeInfoDws())
    r = t.execute({"chat_id": "cid_abc"})
    assert r["title"] == "PMOO"
    assert r["type"] == "group"          # 修复前因 singleChat 取不到而误报 "single"
    assert r["owner"] == "群主"
    assert r["member_count"] == 2


def test_tool_returns_member_list():
    """工具真正返回成员名单（含角色）与机器人分桶。"""
    t = GetConversationInfoTool(_FakeInfoDws())
    r = t.execute({"chat_id": "cid_abc"})
    assert [m["name"] for m in r["members"]] == ["群主", "成员甲"]
    assert r["members"][0]["role"] == "群主"
    assert r["bots"][0]["name"] == "小钉"
    assert r["members_complete"] is True


def test_tool_include_members_false_skips_enumeration():
    """include_members=False → 不发起成员枚举调用。"""
    d = _FakeInfoDws()
    t = GetConversationInfoTool(d)
    r = t.execute({"chat_id": "cid_abc", "include_members": False})
    assert d.member_calls == 0
    assert r["members"] == []
    assert r["title"] == "PMOO"      # 基础信息仍然返回


def test_tool_member_enumeration_failure_degrades_gracefully():
    """成员枚举失败 → 降级（名单空 + members_complete=False），基础信息不受影响。"""
    t = GetConversationInfoTool(_FakeInfoDws(members_fail=True))
    r = t.execute({"chat_id": "cid_abc"})
    assert r["title"] == "PMOO"
    assert r["member_count"] == 2
    assert r["members"] == []
    assert r["members_complete"] is False


def test_tool_member_list_capped_at_50():
    """超大群成员名单截断到 50 条（避免撑爆 LLM 上下文）。"""
    big = {
        "users": [{"name": f"u{i}", "openDingtalkId": f"DD{i}"} for i in range(200)],
        "bots": [],
        "counts": {"users": 200, "bots": 0, "total": 200},
        "complete": True, "partial": False, "failures": [],
    }
    t = GetConversationInfoTool(_FakeInfoDws(members_result=big))
    r = t.execute({"chat_id": "cid_abc"})
    assert len(r["members"]) == 50
    # 人数仍报真实值（不是 len(members)），否则会被误读为「群里只有 50 人」
    assert r["member_count"] == 2   # conversation-info 的 memberCount


def test_inline_members_take_priority_over_enumeration():
    """会话信息内嵌名单（飞书路径）优先，不触发成员枚举调用。

    飞书 ``chat_conversation_info`` 直接返回 members；钉钉 dws 不返回内嵌名单，
    才走 ``+chat-members-list``。两条路径按「内嵌优先」择一，避免重复请求。
    """
    class _InlineDws(_FakeInfoDws):
        def chat_conversation_info(self, chat_id):
            return {
                "title": "飞书群", "singleChat": False, "memberCount": 2,
                "members": [{"name": "内嵌甲", "userId": "u1"},
                            {"name": "内嵌乙", "userId": "u2"}],
            }

    d = _InlineDws()
    t = GetConversationInfoTool(d)
    r = t.execute({"chat_id": "oc_x"})
    assert d.member_calls == 0
    assert [m["name"] for m in r["members"]] == ["内嵌甲", "内嵌乙"]
    assert r["member_count"] == 2
    assert r["members_complete"] is True   # 内嵌名单视为已给出


def test_single_chat_skips_enumeration():
    """单聊无成员列表概念 → 不触发成员枚举。"""
    class _SingleDws(_FakeInfoDws):
        def chat_conversation_info(self, chat_id):
            return {"title": "某人", "singleChat": True, "memberCount": 0}

    d = _SingleDws()
    r = GetConversationInfoTool(d).execute({"chat_id": "cid_abc"})
    assert d.member_calls == 0
    assert r["type"] == "single"
