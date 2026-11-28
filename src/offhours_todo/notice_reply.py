"""工作时间外待办回执文案：LLM 生成 + 硬约束校验 + 固定文案回退。

回执是以主人账号发给真人的消息，方向一旦跑偏（承诺时间、声称已处理、要求对方
做事、夹带无关内容）比不发更糟。故这里不把对方的原始正文交给模型（避免提示注入），
只用固定指令生成一句短回执，再用**结构正则 + 禁词表**校验；任何一步不合格都返回
空串，由调用方回退到 ``unreplied_todo.sender_notice_text`` 原固定文案。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.exceptions import LLMError
from src.llm.usage_ledger import purpose_scope
from src.utils.media_text import strip_media_refs

logger = logging.getLogger(__name__)

DEFAULT_SENDER_NOTICE_TEXT = "你的消息我已记入待办，稍后回复，谢谢～"

_NOTICE_SYSTEM_PROMPT = """你负责替主人写一条「工作时间外自动回执」，发给刚留言的人。
必须按顺序、在同一句里完整表达四件事：① 消息已收到；② 已记入待办；③ 恢复工作后会查看或回复；④ 感谢理解。
语气友好自然、简洁，20~60 字，以「收到」或「你的消息收到」开头，只输出回执正文。

严禁（出现任何一条都不合格）：
- 具体回复时间或时间承诺：今天、明天、上午、下午、几点、几分钟/小时/天后、尽快、马上、立刻、稍后、稍等；
- 声称已经处理、解决、完成或答复完毕；
- 让对方做任何事：提问、请稍等、请再发一遍、请补充信息、留电话/微信/邮箱、点链接；
- 媒体标记、链接、编号、称呼、落款，以及上述四件事以外的任何内容。
"""

# 结构闸门：四层语义缺一不可、顺序固定、句内不另起一句，杜绝夹带无关内容。
_NOTICE_SHAPE = re.compile(
    r"^(?:你的消息|消息)?(?:已)?(?:收到|看到|知悉)"
    r"[^。！？；;]{0,24}"
    r"(?:已)?(?:记入|记下|记录|登记|放进)"
    r"[^。！？；;]{0,8}待办"
    r"[^。！？；;]{0,24}"
    r"(?:恢复工作|回到工位|回到岗位|上班后|开工后|恢复办公)"
    r"[^。！？；;]{0,16}"
    r"(?:查看|回复|答复|跟进|看)"
    r"[^。！？；;]{0,16}"
    r"(?:谢谢|感谢)"
    r"[^。！？；;]{0,8}"
    r"[。！!～~]?$"
)

# 禁词闸门：比结构闸门更直接地拦住「方向偏了」的高危表述。
_BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("contains_digit", re.compile(r"\d")),
    ("specific_time", re.compile(
        r"今天|明天|后天|今晚|今早|本周|下周|上周|上午|下午|中午|晚上|凌晨"
        r"|周末|周[一二三四五六日天]|星期[一二三四五六日天]|节假日"
        r"|半小时|一小时|几分钟|小时后|分钟后|天后|马上|立刻|立即"
        r"|尽快|第一时间|随时|稍后|稍等|几点|点钟|点整"
    )),
    ("claims_done", re.compile(
        r"已(?:经)?(?:处理|解决|完成|办妥|回复|答复|搞定)"
        r"|处理好了|解决了|办好了|搞定了|无需(?:处理|回复)|不用(?:处理|回复)"
    )),
    ("asks_user", re.compile(
        r"请|麻烦|再发|重发|补充|提供|联系|拨打|邮箱|微信|电话|扫码|点击"
        r"|耐心等待|方便的话|有急事|[?？]"
    )),
    ("unrelated", re.compile(
        r"另外|顺便|对了|记得|提醒|还有|附注|补充|祝|再次|此外|链接|网址"
        r"|https?://|www\.|mediaId|fileId|fileName|[\[\]{}<>|#*`]"
    )),
)

_MAX_NOTICE_CHARS = 80
_LLM_ERRORS = (
    LLMError, OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError,
)


def normalize_notice_text(text: str) -> str:
    """去掉模型常见的包装（引号/换行），不做任何改写语义的动作。"""
    cleaned = str(text or "").strip().strip("\"'“”‘’「」『』")
    return re.sub(r"\s+", " ", cleaned).strip()


def validate_notice_text(text: str) -> str:
    """校验回执文案。返回空串表示通过；否则返回失败原因码（仅用于日志）。"""
    cleaned = normalize_notice_text(text)
    if not cleaned:
        return "empty"
    if len(cleaned) > _MAX_NOTICE_CHARS:
        return "too_long"
    if strip_media_refs(cleaned, placeholder="") != cleaned:
        return "media_ref"
    for reason, pattern in _BANNED_PATTERNS:
        if pattern.search(cleaned):
            return reason
    if not _NOTICE_SHAPE.fullmatch(cleaned):
        return "unsupported_shape"
    return ""


def generate_notice_text(llm: Any) -> str:
    """调 LLM 生成回执；不可用 / 异常 / 校验不过一律返回空串（调用方回退固定文案）。"""
    if llm is None or not callable(getattr(llm, "chat", None)):
        return ""
    try:
        with purpose_scope("other"):
            resp = llm.chat(
                [
                    {"role": "system", "content": _NOTICE_SYSTEM_PROMPT},
                    {"role": "user", "content": "请生成一条工作时间外留言的回执。"},
                ],
                temperature=0.7,
            )
    except _LLM_ERRORS as e:
        logger.info("[未回复待办] LLM 回执生成不可用，回退固定文案: %s", e)
        return ""
    raw = str(getattr(resp, "content", "") or "")
    text = normalize_notice_text(raw)
    reason = validate_notice_text(text)
    if reason:
        logger.info("[未回复待办] LLM 回执未通过校验(%s)，回退固定文案: %.80s", reason, raw)
        return ""
    return text
