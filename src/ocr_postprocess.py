"""OCR 后处理管线 —— 在 OCR 识别结果投喂 LLM 之前做可配置的多步清洗。

设计为 Pipeline 模式，每个步骤独立开关，config.yaml 的 [ocr_postprocess] 节控制。
本模块是纯函数层，不持有状态，不依赖项目其他模块（仅依赖 config 读取开关）。
"""

from __future__ import annotations

import re
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# ── 工具字符串常量 ──
_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\u2060\ufeff]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_REPEAT_PUNCT = re.compile(r"([。，！？、；：…—,.!?;:])\1{2,}")
_FILLER_WORDS_RE = re.compile(
    r"\b(?:呃|啊|嗯|哦|噢|哎|哈|嘿嘿|那个|就是说|就是说呢|然后然后|"
    r"这个|这个嘛|这样|那样|反正|其实|怎么说呢|怎么说|那么|"
    r"对了|就是说嘛|嘛|吧|呢|啦|呀|哟|诶|咦|呵|喔)\b"
)
_MULTI_BLANK = re.compile(r"\n{3,}")
_WHITESPACE_LINE = re.compile(r"^\s+$")
_CJK_EN_SPACE = re.compile(
    r"([\u4e00-\u9fff\u3400-\u4dbf])([A-Za-z0-9@#])|"
    r"([A-Za-z0-9@#])([\u4e00-\u9fff\u3400-\u4dbf])"
)


# ── Pipeline Step Functions ──

def _step_remove_invisible(text: str) -> str:
    """Step 1: 剔除零宽字符、不可见控制字符。"""
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL_CHARS.sub("", text)
    return text


def _step_dedup_punctuation(text: str) -> str:
    """Step 2: 连续重复标点符号压缩为 1 个（保留开头的 2 个连续）。"""
    def _reduce(m: re.Match) -> str:
        p = m.group(1)
        return p * 1
    return _REPEAT_PUNCT.sub(_reduce, text)


def _step_remove_fillers(text: str) -> str:
    """Step 3: 剔除口语填充词/语气词。"""
    return _FILLER_WORDS_RE.sub("", text)


def _step_normalize_layout(text: str) -> str:
    """Step 4: 排版规范化 —— 合并连续空行、去除首尾空白、去除仅空白行。"""
    lines = text.split("\n")
    # 去掉纯空白行
    lines = [line.strip() for line in lines if not _WHITESPACE_LINE.match(line)]
    text = "\n".join(lines)
    # 合并连续空行（≥3 个换行 → 2 个换行，保留段落间距）
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def _step_cjk_spacing(text: str) -> str:
    """Step 5: 中文/英文/数字间合理加空格（可选）。"""
    # a-zA-Z0-9@# ↔ 中文字符（U+4E00-9FFF / U+3400-4DBF）
    return _CJK_EN_SPACE.sub(r"\1\3 \2\4", text)


# ── Pipeline 入口 ──

_PIPELINE_STEPS: list[tuple[str, Callable[[str], str]]] = [
    ("remove_invisible", _step_remove_invisible),
    ("dedup_punctuation", _step_dedup_punctuation),
    ("remove_fillers", _step_remove_fillers),
    ("normalize_layout", _step_normalize_layout),
    ("cjk_spacing", _step_cjk_spacing),
]


def run_ocr_postprocess(text: str, min_chars: int = 5) -> tuple[str, bool]:
    """对 OCR 原始文本执行可配置的后处理管线。

    每个步骤受 config.yaml [ocr_postprocess] 节的独立开关控制。
    如果处理后的有效字符数 < min_chars，标记为跳过。

    Args:
        text: OCR 原始文本。
        min_chars: 有效字符数阈值，低于此值标记跳过。

    Returns:
        (processed_text, skipped): skipped=True 表示文本过短应跳过不投喂 LLM。
    """
    if not text or not text.strip():
        return "", True

    # 懒加载 config 获取 [ocr_postprocess] 节
    try:
        from src.config import get_config
        cfg = get_config()
        ocfg = getattr(cfg, "ocr_postprocess", None)
    except Exception:
        logger.debug("无法加载 ocr_postprocess 配置，使用默认全开启", exc_info=True)
        ocfg = None

    enabled_steps: dict[str, bool] = {}
    if ocfg is not None:
        enabled_steps = getattr(ocfg, "enabled_steps", {}) or {}
        if not isinstance(enabled_steps, dict):
            enabled_steps = {}

    # 默认：所有步骤全开
    defaults = {
        "remove_invisible": True,
        "dedup_punctuation": True,
        "remove_fillers": True,
        "normalize_layout": True,
        "cjk_spacing": False,
    }

    result = text
    for name, func in _PIPELINE_STEPS:
        is_on = enabled_steps.get(name, defaults.get(name, True))
        if is_on:
            try:
                result = func(result)
            except Exception:
                logger.debug("OCR 后处理步骤 %s 失败，跳过", name, exc_info=True)

    # 计算有效字符数（扣除空白）
    effective = len(result.strip())
    if effective < min_chars:
        logger.info("OCR 后处理：文本过短（%d 有效字符 < %d），标记跳过 LLM", effective, min_chars)
        return result, True

    return result, False
