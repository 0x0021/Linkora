"""安全工具函数——敏感信息脱敏、输入校验等。"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 常见敏感标识正则（用于通用脱敏兜底）
_SENSITIVE_PATTERNS = [
    re.compile(r'[A-Za-z0-9+/]{20,}={0,2}'),  # Base64 token
    re.compile(r'Bearer\s+[A-Za-z0-9\-_.~+]+'),  # Bearer token
    re.compile(r'openDingTalkId[=:]\s*[A-Za-z0-9]+'),  # DingTalk open ID
    re.compile(r'userId[=:]\s*[A-Za-z0-9]+'),  # User ID
]


def mask_oid(oid: str, visible_prefix: int = 2, visible_suffix: int = 2) -> str:
    """脱敏 openDingTalkId / userId 等敏感标识：仅保留首尾各 N 位。

    Args:
        oid: 原始标识符
        visible_prefix: 保留的前缀字符数，默认 2
        visible_suffix: 保留的后缀字符数，默认 2

    Returns:
        脱敏后的字符串，如 "ab***yz"
    """
    if not oid:
        return ""
    if len(oid) <= visible_prefix + visible_suffix:
        return "*" * max(len(oid), 3)
    return f"{oid[:visible_prefix]}***{oid[-visible_suffix:]}"


def mask_token(token: str, visible_chars: int = 4) -> str:
    """脱敏 API Token：仅显示前 4 位。

    Args:
        token: 原始 token
        visible_chars: 可见字符数

    Returns:
        脱敏后的字符串，如 "sk-ab12***"
    """
    if not token:
        return ""
    token_str = str(token)
    if len(token_str) <= visible_chars:
        return "*" * len(token_str)
    return f"{token_str[:visible_chars]}***"


def mask_sensitive_text(text: str, max_length: int = 100) -> str:
    """对文本进行通用敏感信息脱敏（兜底）。

    适用于无法明确知道字段含义的异常堆栈或日志内容。
    """
    if not text:
        return ""
    # 限制长度避免日志爆炸
    text = text[:max_length]
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub(lambda m: m.group(0)[:4] + "***" + m.group(0)[-4:], text)
    # 对长随机字符串也做脱敏
    text = re.sub(r'\b[A-Za-z0-9+/]{30,}={0,2}\b', '***BASE64***', text)
    return text


def sanitize_log_message(msg: str) -> str:
    """对日志消息做最终脱敏处理（防止漏网之鱼）。

    应用于所有含用户标识的日志输出。
    """
    # 对 openDingTalkId 格式做兜底脱敏
    msg = re.sub(
        r'openDingTalkId[=:]\s*([A-Za-z0-9]{8,})',
        lambda m: f'openDingTalkId={mask_oid(m.group(1))}',
        msg,
    )
    # 对孤立长标识符做脱敏（纯字母数字，长度 > 20）
    msg = re.sub(
        r'\b([A-Za-z0-9_-]{20,})\b',
        lambda m: mask_oid(m.group(1)) if not m.group(1).startswith('http') else m.group(1),
        msg,
    )
    return msg


def is_safe_ip(ip: str) -> bool:
    """检查 IP 是否为安全的公网地址（非内网/保留地址）。

    Args:
        ip: IPv4 地址字符串

    Returns:
        True 如果是安全的公网 IP
    """
    if not ip:
        return False
    parts = ip.split('.')
    if len(parts) != 4:
        return False
    try:
        first_octet = int(parts[0])
        second_octet = int(parts[1])
    except ValueError:
        return False
    # 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 0.0.0.0
    if first_octet == 10:
        return False
    if first_octet == 172 and 16 <= second_octet <= 31:
        return False
    if first_octet == 192 and second_octet == 168:
        return False
    if first_octet == 127:
        return False
    if first_octet == 0:
        return False
    return True


def validate_platform_id(platform_id: str) -> bool:
    """校验平台 ID 是否合法。

    Args:
        platform_id: 平台标识符

    Returns:
        True 如果是已知的合法平台
    """
    known_platforms = {"dingtalk", "feishu", "wecom"}
    return platform_id in known_platforms


def safe_get_dict(obj: Any, *keys: str, default: Any = None) -> Any:
    """安全地从嵌套字典中取值，避免 KeyError。

    Usage:
        safe_get_dict(data, "user", "profile", "name", default="未知")
    """
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current
