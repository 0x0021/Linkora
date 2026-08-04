"""dws_adapter 核心：异常别名 + 错误分类函数。拆分自 dws_adapter.py。"""
from __future__ import annotations

import os

from src.im_adapter.errors import (
    IMAdapterError,
    IMAdapterNonRetryableError,
    IMAdapterPermissionError,
    IMAdapterRetryableError,
)

# 构建无浏览器弹窗的子进程环境：覆盖 BROWSER / DISPLAY 等变量，
# 从 OS 层面阻止 dws CLI（或其内部 OAuth 流程）弹出授权窗口。
_NO_BROWSER_ENV: dict[str, str] = {
    **os.environ,
    "BROWSER": "/bin/echo",       # webbrowser.open() → /bin/echo 吸收 URL 不打开
    "DISPLAY": "",                 # 空 DISPLAY 阻止 GUI 调用
    "WSL_DISTRO_NAME": "",        # 避免 WSL 浏览器回退
    "BROWSER_NON_INTERACTIVE": "1",
}


# 向后兼容别名：旧代码 import 的 Dws* 异常 = 通用 IMAdapter* 异常
DwsError = IMAdapterError
DwsRetryableError = IMAdapterRetryableError
DwsNonRetryableError = IMAdapterNonRetryableError
DwsPermissionError = IMAdapterPermissionError


def is_permission_error(error_msg: str) -> bool:
    """判断是否为权限类错误或认证失效。"""
    return any(
        code in error_msg
        for code in (
            "TOKEN_VERIFIED_FAILED",
            "该组织尚未开启 CLI 数据访问权限",
            "is not in conversation",
            "AGENT_CODE_NOT_EXISTS",  # 认证会话失效，需要重新登录
            "AUTH_PERMISSION_DENIED",  # 会话级权限不足（无权限访问该会话/资源）
        )
    )


def is_org_config_problem(error_msg: str) -> bool:
    """判断是否为「组织未配置 CLI 权限」问题。

    这类错误不是 token 过期或登录失效，而是钉钉开放平台上
    组织管理员还没有给 DingTalk-Workspace 应用开启数据访问权限。
    重复登录无法解决，必须由管理员在开放平台操作。
    """
    return any(
        keyword in error_msg
        for keyword in (
            "该组织尚未开启 CLI 数据访问权限",
            "AGENT_CODE_NOT_EXISTS",
        )
    )


def classify_dws_error(error_msg: str) -> type[DwsError]:
    """根据错误消息分类是否可重试。

    Returns:
        DwsRetryableError: 网络超时、连接拒绝、临时服务器错误
        DwsPermissionError: 权限不足
        DwsNonRetryableError: 认证失败、参数错误
        DwsError: 未知错误（默认不可重试）
    """
    if is_permission_error(error_msg):
        return DwsPermissionError

    error_lower = error_msg.lower()

    # 可重试错误模式
    retryable_patterns = [
        "timeout", "timed out", "connection refused",
        "network unreachable", "temporary failure",
        "503", "504", "service unavailable", "gateway timeout",
    ]

    # 不可重试错误模式
    non_retryable_patterns = [
        "authentication failed", "unauthorized", "401", "403",
        "invalid token", "expired", "permission denied",
        "not found", "404", "invalid parameter", "bad request", "400",
        "account locked", "rate limit exceeded", "429",
    ]

    for pattern in retryable_patterns:
        if pattern in error_lower:
            return DwsRetryableError

    for pattern in non_retryable_patterns:
        if pattern in error_lower:
            return DwsNonRetryableError

    return DwsError  # 默认不可重试
