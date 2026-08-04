"""IM CLI 适配器抽象层。

把钉钉 ``dws`` CLI 的执行引擎抽离为可继承的 ``BaseIMAdapter``，
飞书 / 企业微信等平台通过继承 + 覆写少量钩子即可接入同一套执行 / 重试 / 错误体系。

公开接口::

    from src.im_adapter import (
        BaseIMAdapter,            # 执行引擎基类 + 23 个能力方法统一接口（拼命令 / subprocess / 重试 / 错误分类）
        FeishuCliAdapter,         # 飞书适配器
        WecomCliAdapter,          # 企业微信适配器
        IMAdapterError,           # 统一异常体系
        IMAdapterRetryableError,
        IMAdapterNonRetryableError,
        IMAdapterPermissionError,
    )
"""
from __future__ import annotations

from .base_adapter import BaseIMAdapter
from .errors import (
    IMAdapterError,
    IMAdapterNonRetryableError,
    IMAdapterPermissionError,
    IMAdapterRetryableError,
)
from .feishu import FeishuCliAdapter
from .wecom import WecomCliAdapter

__all__ = [
    "BaseIMAdapter",
    "FeishuCliAdapter",
    "WecomCliAdapter",
    "IMAdapterError",
    "IMAdapterRetryableError",
    "IMAdapterNonRetryableError",
    "IMAdapterPermissionError",
]
