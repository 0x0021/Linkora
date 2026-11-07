"""表情回复（emoji reaction）→ 请求「已完成」信号解析。

背景：钉钉的表情回复是消息的元数据，不是独立消息，不会进入 Linkora 的消息轮询流。
因此当主人用「Done / ✅ / OK」等表情回应一条「对方提出的请求」时，纯 LLM 摘要
无法感知，导致请求在摘要/主动汇报里长期显示为待处理（2026-11-03 一次「导出权限」
请求即此案例：主人回了 Done 表情，摘要仍当未办）。

本模块把 dws ``chat message list-emotion-replies`` 返回的 reaction 数据，归一化为
「哪些消息被主人用完成类表情标记了」的信号，供摘要链路注入提示词。

设计约束：
- 纯函数为主，便于单测、不碰 IO。
- 完成类表情取**明确语义**集合，避免过度宽泛导致误判（如「赞 / 感谢 / 鼓掌」不视为完成）。
- 必须判定 reaction 来自**主人本人**（按 sender_name 或 owner_ids 匹配），否则群里第三人
  回 Done 会错误地把别人的请求标成已办。
- owner_ids 与 owner_name 任一命中即可，适应「只有名字 / 只有 id」的不同来源。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

# 完成类表情（归一化：去空白 + 小写）。覆盖钉钉默认表情的 name/en_US 与常见符号。
# 故意不含「赞 / 感谢 / 鼓掌 / 666」等模糊语义，避免把「礼貌性回应」误判为「已办」。
_COMPLETION_TOKENS: frozenset[str] = frozenset({
    "done", "ok", "完成", "搞定", "已办", "已完成", "处理完", "处理完毕",
    "done.", "任务完成", "办结",
})

# 直接用符号表示的完成类表情（这些 unicode 字符归一化后不会被上面的小写集合覆盖）。
_COMPLETION_SYMBOLS: frozenset[str] = frozenset({
    "✅", "✔", "✓", "☑", "✔️", "👍", "🆗",
})


def is_completion_emoji(emoji: str) -> bool:
    """判断一个表情（emoji name / en_US / 符号）是否表示「已完成」。

    Args:
        emoji: dws 返回的表情标识。钉钉默认表情通常是中文 name（如「Done」的 name 即
            ``Done``，en_US 也是 ``Done``）或 unicode 符号（如 ✅）。
    """
    if not emoji:
        return False
    e = emoji.strip()
    if not e:
        return False
    # 符号直接命中
    if e in _COMPLETION_SYMBOLS:
        return True
    # 小写归一化后命中词表
    return e.lower() in _COMPLETION_TOKENS


def _norm(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def collect_owner_resolved_msg_ids(
    reactions_by_msg: Mapping[str, Iterable[dict[str, Any]]],
    owner_ids: Iterable[str] | None = None,
    owner_name: str | None = None,
) -> set[str]:
    """从 reaction 数据里挑出「被主人用完成类表情标记」的消息 id 集合。

    Args:
        reactions_by_msg: ``{msg_id: [reaction, ...]}``，reaction 为已归一化的
            ``{emoji, sender_id, sender_name}``（见 dws 适配器归一化层）。
        owner_ids: 主人的 user/openDingTalkId 等 id 集合（来自 contact_user_get_self 等）。
        owner_name: 主人的显示名（与消息 sender_name 同口径）。

    Returns:
        被主人标记完成的 msg_id 集合（空集合表示无）。
    """
    owner_id_set = {_norm(i) for i in (owner_ids or []) if _norm(i)}
    owner_name_norm = _norm(owner_name).lower()

    resolved: set[str] = set()
    for msg_id, reacts in (reactions_by_msg or {}).items():
        if not msg_id:
            continue
        for r in (reacts or []):
            if not is_completion_emoji(r.get("emoji") or ""):
                continue
            rid = _norm(r.get("sender_id"))
            rname = _norm(r.get("sender_name")).lower()
            is_owner = (rid and rid in owner_id_set) or (
                owner_name_norm and rname == owner_name_norm
            )
            if is_owner:
                resolved.add(msg_id)
                break  # 本条消息已确认被主人标记完成，无需再看下一个 reaction
    return resolved


def build_resolution_note(
    resolved_msg_ids: Iterable[str],
    messages: list,
) -> str:
    """为摘要提示词生成「已办」约束段落。

    列出被主人标记完成的消息（带时间 + 发言者 + 内容预览），让 LLM 在摘要里
    把对应请求/待办标注为「（已办）」，而不是继续当作未处理事项。

    Args:
        resolved_msg_ids: ``collect_owner_resolved_msg_ids`` 的结果。
        messages: 本次参与摘要的 Message 列表（仅用于取预览文本）。

    Returns:
        非空约束段落；resolved 为空时返回空串（调用方据此决定是否注入）。
    """
    resolved = set(resolved_msg_ids or ())
    if not resolved:
        return ""

    # 用 msg_id 反查消息行，构造可读预览（按原顺序，保证与对话素材对应）
    lines: list[str] = []
    for m in messages:
        mid = getattr(m, "msg_id", "")
        if mid not in resolved:
            continue
        ts = getattr(m, "timestamp", None)
        time_str = ts.strftime("%H:%M") if ts else "--:--"
        sender = getattr(m, "sender_name", "") or "对方"
        content = (getattr(m, "content", "") or "").strip().replace("\n", " ")
        preview = content[:80] + ("…" if len(content) > 80 else "")
        lines.append(f"- [{time_str}] {sender}: {preview}")

    if not lines:
        return ""

    bullet = "\n".join(lines)
    return (
        "\n9. ★ 完成状态标记（最高优先级）：下面这些消息已被**主人本人**用表情回复"
        "（Done / ✅ / OK 等）明确标记为「已完成」。若它们是「对方提出的请求 / 待办」，"
        "摘要中必须在对应事项后明确标注「（已办）」，严禁继续当作未处理的待办或追问。\n"
        "已被标记完成的消息：\n"
        f"{bullet}\n"
    )
