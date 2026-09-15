"""DwsAdapter 群消息逐群拉取的回归测试。

核心防回归点：钉钉群消息必须走用户级逐群接口 ``chat message list --group``，
而**不是** ``chat message list-all``（底层 ``search_messages_by_time_range``）。
list-all 依赖「消息搜索权益」，该权益默认不覆盖群聊，对群返回业务错误
（PREPARE_CALL_TOOL_ERROR），会导致群消息长期拉不到。
"""
import sys

sys.path.insert(0, "src")

from src.dws_adapter import DwsAdapter


class _FakeDws(DwsAdapter):
    """内存 mock：run 返回包裹在 result 里的群消息，并记录命令。"""

    def __init__(self):
        self.cli_path = "dws"
        self.dry_run = False
        self.profile = ""
        self.timeout = 30
        self._calls = []

    def run(self, args, *a, **k):
        self._calls.append(list(args))
        return {
            "success": True,
            "result": {
                "hasMore": False,
                "nextCursor": 0,
                "messages": [
                    {"senderName": "张三", "content": "hello"},
                    {"senderName": "李四", "content": "world"},
                ],
            },
        }


def test_group_message_uses_per_group_command_not_list_all():
    a = _FakeDws()
    msgs = a.chat_message_list_group("cidX", "2026-01-01 00:00:00", 5)
    assert len(msgs) == 2
    assert msgs[0]["content"] == "hello"

    # 唯一一次调用必须是 chat message list --group，绝不走 list-all
    assert len(a._calls) == 1
    cmd = a._calls[0]
    assert "message" in cmd and "list" in cmd
    assert "list-all" not in cmd, "群消息不能走 list-all（群消息搜索权益限制）"
    gi = cmd.index("--group")
    assert cmd[gi + 1] == "cidX"
    ti = cmd.index("--time")
    assert cmd[ti + 1] == "2026-01-01 00:00:00"
    assert "--direction" in cmd and cmd[cmd.index("--direction") + 1] == "newer"
    li = cmd.index("--limit")
    assert cmd[li + 1] == "5"


def test_chat_message_list_delegates_to_per_group():
    a = _FakeDws()
    # chat_message_list 应直接委托给逐群接口（cached_result 不再生效）
    msgs = a.chat_message_list(group="cidY", time_str="2026-02-01 00:00:00", limit=3)
    assert len(msgs) == 2
    assert "list-all" not in a._calls[0]


def test_empty_result_returns_empty_list():
    class _Empty(_FakeDws):
        def run(self, args, *a, **k):
            self._calls.append(list(args))
            return {"success": True, "result": {"hasMore": False, "nextCursor": 0, "messages": []}}

    a = _Empty()
    assert a.chat_message_list_group("cidZ", "2026-01-01 00:00:00", 5) == []


# ── 分页完整性（防静默丢消息）──
# 实测 dws v1.0.62-beta.8：`chat message list --limit 5` 只回 5 条且 hasMore=true，
# 而同一窗口实际有 6 条；加 --page-all 后回完整 6 条（complete=true）。
# 旧实现忽略 hasMore/nextCursor，超出一页的消息被永久丢弃。


def test_group_message_requests_page_all_to_avoid_silent_loss():
    """必须显式请求自动翻页，且页数/总量上限与 limit 挂钩。"""
    a = _FakeDws()
    a.chat_message_list_group("cidX", "2026-01-01 00:00:00", 5)

    cmd = a._calls[0]
    assert "--page-all" in cmd, "不带 --page-all 会只取第一页并静默丢弃其余消息"
    pages = DwsAdapter._MESSAGE_LIST_MAX_PAGES
    assert cmd[cmd.index("--page-limit") + 1] == str(pages)
    # 总量上限 = 每页 limit × 最大页数，保证「单次积压 > 一页」也能分批追赶
    assert cmd[cmd.index("--max-items") + 1] == str(5 * pages)


def test_truncated_paging_logs_warning(caplog):
    """翻页被页数/总量上限截断时必须告警——不能静默返回部分结果。"""

    class _Truncated(_FakeDws):
        def run(self, args, *a, **k):
            self._calls.append(list(args))
            return {"success": True, "result": {
                "partial": True, "truncated": True, "stopReason": "page_limit",
                "pagesFetched": 10, "hasMore": True,
                "messages": [{"content": "x"}],
            }}

    a = _Truncated()
    with caplog.at_level("WARNING"):
        msgs = a.chat_message_list_group("cidQ", "2026-01-01 00:00:00", 5)
    assert len(msgs) == 1
    assert any("翻页被截断" in r.message for r in caplog.records)


def test_direct_message_has_more_logs_warning(caplog):
    """list-direct 没有 --page-all，单页满载时必须告警提醒依赖下轮补拉。"""

    class _DirectFull(_FakeDws):
        def run(self, args, *a, **k):
            self._calls.append(list(args))
            return {"success": True, "result": {
                "hasMore": True, "messages": [{"content": "hi"}],
            }}

    a = _DirectFull()
    with caplog.at_level("WARNING"):
        msgs = a.chat_message_list_direct(user_id="u1", time_str="2026-01-01 00:00:00", limit=20)
    assert len(msgs) == 1
    assert any("单聊消息单页已满" in r.message for r in caplog.records)
    # list-direct 不得被塞入它不支持的翻页参数
    cmd = a._calls[0]
    assert "--page-all" not in cmd
    assert "--cursor" not in cmd
