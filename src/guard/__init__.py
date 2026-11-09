"""入站消息提示词注入防护（IM 消息链路，非 Web 表单）。

只做四件事：结构化风险识别、载荷清洗、不可信边界隔离、事件记录。
Web 管理台通过 ``web/routers/guard.py`` 展示事件与统计。

⚠️ 包初始化保持零副作用：``src/config_models/system.py`` 会在**模块导入期**
   import ``src.guard.config``，此处若 import service/events 会引入
   ``shared_state`` 与文件 IO，甚至形成循环依赖。调用方请显式导入子模块。
"""
from __future__ import annotations

from src.guard.config import GuardConfig

__all__ = ["GuardConfig"]
