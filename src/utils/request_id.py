"""轻量级 request_id 上下文管理。

每条入站消息/每次后台任务/每次 Web 请求都会生成一个唯一的 request_id，
贯穿 poller → llm_agent → LLM 调用 → tool 调用 → store 落库 → 日志，
便于出问题时 grep 一个 id 看全链路。

设计要点：
- 用 contextvars 而非 threading.local：async/线程池切线程时仍能传递
- 同一线程上同步代码天然可见；跨线程（后台任务、Web worker）需显式 token 透传
- 提供 @with_request_id 装饰器把外部传入的 id 透到内部函数
- 不依赖任何 web 框架，避免污染核心模块

状态管理：使用 AtomicCounter 类封装计数器，避免 global 关键字。
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from contextvars import ContextVar, Token
from typing import Optional

_current_request_id: ContextVar[str] = ContextVar("request_id", default="")
# 同一请求的创建时间（毫秒），便于日志/前端按时间窗口过滤
_current_request_start_ms: ContextVar[int] = ContextVar("request_start_ms", default=0)
# 贯穿全链路的 trace_id：同一会话/多轮对话下保持稳定，便于把一次对话的多条
# request_id 关联起来（Web→Runtime→LLM/DWS）。缺省等于当前 request_id（按消息粒度追踪），
# 调用方可显式 set_trace_id 传入跨消息的会话级 id。
_current_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


class AtomicCounter:
    """线程安全的原子计数器（替代 global _id_counter）。"""

    def __init__(self, initial: int = 0, modulus: int = 65536) -> None:
        self._value = initial
        self._modulus = modulus
        self._lock = threading.Lock()

    def increment(self) -> int:
        with self._lock:
            self._value = (self._value + 1) % self._modulus
            return self._value


_id_counter = AtomicCounter()

logger = logging.getLogger(__name__)


def generate_request_id(prefix: str = "r") -> str:
    """生成一个唯一的 request_id。

    格式: {prefix}{8位时间戳(36进制)}{4位随机}{2位计数} —— 可读 + 短。
    例: r01h4d3x2-a3f2-1f    (24 字符内)
    """
    ts = time.time()
    # ts_part: 8 chars 36-encoded（覆盖到秒级，约 30 年不撞）
    ts_part = format(int(ts * 1000) % (36 ** 8), "08x")
    rand_part = secrets.token_hex(2)
    cnt_part = format(_id_counter.increment(), "04x")
    return f"{prefix}{ts_part}{rand_part}{cnt_part}"


def get_request_id() -> str:
    """获取当前 ContextVar 中的 request_id（无则返回空串）。"""
    return _current_request_id.get()


def get_request_start_ms() -> int:
    """获取当前请求的开始时间（毫秒），无则返回 0。"""
    return _current_request_start_ms.get()


def set_request_id(rid: str = "", *, prefix: str = "r") -> str:
    """设置当前 ContextVar 的 request_id。

    - 传入空串时自动生成新 id
    - 返回实际生效的 id（供调用方记录）
    - 返回的 Token 用于 reset（成对调用 set/reset 防止泄漏到下个请求）
    """
    rid = rid or generate_request_id(prefix=prefix)
    _current_request_id.set(rid)
    _current_request_start_ms.set(int(time.time() * 1000))
    return rid


def _safe_reset(var: ContextVar, token: Token, label: str) -> None:
    """安全重置 ContextVar。

    跨线程使用 token 会抛 ValueError，静默吞掉会让 request_id/trace_id 串号
    （最难查的一类 bug），故失败仅记 debug 并带 token 与当前线程名留痕。
    """
    try:
        var.reset(token)
    except Exception as _e:  # noqa: BLE001
        logger.debug(
            "[rid] reset %s failed (token=%r, thread=%s): %s",
            label, token, threading.current_thread().name, _e,
        )


def reset_request_id(token: Token) -> None:
    """恢复 ContextVar 状态，配合 set_request_id 成对使用。"""
    _safe_reset(_current_request_id, token, "request_id")
    _safe_reset(_current_request_start_ms, token, "request_start_ms")


def get_trace_id() -> str:
    """获取当前 ContextVar 中的 trace_id（无则返回空串）。"""
    return _current_trace_id.get()


def set_trace_id(tid: str = "", *, prefix: str = "t") -> str:
    """设置当前 ContextVar 的 trace_id。

    - 传入空串时自动生成新 id（默认前缀 t）
    - 返回实际生效的 id（供调用方记录）
    """
    tid = tid or generate_request_id(prefix=prefix)
    _current_trace_id.set(tid)
    return tid


def reset_trace_id(token: Token) -> None:
    """恢复 ContextVar 状态，配合 set_trace_id 成对使用。"""
    _safe_reset(_current_trace_id, token, "trace_id")


class request_id_scope:
    """Context manager / decorator：在作用域内自动生成并设置 request_id。

    用法1 - with：
        with request_id_scope() as rid:
            ...  # 内部所有日志自动带 rid
            logger.info("hello")

    用法2 - 装饰器：
        @request_id_scope()
        def handle_message(msg):
            ...

    用法3 - 传入外部 id（重放/Web 请求用调用方 id）：
        with request_id_scope(rid="web-abc123"):
            ...
    """

    def __init__(self, rid: str = "", *, prefix: str = "r", trace_id: str = ""):
        self._rid = rid
        self._prefix = prefix
        self._trace_id = trace_id
        self._token_id: Optional[Token] = None
        self._token_ts: Optional[Token] = None
        self._token_tid: Optional[Token] = None
        self.rid: str = ""
        self.trace_id: str = ""

    def __enter__(self) -> str:
        self.rid = self._rid or generate_request_id(prefix=self._prefix)
        self._token_id = _current_request_id.set(self.rid)
        self._token_ts = _current_request_start_ms.set(int(time.time() * 1000))
        # trace_id 缺省等于本作用域的 request_id（按消息粒度追踪）；
        # 调用方传入 trace_id 时用于跨消息的会话级关联。
        self.trace_id = self._trace_id or self.rid
        self._token_tid = _current_trace_id.set(self.trace_id)
        logger.debug("[rid] 进入作用域: %s (trace=%s)", self.rid, self.trace_id)
        return self.rid

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token_id is not None:
            _safe_reset(_current_request_id, self._token_id, "request_id")
        if self._token_ts is not None:
            _safe_reset(_current_request_start_ms, self._token_ts, "request_start_ms")
        if self._token_tid is not None:
            _safe_reset(_current_trace_id, self._token_tid, "trace_id")
        logger.debug("[rid] 退出作用域: %s (err=%s)", self.rid, exc_type.__name__ if exc_type else None)

    def __call__(self, fn):
        """装饰器形态。"""
        def wrapper(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper


def install_log_filter() -> None:
    """给 root logger 安装一个 Filter，自动把 request_id 注入每条日志记录。

    这样所有现有 logger.info("xxx") 自动带 rid，无需改调用点。
    重复安装是幂等的（只装一次）。
    """
    root = logging.getLogger()
    if getattr(root, "_rid_filter_installed", False):
        return

    class _RidFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            rid = get_request_id()
            # 始终设置 record.request_id（保证 logging.Formatter 用 %(request_id)s 时不 KeyError）
            record.request_id = rid if rid else "-"
            tid = get_trace_id()
            # 同理注入 trace_id（F29：全链路关联字段）
            record.trace_id = tid if tid else "-"
            return True

    f = _RidFilter()
    root.addFilter(f)
    # 给所有已注册的 handler 也装一遍（filter 是按 logger 层级生效的，
    # 但 root 上挂的 filter 会被子 logger 继承；这里保险起见都加）
    for h in root.handlers:
        h.addFilter(f)
    root._rid_filter_installed = True
    logger.info("[rid] 日志过滤器已安装")


def format_rid_for_log() -> str:
    """返回当前 rid 的简短展示，供日志 format string 使用。"""
    rid = get_request_id()
    return rid[:12] if rid else "-"
