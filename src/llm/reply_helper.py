"""回复补全辅助函数。

从 src.llm.agent 拆出——处理弱模型早停导致的截断回复补全逻辑。
"""
from __future__ import annotations

import logging

# 直接从 src.llm.reply 取（style 只是转存并反向 import 本模块，
# 走 style 会形成 style ↔ reply_helper 循环导入，单独 import 本模块即炸）。
from src.llm.reply import (
    _REPLY_CONNECTORS,
    _is_reply_incomplete,
    _split_sentences,
    _segment_is_incomplete,
    enforce_brevity,
)

logger = logging.getLogger(__name__)


def ensure_complete_reply(
    text: str,
    client,
    auto_complete_enabled: bool = True,
    agent: object | None = None,
) -> str:
    """末尾自动续写补全（截断确定性修复）。

    弱模型/早停可能导致回复以连接词（及/与/然后…）、逗号或无结尾标点收尾。
    检测到不完整时，用一次低成本 LLM 续写把句子补全到正常句号/问号；
    仅尝试一次，失败（限流/网络/解析）即降级返回原文，绝不阻塞主回复。
    补全后经 enforce_brevity 再次清洗（清泄漏/粘连标点/主人身份词）。
    可用 auto_complete_enabled 关闭。

    分段续写（2026-07-28）：只把「第一个断点句（含之前）」喂给续写器补全，
    再把原始后半段拼回，避免后面的完整句把续写器带偏、也不丢内容。

    ★ 2026-09-02 修复：找不到句子级断点（break_idx<0）时，**绝不把整段原文**
    丢给续写器。整段原文句子层面都是完整的，模型只会把它理解成"接着说下一句"，
    从而凭空编出新内容（曾编出冒充对方口吻的"明白了，我这就去…"并对外发出）。
    此时只把最后一个句末标点之后的那一小段尾巴喂给续写器。

    agent 参数用于让 enforce_brevity 读到真实配置（长度上限）；传 None 时会
    退回默认 150 字上限，非结构化长回复可能被误截。
    """
    if not auto_complete_enabled:
        return text
    if not text or not _is_reply_incomplete(text):
        return text
    try:
        # 找第一个不完整断点（以连接词/逗号收尾的句），只补全到该断点。
        segments = _split_sentences(text)
        break_idx = -1
        for i, seg in enumerate(segments):
            if _segment_is_incomplete(seg):
                break_idx = i
                break
        messages = [
            {"role": "system", "content": (
                "你是文本续写器。下面是一句话的未完部分，请只输出它缺失的结尾，"
                "使其成为一个语法完整、以句号或问号正常结尾的句子。"
                "严禁重复已给出的内容，不要添加任何解释、前缀、引号或换行。"
                "你只能补全这一句的结尾：严禁另起新的一句、严禁写新的话题、"
                "严禁模拟或代替对方（用户）说话、严禁编造任何不存在的信息。"
            )},
        ]
        if break_idx >= 0:
            partial = "".join(segments[:break_idx + 1]).rstrip()
            rest = "".join(segments[break_idx + 1:])
            messages.append({"role": "user", "content": partial})
            resp = client.chat(messages, stream=False, temperature=0.2)
            cont = getattr(resp, "content", "") or ""
            cont = cont.strip()
            if not cont:
                return text
            # 去重：若 partial 末尾（去掉句末标点）以连接词结尾、且续写以同词开头，
            # 削掉续写开头的连接词，避免"及。及"这类重复。
            tail = partial.rstrip().rstrip("。！？!?")
            for c in _REPLY_CONNECTORS:
                if tail.endswith(c) and cont.startswith(c):
                    cont = cont[len(c):].lstrip()
                    break
            # 拼接：补后的前半段 + 原始后半段（断点之后的所有句子，原样保留）。
            combined = partial.rstrip().rstrip("。！？!?") + cont + rest
        else:
            # 句子层面都完整 —— 只有末尾那一小段（最后一个句末标点之后）没收尾。
            # 只补这段尾巴，绝不拿整段原文去诱导模型"接着说下一句"。
            tail_seg = segments[-1].strip() if segments else ""
            if not tail_seg:
                # 整段连一个句末标点都没有：确属被截断的长句，只能整段续写。
                tail_seg = text.rstrip()
            messages.append({"role": "user", "content": tail_seg})
            resp = client.chat(messages, stream=False, temperature=0.2)
            cont = getattr(resp, "content", "") or ""
            cont = cont.strip()
            if not cont:
                return text
            combined = text.rstrip() + cont
        finalized = enforce_brevity(agent, combined)
        if not _is_reply_incomplete(finalized):
            logger.info("[续写补全] 已补全截断回复: %d -> %d 字符 (断点句=%d/%d)",
                        len(text), len(finalized), break_idx + 1, len(segments))
            return finalized
        # 仍不完整（极少见）：返回最佳努力结果，不再二次调用 LLM。
        logger.warning("[续写补全] 续写后仍未完整，返回最佳努力结果")
        return finalized
    except Exception as e:  # 任意异常均降级，保证主回复不受影响
        logger.warning("[续写补全] 调用失败，降级返回原文: %s", e)
        return text
