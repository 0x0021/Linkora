"""Linkora 组件共享状态基类（类型治理 F9）。

所有 mixin 家族（poller / engine / memory / dws / im）的共同祖先，集中声明跨 mixin
交叉访问的「共享实例状态」（config / store / dws / current_user_* / platform_id 等）。
组合类在 __init__ 中赋值；此处仅做类型声明，供 pyright 静态解析交叉成员访问。

设计要点（零运行时风险）：
- 所有注解为惰性字符串（from __future__ import annotations）+ TYPE_CHECKING 导入，
  不触发运行时 import，避免循环依赖。
- 本类无 __init__、无方法实现，仅声明状态；作为各 mixin 的最终祖先，组合类运行时的
  真实赋值在子类 MRO 中先于本类，不会遮蔽任何行为。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.im_adapter.base_adapter import BaseIMAdapter
    from src.memory.sqlite_store import SQLiteStore


class LinkoraComponentBase:
    # === 跨家族共享状态（组合类 __init__ 赋值） ===
    # 注：config 不在共享基类声明——poller 家族为 PollerConfig、engine 家族为 AppConfig，
    # 由各家族基类（PollerMixinBase / EngineMixinBase）分别声明，避免类型冲突。
    store: SQLiteStore
    # dws 声明为 BaseIMAdapter 而非具体的 DwsAdapter：钉钉/飞书/企微三种适配器
    # 都只实现 BaseIMAdapter 这一层契约，poller / engine 也只用这层 API。
    # 之前钉死成 DwsAdapter，导致 primary._build_platform_ctx 里把飞书/企微
    # 适配器传进 MessagePoller / PlatformContext 时全线报类型不兼容（62 处）。
    dws: BaseIMAdapter
    # 三个 current_user_* 在所有赋值点都保证为 str（未知时为空串 ""，
    # 见 primary._init_user / poller.__init__ / sync_history）。
    # 早先声明成 `str | None` 纯属保守，反而让每个下游消费点（MessagePoller
    # 等要求 str 的构造参数）都报 reportArgumentType。
    current_user_id: str
    current_user_name: str
    current_user_user_id: str
    platform_id: str
