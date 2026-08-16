"""全局共享常量（跨模块单一真源）。

集中存放需要在多个层（src / web / config 模型）间共享、且历史上曾出现「多份副本
各自漂移」的常量，例如平台白名单。任何新增常量请确认是否应归口此处，避免再次
散落为三处互不感知的副本。
"""
from __future__ import annotations

from typing import Final

# 系统支持的全部 IM 平台。
# 与 src/config_models.py 中 ``PlatformConfig.adapter_type:
# Literal["dingtalk", "feishu", "wecom"]`` 保持一致；新增平台时两处必须同步更新，
# 否则会破坏「新增平台一处漏改」一致性断言（tests/test_platform_whitelist.py）。
SUPPORTED_PLATFORMS: Final[frozenset[str]] = frozenset({"dingtalk", "feishu", "wecom"})
