"""P1-5 等价性测试：``_normalize_chat`` 下沉到基类后，行为与两平台旧实现**完全一致**。

这是本次重构的核心安全网：把下沉前的两个平台实现**原样复制**为测试内的 legacy
函数，对同一组用例断言「基类模板方法 == legacy」——任何行为漂移都会红
（含字段取值顺序、标题兜底、int 型 ID 是否被转成 str 这些易漏细节）。

同时固化两个不变量：
- 两个子类**不得**再 shadow 基类模板方法（否则又退回「基类实现是死代码」的状态）；
- ``_infer_single_chat`` 仍由各平台自行覆写（单聊判定语义确实分叉，不可合并）。
"""
from __future__ import annotations

from typing import Any

from src.im_adapter.base_adapter import BaseIMAdapter
from src.im_adapter.feishu import FeishuCliAdapter
from src.im_adapter.wecom import WecomCliAdapter


# ── 下沉前的两个平台实现（原样复制，作为等价性基准） ────────────────────────
def _legacy_normalize_chat_feishu(self: Any, chat: dict) -> dict:
    cid = chat.get("chat_id") or chat.get("id") or ""
    if not cid:
        return chat
    result = dict(chat)
    result.setdefault("openConversationId", cid)
    result.setdefault("title", chat.get("name") or chat.get("title") or "")
    if "singleChat" not in result:
        result["singleChat"] = self._infer_single_chat(chat)
    return result


def _legacy_normalize_chat_wecom(self: Any, chat: dict) -> dict:
    cid = chat.get("chatid") or chat.get("id") or chat.get("chat_id") or ""
    if not cid:
        return chat
    result = dict(chat)
    result.setdefault("openConversationId", cid)
    result.setdefault("title", chat.get("name") or chat.get("title") or cid)
    if "singleChat" not in result:
        result["singleChat"] = self._infer_single_chat(chat)
    return result


# ── 用例表：覆盖 3 个注入点 + 取值优先级 + int 型 ID 不转 str ───────────────
FEISHU_CASES: list[dict] = [
    {},                                                   # 无 ID → 原样返回
    {"chat_id": "oc_1"},                                  # 仅 chat_id
    {"id": "i_1"},                                        # 仅 id
    {"chat_id": "oc_1", "name": "群A"},                    # 标题取 name
    {"chat_id": "oc_1", "title": "T"},                     # 标题取 title
    {"chat_id": "oc_1"},                                  # 标题缺失 → 回退空串
    {"chat_mode": "p2p", "chat_id": "oc_1"},              # 单聊（chat_mode=p2p）
    {"chat_mode": "group", "chat_id": "oc_1"},            # 群聊
    {"chat_id": "oc_1", "singleChat": False, "chat_mode": "p2p"},  # 预置值不被覆盖
    {"chat_id": 12345, "name": "N"},                      # int 型 ID **不转 str**
    {"chatid": "wrong", "chat_id": "oc_2"},               # chat_id 优先于 chatid
    {"name": "只有名字"},                                  # 无 ID → 原样返回
]

WECOM_CASES: list[dict] = [
    {},                                                   # 无 ID → 原样返回
    {"chatid": "c_1"},                                    # 仅 chatid
    {"id": "i_1"},                                        # 仅 id
    {"chat_id": "c_2"},                                   # 仅 chat_id
    {"chatid": "c_1", "name": "群"},                       # 标题取 name
    {"chatid": "c_1", "title": "T"},                       # 标题取 title
    {"chatid": "c_1"},                                    # 标题缺失 → 回退会话 ID
    {"chat_type": 1, "chatid": "c_1"},                    # 单聊（chat_type=1）
    {"chat_type": 2, "chatid": "c_1"},                    # 群聊
    {"chatid": "c_1", "is_group": True},                  # chat_type 缺失 → 按 is_group 推断
    {"chatid": "c_1", "singleChat": True, "chat_type": 2},  # 预置值不被覆盖
    {"chatid": 12345},                                    # int 型 ID **不转 str**
    {"chatid": "c_1", "chat_id": "c_9"},                  # chatid 优先于 chat_id
    {"name": "只有名字"},                                  # 无 ID → 原样返回
]


def _adapter(cls: type) -> Any:
    """绕过 ``__init__`` 构造实例。

    ``_normalize_chat`` 只用到类属性（chat_id_fields / chat_title_fields /
    title_fallback_to_cid）与 ``_infer_single_chat``，不触碰任何实例状态 →
    无需真实 CLI、无外部依赖，测试纯内存可跑。
    """
    return object.__new__(cls)


def test_feishu_normalize_chat_matches_legacy():
    adapter = _adapter(FeishuCliAdapter)
    for chat in FEISHU_CASES:
        assert adapter._normalize_chat(chat) == _legacy_normalize_chat_feishu(adapter, chat), chat


def test_wecom_normalize_chat_matches_legacy():
    adapter = _adapter(WecomCliAdapter)
    for chat in WECOM_CASES:
        assert adapter._normalize_chat(chat) == _legacy_normalize_chat_wecom(adapter, chat), chat


def test_normalize_chat_is_not_shadowed_by_subclasses():
    """核心不变量：子类不得再 shadow 基类模板方法。

    下沉前的状态是「基类有模板方法、两个子类各自 shadow 出一份」——基类那份
    实际是死代码。下沉后必须只剩一份实现。
    """
    assert FeishuCliAdapter._normalize_chat is BaseIMAdapter._normalize_chat
    assert WecomCliAdapter._normalize_chat is BaseIMAdapter._normalize_chat


def test_infer_single_chat_stays_platform_specific():
    """单聊判定语义确实分叉（飞书 chat_mode / 企微 chat_type），必须各自覆写。"""
    assert FeishuCliAdapter._infer_single_chat is not BaseIMAdapter._infer_single_chat
    assert WecomCliAdapter._infer_single_chat is not BaseIMAdapter._infer_single_chat


def test_platform_injection_points_are_declared():
    """三个注入点必须按平台声明正确（否则归一化会读错字段）。"""
    assert FeishuCliAdapter.chat_id_fields == ("chat_id", "id")
    assert FeishuCliAdapter.title_fallback_to_cid is False
    assert WecomCliAdapter.chat_id_fields == ("chatid", "id", "chat_id")
    assert WecomCliAdapter.title_fallback_to_cid is True
