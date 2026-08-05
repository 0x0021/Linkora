"""动态场景 few-shot 检索（_SceneFewShotSelector）回归测试。

缺陷背景：`_SceneFewShotSelector` 只在 __init__ 里存了 self.store，却没有 `_cc`
方法（那是同文件 BaselineRepo 的），而 `retrieve()` 第一次访问数据库就是
`self._cc(platform).cursor()` —— 必抛 AttributeError。调用方
`src/llm/system_prompt.py` 又用 `except Exception` 把它吞成一行 warning 并降级
为静态样例，于是 `dynamic_few_shot=True` 配了也 100% 不生效，且外部完全无感。

这里做两层护栏：
1. 结构层：selector 必须具备 `_cc` 且签名与 BaselineRepo 一致；
2. 行为层：塞入真实 user→assistant 配对后，retrieve 必须能检索出来（不抛异常，
   且返回内容正确）——只断言"不抛异常"不够，那样即使 SQL 全错也能过。
"""

import inspect

import pytest

import src.memory.account_identity as ai
from src.memory.baseline_repo import BaselineRepo, _SceneFewShotSelector
from src.memory.sqlite_store import SQLiteStore

OWNER = "0x0021"
PLATFORM = "dingtalk"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "resolve_account_id", lambda p, fb=None: f"{p}:acct-few-shot")
    s = SQLiteStore(str(tmp_path / "linkora.db"))
    yield s
    s.close()


def _seed_pair(conn, chat_id: str, user_msg: str, owner_reply: str) -> None:
    """写入一组 user→assistant 配对（满足 retrieve 的质量门：8~120 字、非媒体）。"""
    for sender, content, role, is_bot in (
        ("对方", user_msg, "user", 0),
        (OWNER, owner_reply, "assistant", 0),
    ):
        conn.execute(
            """INSERT INTO messages
               (chat_id, chat_type, msg_id, sender_id, sender_name, content,
                msg_type, timestamp, role, is_bot, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (chat_id, "single", f"{chat_id}-{role}-{abs(hash(content)) % 10**8}",
             "uid", sender, content, "text", "2026-01-01T00:00:00", role, is_bot,
             "2026-01-01T00:00:00"),
        )
    conn.commit()


def test_selector_has_cc_method_matching_baseline_repo():
    """结构护栏：selector 必须有 _cc，且签名与 BaselineRepo._cc 一致。"""
    assert hasattr(_SceneFewShotSelector, "_cc"), (
        "_SceneFewShotSelector 缺少 _cc —— retrieve() 首行就会 AttributeError，"
        "且会被 system_prompt.py 的 except Exception 静默吞掉"
    )
    assert (inspect.signature(_SceneFewShotSelector._cc)
            == inspect.signature(BaselineRepo._cc))


def test_retrieve_returns_seeded_pair(store):
    """行为护栏：检索必须真能命中写入的配对，而不只是「没抛异常」。"""
    conn = store.conv_conn(PLATFORM)
    _seed_pair(conn, "chat_a", "这个方案的排期能不能提前一周？", "排期可以提前，我today先把接口对齐")
    _seed_pair(conn, "chat_b", "午饭吃什么比较好呢", "楼下那家面馆还行，人少不用排队")

    pairs = _SceneFewShotSelector(store).retrieve(
        OWNER, "方案排期能提前吗", limit=4, method="trigram", platform=PLATFORM,
    )

    assert isinstance(pairs, list) and pairs, "动态 few-shot 检索返回空 —— 功能未生效"
    assert all({"user", "assistant"} <= set(p) for p in pairs)
    assert any("排期" in p["assistant"] for p in pairs), (
        f"未检索到语义相关的配对，实际返回: {pairs}"
    )


def test_retrieve_excludes_given_pairs(store):
    """exclude 去重生效（顺带证明 _cc 之后的主流程确实跑到了底）。"""
    conn = store.conv_conn(PLATFORM)
    _seed_pair(conn, "chat_c", "周报什么时候交比较合适", "周五下班前给我就行，不用太赶")

    selector = _SceneFewShotSelector(store)
    first = selector.retrieve(OWNER, "周报交付时间", limit=4, method="trigram", platform=PLATFORM)
    assert first, "基线检索为空，后续 exclude 断言无意义"

    after = selector.retrieve(
        OWNER, "周报交付时间", limit=4, method="trigram",
        exclude=[{"user": p["user"], "assistant": p["assistant"]} for p in first],
        platform=PLATFORM,
    )
    assert after == [], f"exclude 未生效，仍返回: {after}"


def test_retrieve_empty_query_short_circuits(store):
    """空 query / 空 owner 直接返回 []（不触库）。"""
    selector = _SceneFewShotSelector(store)
    assert selector.retrieve("", "有内容", platform=PLATFORM) == []
    assert selector.retrieve(OWNER, "   ", platform=PLATFORM) == []
