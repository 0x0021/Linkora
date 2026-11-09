"""出站回复正文的敏感凭据脱敏（输出内容过滤）。

只处理**确定性凭据形态**，不碰自然语言：AK/SK、Bearer/JWT、私钥块、
``key=...`` 形式的高熵串。命中即替换为占位符，避免 RAG 文档 / 工具返回值里
夹带的密钥被模型复述到 IM 群里。

与 ``safety.sensitive_words``（整条拦截）互补：这里做**替换**而非拦截，
不改变回复是否发出，因此不影响业务流程。
"""
from __future__ import annotations

import re

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[已脱敏的私钥]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "[已脱敏的令牌]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*", re.IGNORECASE),
     "Bearer [已脱敏的令牌]"),
    (re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{16,}\b"),
     "[已脱敏的密钥]"),
    (re.compile(r"\b(?:AKID|LTAI)[A-Za-z0-9]{12,}\b"),
     "[已脱敏的访问密钥]"),
    # key/token/secret 后跟高熵值（>=20 位），避免误伤普通短参数
    (re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\s*[:=]\s*[\"']?[A-Za-z0-9/+_-]{20,}[\"']?",
                re.IGNORECASE),
     "[已脱敏的凭据]"),
)


def redact_secrets(text: str) -> tuple[str, int]:
    """返回 ``(脱敏后的文本, 替换次数)``；无命中时文本原样返回、计数 0。"""
    if not text:
        return text, 0
    total = 0
    out = text
    for pattern, placeholder in _REDACTIONS:
        out, count = pattern.subn(placeholder, out)
        total += count
    return out, total


__all__ = ["redact_secrets"]
