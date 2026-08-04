"""平台核心类组合单测。

覆盖：LinkoraEngine 组合来自 5 个 mixin 的方法均可用，MRO 解析。
"""

from __future__ import annotations

import pytest

from src.platform.core import LinkoraEngine
from src.platform.primary import PrimaryMixin
from src.platform.runtime import RuntimeMixin
from src.platform.message_loop import MessageLoopMixin
from src.platform.memory import MemoryMixin
from src.platform.lifecycle import LifecycleMixin


class TestLinkoraEngineMRO:
    """验证 LinkoraEngine 的 MRO 和各 mixin 方法可达性。"""

    def test_mixin_inheritance(self):
        mro_names = [c.__name__ for c in LinkoraEngine.__mro__]
        for mixin in [
            "PrimaryMixin", "RuntimeMixin", "MessageLoopMixin",
            "MemoryMixin", "LifecycleMixin",
        ]:
            assert mixin in mro_names, f"{mixin} 不在 LinkoraEngine MRO 中"


class TestCrossMixinMethodPresence:
    """验证关键跨 mixin 方法在 LinkoraEngine 上可访问。"""

    def test_lifecycle_methods(self):
        assert hasattr(LinkoraEngine, "shutdown")
        assert hasattr(LinkoraEngine, "run")

    def test_message_loop_methods(self):
        assert hasattr(LinkoraEngine, "_is_incomplete_message")
        assert hasattr(LinkoraEngine, "_batch_has_structured_data")
        assert hasattr(LinkoraEngine, "_batch_has_request")
        assert hasattr(LinkoraEngine, "_compute_debounce_delay")

    def test_runtime_methods(self):
        assert hasattr(LinkoraEngine, "_send_reply")
