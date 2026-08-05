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
    from src.dws_adapter import DwsAdapter
    from src.memory.sqlite_store import SQLiteStore


class LinkoraComponentBase:
    # === 跨家族共享状态（组合类 __init__ 赋值） ===
    # 注：config 不在共享基类声明——poller 家族为 PollerConfig、engine 家族为 AppConfig，
    # 由各家族基类（PollerMixinBase / EngineMixinBase）分别声明，避免类型冲突。
    store: SQLiteStore
    dws: DwsAdapter
    current_user_id: str | None
    current_user_name: str | None
    current_user_user_id: str | None
    platform_id: str
