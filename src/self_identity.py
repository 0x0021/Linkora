"""自身身份判定的唯一口径（防「自己回自己」/ 自言自语）。

背景（2026-11-06 事故）：钉钉 list-all 把用户本人发的消息回灌进轮询器时，
消息里的 ``sender`` 是**昵称**，而通讯录里的 ``userName`` 是**正式姓名**，
两者字面不同；``senderId`` / ``senderOpenDingTalkId`` 是 openDingTalkId，与
profile 里的 userId 也不同。旧逻辑只比 userId、姓名兜底又要求 sender_id 为空，
于是本人消息被判成「对方」→ 触发 LLM → 回复按 peer_oid 发回自己的 1:1 窗口。

本模块提供两类能力，poller / runtime / 发送护栏共用同一套判定，避免各处
口径漂移：

1. :func:`is_self_message` —— 多信号判定一条消息是否由本人发出；
2. :func:`learn_self_from_single_chats` —— 从 list-all 的单聊消息里反推自身
   openDingTalkId 与姓名别名（昵称），**不依赖配置、不依赖通讯录、不硬编码姓名**。

统计法的依据：每个 1:1 会话的参与者必然包含「我」，因此本人 openDingTalkId
会横跨几乎所有单聊会话；任何其他人只会出现在与其相关的少数会话里。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

# AI 助手身份的历史标记（兼容旧数据）
_BOT_NAMES = frozenset({"AI助手", "AI 助手"})
_BOT_IDS = frozenset({"ai"})


def normalize_name(value: Any) -> str:
    """姓名归一化：转 str + 去首尾空白（钉钉昵称常带空格）。"""
    return str(value or "").strip()


def collect_self_ids(*values: Any) -> frozenset[str]:
    """收敛所有已知的自身标识（openDingTalkId / userId 等），去空去重。"""
    out: set[str] = set()
    for v in values:
        s = str(v or "").strip()
        if s:
            out.add(s)
    return frozenset(out)


def collect_self_names(name: Any, aliases: Iterable[Any] = ()) -> frozenset[str]:
    """收敛自身姓名集合：正式姓名 + 别名（昵称 / 显示名）。"""
    out = {normalize_name(name)}
    out.update(normalize_name(a) for a in aliases)
    out.discard("")
    return frozenset(out)


def _raw_sender_ids(raw: Mapping[str, Any] | None) -> tuple[str, ...]:
    """从原始消息 dict 里抽出所有可能的发送者标识字段。"""
    if not isinstance(raw, Mapping):
        return ()
    keys = ("senderOpenDingTalkId", "senderId", "openDingTalkId", "senderUserId")
    return tuple(str(raw.get(k) or "").strip() for k in keys)


def is_self_message(*, sender_id: Any = "", sender_name: Any = "",
                    raw: Mapping[str, Any] | None = None,
                    self_ids: frozenset[str] | set[str],
                    self_names: frozenset[str] | set[str]) -> bool:
    """判断一条消息是否由本人（或本 AI 分身）发出。

    判定优先级：
    1. AI 助手的历史标记；
    2. sender_id / raw 里的任一 ID 命中已知自身 ID（最强证据）；
    3. 姓名命中正式名或别名，且没有「非自身 ID」作为反证（防组织内同名误杀）。

    「防同名」与「别漏判」的取舍：姓名匹配时若 ``sender_id`` 有值且不在自身 ID
    集合里，不能直接判自我（否则同名同事的消息会被吞掉），需要由调用方补一个
    强证据（如 DB 里的 assistant 回复回灌），见 ``DedupMixin._is_self_message``。
    """
    sid = str(sender_id or "").strip()
    name = normalize_name(sender_name)
    raw_ids = _raw_sender_ids(raw)

    if name in _BOT_NAMES or sid in _BOT_IDS:
        return True
    if sid and sid in self_ids:
        return True
    if any(x and x in self_ids for x in raw_ids):
        return True
    if name and name in self_names:
        if not sid or sid in self_ids:
            return True
        if any(x and x in self_ids for x in raw_ids):
            return True
    return False


def _is_single_conversation(conv: Mapping[str, Any]) -> bool:
    """list-all 会话是否是单聊；会话层缺字段时用消息层的 singleChat 兜底。"""
    flag = conv.get("singleChat")
    if flag is None:
        msgs = conv.get("messages") or []
        flag = msgs[0].get("singleChat") if msgs and isinstance(msgs[0], Mapping) else None
    if isinstance(flag, str):
        return flag.strip().lower() in ("true", "1", "yes")
    return bool(flag)


def learn_self_from_single_chats(
    conversations: Iterable[Mapping[str, Any]],
    *,
    known_ids: Iterable[Any] = (),
    known_names: Iterable[Any] = (),
    min_conversations: int = 3,
    min_ratio: float = 0.6,
) -> tuple[str, frozenset[str]]:
    """从 list-all 的单聊消息反推「自身 openDingTalkId + 姓名别名」。

    返回 ``(open_dingtalk_id, aliases)``；证据不足时返回 ``("", frozenset())``，
    调用方应保留已有的降级路径，绝不因学习失败而阻断启动。

    ``known_ids`` / ``known_names`` 命中的候选会被优先采纳（例如消息里直接带了
    通讯录 userId 或正式姓名），但仍要求它满足单聊覆盖度门槛，避免把偶然同名
    或某个活跃同事误认成自己。
    """
    known_id_set = collect_self_ids(*known_ids)
    known_name_set = collect_self_names("", known_names)
    single = [c for c in conversations if isinstance(c, Mapping) and _is_single_conversation(c)]
    if len(single) < min_conversations:
        return "", frozenset()

    coverage: dict[str, set[int]] = defaultdict(set)
    names: dict[str, Counter[str]] = defaultdict(Counter)
    id_hits: dict[str, int] = defaultdict(int)
    name_hits: dict[str, int] = defaultdict(int)

    for idx, conv in enumerate(single):
        msgs = conv.get("messages") or []
        if not isinstance(msgs, list):
            continue
        for msg in msgs:
            if not isinstance(msg, Mapping):
                continue
            oid = str(msg.get("senderOpenDingTalkId") or "").strip()
            if not oid:
                continue
            coverage[oid].add(idx)
            nm = normalize_name(msg.get("sender"))
            if nm:
                names[oid][nm] += 1
            for rid in _raw_sender_ids(msg):
                if rid and rid in known_id_set and rid == oid:
                    id_hits[oid] += 1
            if nm and nm in known_name_set:
                name_hits[oid] += 1

    if not coverage:
        return "", frozenset()

    # 覆盖度优先，其次用已知 ID / 已知姓名命中数打破平局。
    def _rank(item: tuple[str, set[int]]) -> tuple[int, int, int]:
        oid, hit = item
        return (len(hit), id_hits[oid] + name_hits[oid], len(oid))

    best_oid, hit = max(coverage.items(), key=_rank)
    if len(hit) < min_conversations or len(hit) / len(single) < min_ratio:
        return "", frozenset()

    aliases = frozenset(n for n, _ in names[best_oid].most_common())
    aliases -= known_name_set  # 只回传新增的别名
    return best_oid, aliases
