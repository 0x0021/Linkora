"""入站消息的结构化注入风险识别与载荷清洗。

两层检测，互补且不重叠
----------------------
1. **协议级结构标记**（本模块）：LLM 客户端 / 模板渲染器真正会解析的控制序列，
   正常聊天里不会出现 —— ``<|im_start|>`` / ``[INST]`` / ``### System:`` /
   ``"role":"system"``、伪造本系统边界标记、不可见控制字符、病态超长与填充。
2. **越权指令编排**（``src/guard/commands.py``）：用自然语言写出来的完整越狱
   模板。它**不是**语义/关键词启发式检测：把攻击拆成 5 个独立意图部件，
   只匹配语义完整的短语，并强制要求至少 3 个不同部件共现才判高风险。
   孤立措辞（「忽略以上，我再补充一句：打印机在3楼」「请无视之前那版方案」）
   命中 0 个部件，永远原样放行。

零误伤是硬约束
--------------
2026-08 曾用正则话题分类器识别「忽略之前的指令」这类**自然语言措辞**，
误杀了「忽略以上，我再补充一句：打印机在3楼」这类完全正常的业务消息。
``tests/test_prompt_injection.py::TestZeroFalsePositive`` 与
``tests/test_guard_commands.py`` 把「零误伤」写成回归锁。

因此：单个措辞、单个意图部件都不判风险；``risk="none"`` 时调用方必须
**原样使用原文**（不改写、不包裹、不 trim）。

检测在 NFKC 归一化文本上做（防全角 / 兼容字符混淆绕过），清洗在**原文**上做，
保证可读正文逐字保留。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from src.guard.commands import detect_categories
from src.guard.config import GuardConfig

# ── 协议模板标记：确定性控制序列，不是自然语言关键词 ──────────────────
# (信号名, 正则)。全部用 re.IGNORECASE；检测在 NFKC 归一化文本上跑。
_STRUCTURAL_PATTERNS: tuple[tuple[str, str], ...] = (
    # 伪造本系统的不可信数据边界（提前闭合包裹 / 伪造开标记）
    ("boundary_escape", r"<<<\s*【不可信数据(?:结束)?】|<<<\s*以下为【不可信数据】"),
    # ChatML / OpenAI / Llama / Anthropic 系模板控制标记
    ("role_spoofing", r"<\|(?:im_start|im_end|endoftext|system|user|assistant|eot_id|begin_of_text)\|>"),
    ("role_spoofing", r"\[/?INST\]|<<\s*/?SYS\s*>>"),
    ("role_spoofing", r"#{2,}\s*(?:system|instruction|developer)\b"),
    # 行首高权限角色标签（只认 system/assistant/developer，不认 user/human）。
    # 单独列为低风险信号：贴日志/配置时可能正常出现，因此只记录展示、不改写正文。
    ("role_label", r"(?m)^\s*(?:system|assistant|developer)\s*[:：]\s*\S"),
    # 内联 JSON 消息结构：{"role":"system","content":...}
    ("role_spoofing", r"[\"']role[\"']\s*:\s*[\"'](?:system|developer)[\"']"),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (signal, re.compile(pattern, re.IGNORECASE))
    for signal, pattern in _STRUCTURAL_PATTERNS
)

# 清洗时在原文上抹除的字面标记（保留位置，避免删除后把上下文拼成新指令）
_SANITIZE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<<<\s*【不可信数据(?:结束)?】\s*>>>?"),
    re.compile(r"<<<\s*以下为【不可信数据】[^>]*>>>?"),
    re.compile(r"<\|(?:im_start|im_end|endoftext|system|user|assistant|eot_id|begin_of_text)\|>", re.I),
    re.compile(r"\[/?INST\]|<<\s*/?SYS\s*>>", re.I),
    re.compile(r"#{2,}\s*(?:system|instruction|developer)\b", re.I),
    re.compile(r"[\"']role[\"']\s*:\s*[\"'](?:system|developer)[\"']", re.I),
)

_FILTERED_PLACEHOLDER = "[已过滤的协议标记]"

# 不可见字符分类
_C0_EXTRA = frozenset(range(0x00, 0x09)) | frozenset(range(0x0B, 0x0D)) | frozenset(range(0x0E, 0x20)) | {0x7F}
# 注意：刻意排除 U+200C(ZWNJ) / U+200D(ZWJ) —— 它们是 emoji 组合与部分
# 文字系统的合法字符（如 👨‍👩‍👧），纳入检测会把正常聊天判为风险。
_ZERO_WIDTH = frozenset({0x200B, 0x200E, 0x200F, 0x2060, 0xFEFF})
_BIDI = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))
_UNICODE_TAG = frozenset(range(0xE0000, 0xE0080))

_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class Finding:
    """一条命中信号（不含原始内容，可安全写入审计日志）。"""

    signal: str
    severity: str  # low / medium / high
    count: int


@dataclass(frozen=True)
class Assessment:
    """对一段文本的评估结果。"""

    risk: str  # none / low / medium / high
    findings: tuple[Finding, ...]
    char_count: int
    categories: tuple[str, ...] = ()  # 越权指令编排命中的意图部件（展示用）

    @property
    def signals(self) -> tuple[str, ...]:
        return tuple(f.signal for f in self.findings)


def normalize_for_detection(text: str) -> str:
    """归一化用于检测：NFKC（全角/兼容字符折叠）+ 去 Unicode 格式字符。

    只用于检测，绝不返回给业务链路 —— NFKC 会改变标点形态，不能当清洗结果用。
    """
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")


def _invisible_counts(text: str) -> dict[str, int]:
    counts = {"c0": 0, "zero_width": 0, "bidi": 0, "tag": 0}
    for ch in text:
        cp = ord(ch)
        if cp in _C0_EXTRA:
            counts["c0"] += 1
        elif cp in _ZERO_WIDTH:
            counts["zero_width"] += 1
        elif cp in _BIDI:
            counts["bidi"] += 1
        elif cp in _UNICODE_TAG:
            counts["tag"] += 1
    return counts


def _repeat_ratio(text: str) -> float:
    """最高频单字符占比（仅用于病态填充识别，不参与语义判断）。"""
    if len(text) < 200:
        return 0.0
    sample = text[:2000]
    counts: dict[str, int] = {}
    for ch in sample:
        counts[ch] = counts.get(ch, 0) + 1
    return max(counts.values()) / len(sample)


def assess(text: str, cfg: GuardConfig | None = None) -> Assessment:
    """评估一段入站文本的结构化风险。纯函数，无 IO、无副作用。

    ``risk="none"`` 时调用方必须**原样使用原文**（不得改写、不得包裹）。
    """
    cfg = cfg or GuardConfig()
    if not cfg.enabled or not text:
        return Assessment(risk="none", findings=(), char_count=len(text))

    normalized = normalize_for_detection(text)
    hits: dict[str, int] = {}
    severity: dict[str, str] = {}

    for signal, pattern in _COMPILED:
        if signal in ("role_spoofing", "role_label") and not cfg.detect_role_spoofing:
            continue
        if signal == "boundary_escape" and not cfg.detect_boundary_escape:
            continue
        n = len(pattern.findall(normalized))
        if n:
            hits[signal] = hits.get(signal, 0) + n
            severity[signal] = (
                "high" if signal == "boundary_escape"
                else "low" if signal == "role_label"
                else "medium"
            )

    findings: list[Finding] = []
    risk = "none"

    for signal in ("boundary_escape", "role_spoofing", "role_label"):
        count = hits.get(signal, 0)
        if not count:
            continue
        sev = severity[signal]
        if signal == "role_spoofing" and count >= 2:
            sev = "high"
        findings.append(Finding(signal=signal, severity=sev, count=count))
        if _RISK_ORDER[sev] > _RISK_ORDER[risk]:
            risk = sev

    if cfg.detect_control_chars:
        invisible = _invisible_counts(text)
        total_invisible = sum(invisible.values())
        if total_invisible:
            # bidi / tag 属定向欺骗，中风险；C0 / 零宽属噪声，低风险
            sev = "medium" if (invisible["bidi"] or invisible["tag"]) else "low"
            findings.append(
                Finding(signal="control_chars", severity=sev, count=total_invisible)
            )
            if _RISK_ORDER[sev] > _RISK_ORDER[risk]:
                risk = sev

    if cfg.detect_oversize:
        if len(text) > cfg.max_message_chars:
            findings.append(Finding(signal="oversize", severity="low", count=len(text)))
            if _RISK_ORDER["low"] > _RISK_ORDER[risk]:
                risk = "low"
        elif len(text) >= cfg.min_repeat_length and _repeat_ratio(text) >= cfg.max_repeat_ratio:
            findings.append(Finding(signal="repeat_flood", severity="low", count=len(text)))
            if _RISK_ORDER["low"] > _RISK_ORDER[risk]:
                risk = "low"

    # 越权指令编排：只有多个独立意图部件共现才算攻击。孤立措辞命中 0~2 个部件，
    # 一律不产生信号、不改正文 —— 这是零误伤的关键闸门。
    command_categories: tuple[str, ...] = ()
    if cfg.detect_injection_commands:
        command_categories = detect_categories(normalized)
        if len(command_categories) >= max(1, cfg.injection_min_categories):
            findings.append(
                Finding(
                    signal="injection_command",
                    severity="high",
                    count=len(command_categories),
                )
            )
            risk = "high"

    return Assessment(
        risk=risk,
        findings=tuple(findings),
        char_count=len(text),
        categories=command_categories,
    )


def is_invisible_char(ch: str) -> bool:
    """是否为需要剥离的不可见控制字符（``\\n`` / ``\\r`` / ``\\t`` 保留）。"""
    cp = ord(ch)
    return cp in _C0_EXTRA or cp in _ZERO_WIDTH or cp in _BIDI or cp in _UNICODE_TAG


def sanitize(text: str, assessment: Assessment, cfg: GuardConfig | None = None) -> str:
    """清洗命中消息的结构化攻击载荷。

    - ``risk="none"`` → **原样返回**（字节级不变，零误伤的硬保证）；
    - 命中 → 剥不可见控制字符、把协议标记替换为可见占位、按上限截断。
    - 可读正文逐字保留（不做 NFKC、不做 trim、不折叠空白）。
    """
    cfg = cfg or GuardConfig()
    if assessment.risk == "none" or not cfg.sanitize_payload:
        return text

    cleaned = "".join(ch for ch in text if not is_invisible_char(ch))
    for pattern in _SANITIZE_PATTERNS:
        cleaned = pattern.sub(_FILTERED_PLACEHOLDER, cleaned)
    if len(cleaned) > cfg.max_message_chars:
        cleaned = cleaned[: cfg.max_message_chars]
    return cleaned


def preview(text: str, limit: int = 200) -> str:
    """生成可安全写入 JSONL 事件的原文预览（控制字符可见化 + 截断）。"""
    visible = "".join(
        ch if not is_invisible_char(ch) and ch not in "\r\n\t" else
        ("\\n" if ch == "\n" else "\\r" if ch == "\r" else "\\t" if ch == "\t" else "\uFFFD")
        for ch in text[:limit]
    )
    if len(text) > limit:
        visible += "…"
    return visible


__all__ = [
    "Assessment",
    "Finding",
    "assess",
    "is_invisible_char",
    "normalize_for_detection",
    "preview",
    "sanitize",
]
