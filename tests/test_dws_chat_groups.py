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


def test_mine_groups_int_cursor_too():
    class _Mine(_FakeDws):
        def run(self, args, *a, **k):
            self._calls.append(list(args))
            if "--cursor" not in args:
                return {"complete": False, "nextCursor": 999,
                        "groups": [{"openConversationId": "cidM1", "name": "我建的群"}]}
            return {"complete": True, "nextCursor": "",
                    "groups": [{"openConversationId": "cidM2", "name": "我建的群2"}]}

    a = _Mine()
    groups = a.chat_list_groups_mine()
    assert {g["openConversationId"] for g in groups} == {"cidM1", "cidM2"}
    ci = a._calls[1].index("--cursor")
    assert isinstance(a._calls[1][ci + 1], str)
