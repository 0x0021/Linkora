"""DwsAdapter 群列表拉取的回归测试。

核心防回归点：钉钉 DWS 的 ``nextCursor`` 是**数字**（不是字符串），
拼 ``--cursor`` 参数时必须转 ``str``，否则 ``subprocess`` 与 debug 日志的
``" ".join(cmd)`` 都会抛 ``TypeError: sequence item 6: expected str instance, int found``。
"""
import sys

sys.path.insert(0, "src")

from src.dws_adapter import DwsAdapter


class _FakeDws(DwsAdapter):
    """用内存脚本替换真实 dws CLI，模拟「首页数 + 数字 nextCursor + 末页空 cursor」。"""

    def __init__(self):
        # 跳过真实构造（不碰 CLI/网络），仅设置 _chat_list_groups 所需的属性
        self.cli_path = "dws"
        self.dry_run = False
        self.profile = ""
        self._calls = []

    def run(self, args, *a, **k):
        self._calls.append(list(args))
        # 第一页：返回 1 个群 + 数字 nextCursor
        if "--cursor" not in args:
            return {
                "complete": False,
                "nextCursor": 1749440351177,  # 关键：DWS 返回数字 cursor
                "groups": [{"openConversationId": "cidA", "name": "群A"}],
            }
        # 后续页（带 --cursor <int>）：末页，无更多
        return {
            "complete": True,
            "nextCursor": "",
            "groups": [{"openConversationId": "cidB", "name": "群B"}],
        }


def test_int_cursor_is_strified_in_args():
    a = _FakeDws()
    groups = a.chat_list_groups_joined()
    assert {g["openConversationId"] for g in groups} == {"cidA", "cidB"}
    # 第二页调用必须把 --cursor 的值转成 str
    second_call = a._calls[1]
    ci = second_call.index("--cursor")
    assert isinstance(second_call[ci + 1], str), "nextCursor 必须转 str 后拼入命令"


def test_mine_groups_no_cursor_pagination():
    """``+chat-list-mine`` 不支持 --cursor，且不传 --limit 即返回全部 → 只请求一次。

    防回归：旧实现复用通用分页函数会给它拼 --cursor，而 dws v1.0.62-beta.8 实测对
    该 shortcut 传 --cursor 会返回 unknown flag 错误，第 2 页必然失败（被 except 吞掉），
    结果只能拿到第一页。旧测试用 mock 伪造了「支持 cursor」的响应，因此长期误绿。
    """
    class _Mine(_FakeDws):
        def run(self, args, *a, **k):
            self._calls.append(list(args))
            return {"complete": True, "nextCursor": "",
                    "groups": [{"openConversationId": "cidM1", "name": "我建的群"},
                               {"openConversationId": "cidM2", "name": "我建的群2"}]}

    a = _Mine()
    groups = a.chat_list_groups_mine()
    assert {g["openConversationId"] for g in groups} == {"cidM1", "cidM2"}
    assert len(a._calls) == 1, "不支持游标分页的命令应只请求一次"
    assert "--cursor" not in a._calls[0], "+chat-list-mine 传 --cursor 会被 dws 拒绝"
    assert "--limit" not in a._calls[0], "不传 --limit 才返回全部自建群"
