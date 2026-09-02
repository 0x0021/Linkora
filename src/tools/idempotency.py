"""副作用工具幂等护栏。

解决 at-least-once 消息处理在 handler 崩溃（未标记「已处理」）场景下，
同一条消息被重新处理、导致副作用型工具（发消息 / 发 DING / 建待办 / 存记忆）
重复施加的问题。

机制：对显式声明的副作用工具，以 (tool_name, session_key, 规范化参数) 为键，
在短时窗口内记录「首次成功执行」。窗口内第二次相同调用直接返回首次结果，
不再真正执行，从而把对外副作用收敛为 exactly-once。

设计约束（满足 P0 安全红线，不破坏消息处理）：
- 仅在**首次执行成功**后记录，失败不记录 → 失败可正常重试（at-least-once 失败语义保留）。
- 仅覆盖「无确认门控、立即执行」的副作用工具；需二次确认的工具天然不会被重放双发。
- 最坏情况（如哈希碰撞或 TTL 过期）只是没拦住，不会把成功误判为失败、也不会丢消息。
- 线程安全：ToolRouter 被主轮询 / 后台任务 / Web 多线程共享，所有访问走锁。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

# 需幂等护栏的副作用型工具（产生对外可见 / 持久化副作用，且无确认门控）。
# 注意：transfer_approval 等 require_confirm=True 的工具不在此列——其首次调用仅返回
# confirm_required（不真正执行），重放天然安全。
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset(
    {
        "send_message",  # 发送消息（钉钉/飞书/企微）
        "send_ding",  # 发送 DING 强提醒（app 类型免确认、立即执行）
        "create_todo",  # 创建待办
        "save_memory",  # 写入长期记忆
    }
)

# 重放窗口（秒）：覆盖一次崩溃后的重试间隔（秒~分钟级），又不长期拦住合法重复发送。
DEFAULT_TTL_SECONDS = 600


def _stable_key(tool_name: str, session_key: str | None, args: dict) -> str:
    """生成幂等键：排除会变化的 confirm_token，保留稳定业务参数。"""
    stable = {k: v for k, v in (args or {}).items() if k != "confirm_token"}
    payload = json.dumps(
        [tool_name, session_key or "", stable],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SideEffectIdempotencyGuard:
    """副作用工具的重放去重护栏（进程内、带 TTL）。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, tool_name: str, session_key: str | None, args: dict) -> tuple[bool, Any]:
        """查询是否为窗口内重放。命中返回 (True, 首次成功结果)；否则 (False, None)。"""
        key = _stable_key(tool_name, session_key, args)
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            entry = self._store.get(key)
            if entry is not None and now - entry[0] <= self.ttl:
                return True, entry[1]
            return False, None

    def record_success(
        self, tool_name: str, session_key: str | None, args: dict, result: Any
    ) -> None:
        """首次成功执行后回填结果，供重放时返回一致内容。"""
        key = _stable_key(tool_name, session_key, args)
        with self._lock:
            self._store[key] = (time.time(), result)

    def _purge_expired(self, now: float) -> None:
        expired = [k for k, (ts, _t) in self._store.items() if now - ts > self.ttl]
        for k in expired:
            self._store.pop(k, None)
