"""跨平台初始化的优雅降级封装。

设计：单个平台（含 dingtalk 主平台与 feishu/wecom 等辅助平台）的运行期组件
构造可能因 CLI 缺失、DB 异常、网络探测等失败。为遵循「单平台失败不拖垮整个
bot」原则，所有平台初始化统一经本模块封装：失败仅记录 [resilience] 日志并跳过
该平台，绝不向上抛异常中止启动。

日志统一使用 [resilience] 前缀，便于监控侧聚合（与 schema/poller 既有约定一致）。
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def init_platform_safe(
    platform_id: str,
    display_name: str,
    build: Callable[[], object],
    register: Callable[[object], None] | None = None,
) -> bool:
    """容错初始化单个平台。

    Args:
        platform_id: 平台 id（如 dingtalk/feishu）。
        display_name: 展示名（用于日志）。
        build: 构造平台运行期组件（返回任意上下文对象）的可调用；抛异常即视为
            该平台初始化失败。
        register: 可选，build 成功后执行注册/接线（如写入 self.platforms、
            注册 tracker store、创建备份实例）。若 register 抛异常同样视为失败。

    Returns:
        True 表示初始化成功并完成注册；False 表示该平台被跳过（已记录 [resilience]）。
    """
    try:
        ctx = build()
        if register is not None:
            register(ctx)
        logger.info("[resilience] 平台 %s(%s) 初始化完成", platform_id, display_name)
        return True
    except Exception as e:  # noqa: BLE001 - 单平台失败必须被隔离
        logger.error(
            "[resilience] 平台 %s(%s) 初始化失败（已跳过，不影响其他平台）: %s",
            platform_id, display_name, e,
        )
        return False
