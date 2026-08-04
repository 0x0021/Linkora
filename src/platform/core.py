from __future__ import annotations

from .primary import PrimaryMixin
from .runtime import RuntimeMixin
from .message_loop import MessageLoopMixin
from .memory import MemoryMixin
from .lifecycle import LifecycleMixin


class LinkoraEngine(
    PrimaryMixin,
    RuntimeMixin,
    MessageLoopMixin,
    MemoryMixin,
    LifecycleMixin,
):
    """多平台 AI 自动回复主引擎。

    由 src/platform/ 下 5 个 mixin 组合而成（仅物理拆分，行为等同原 main.LinkoraEngine）：
    - PrimaryMixin     初始化链（__init__ + _init_*）
    - RuntimeMixin     平台上下文/属性、配置热重载、回复与发送、风格画像、草稿、引用、限流、死信
    - MessageLoopMixin 消息主循环、debounce、backpressure、轮询状态
    - MemoryMixin      自动记忆保存与各类清理调度器
    - LifecycleMixin   shutdown / run / dev watcher（模块级函数）
    """
