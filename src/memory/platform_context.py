"""当前平台上下文——**全局唯一真源**。

用于在「不改动 60+ 调用方签名」的前提下，让仓储层在 platform 缺省时仍能取到
当前处理中的平台，从而正确路由到该平台的 per-account 会话库。

使用方式：
  - 在平台处理的边界（runtime 的 platform 回调、poller 每平台循环、agent 消息入口）
    调用 ``set_current_platform(platform_id)`` 拿到 token，退出时 ``reset_current_platform(token)``。
  - 需要「一次性把整条链路的平台上下文全部对齐」时，用 :func:`platform_scope`。
  - 仓储方法 platform 缺省时，``get_current_platform()`` 提供回退值。

contextvar 是 per-context（协程/线程）隔离的，并发处理多平台时互不串扰。

────────────────────────────────────────────────────────────────────────
平台上下文变量全景（本模块是唯一同步点，请勿再在业务代码里手工逐个 set）
────────────────────────────────────────────────────────────────────────
历史上「当前平台」散落在 4 处、需要调用方手工同步，漏设任意一处就会出现
串图 / 空 platform / 日志归属错。现已收敛为 **3 个 ContextVar + 1 个统一入口**：

1. ``current_platform_var``（本模块，default ``""``）
   —— 唯一真源。服务于仓储层 ``conv_conn(get_current_platform())`` 的会话库路由。
      Web 层（``web/dependencies.py``）原先自建的「第二套」平台 ContextVar 已删除，
      直接复用本变量，Web 侧仅在读取时套一层 ``or "dingtalk"`` 的向后兼容回退。

2. ``src.platform.base._active_platform_ctx``（default ``"dingtalk"``）
   —— 服务于 runtime 的组件路由（``self.store``/``dws``/``poller``/``llm_agent``
      四个属性按它解析到对应 ``PlatformContext``）。
      **不能与 (1) 物理合并**：其 default 为非空的 ``"dingtalk"``（后台线程必须回退主
      平台），而 (1) 的 default 必须为空串（空 = 未知，由 conv_conn 记 warning）。
      两者默认值语义相反，合并会让 ``runtime.py`` 中形如
      ``_active_platform_ctx.get() or message.platform_id`` 的死分支复活。

3. ``src.utils.logger._log_platform_ctx``（default ``None``）
   —— 服务于日志归属（Web 日志视图按平台过滤）。
      **不能与 (1)(2) 物理合并**：它必须能区分「显式设置」与「未设置」，否则
      Web / 调度器等非消息链路的中性日志会被误标成钉钉。

三者的 set/reset 时序由 :func:`platform_scope` 统一封装——业务代码只调它一处，
不再需要手工同步；:func:`get_current_platform` 额外提供 (1)→(2) 的跨 var 兜底。
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# 默认空串：调用方未设置上下文时回退；conv_conn 会对空 platform 记 warning 以便排查。
current_platform_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "linkora_current_platform", default=""
)


def get_current_platform() -> str:
    """读取当前平台上下文，多源回退，缺省返回空串（conv_conn 会再记 warning）。

    回退顺序（先匹配者胜出）：

    1. ``current_platform_var``（src 端主用）—— 由 poller / message_loop 显式 set；
    2. ``_active_platform_ctx``（platform.runtime 内部用）—— runtime 派发消息时 set，
       但 message_repo 不读这个 var，所以这里反向兜底，把 runtime 的设置也路由进来，
       避免「runtime 设了 platform，但仓储因读不到而走空命名空间」的串图问题；
    3. 都未设置 → 返回空串（由 conv_conn 兜底为 :unknown 命名空间并打 warning）。

    子线程（ThreadPoolExecutor / threading.Timer）默认不继承父线程的 ContextVar，
    本函数作为「跨 var 兜底」是修复串图问题的关键防线。
    """
    v = current_platform_var.get()
    if v:
        return v
    # 兜底 2：runtime 的 _active_platform_ctx（base.py 内 default="dingtalk"，天然非空）
    try:
        # lazy import 防止 src/memory ↔ src/platform 之间出现循环导入
        from src.platform.base import _active_platform_ctx as _rt_ctx
        v2 = _rt_ctx.get()
        if v2:
            return v2
    except Exception:
        logger.debug("copy_platform_context 读取运行时上下文失败，返回空串")
    return v  # 空串


def set_current_platform(platform_id: str) -> contextvars.Token:
    return current_platform_var.set(platform_id)


def reset_current_platform(token: contextvars.Token) -> None:
    current_platform_var.reset(token)


def with_platform(platform_id: str):
    """上下文管理器：进入时设置当前平台，退出时复位。

    **只设置仓储路由这一个 var**，不触碰 runtime 组件路由与日志归属——适用于后台
    任务（摘要/记忆提取/历史回填）这类「只需要写对库、但不应把中性日志强行归属到
    某平台」的场景。需要整条消息链路对齐时请改用 :func:`platform_scope`。

    用法::

        with with_platform("feishu"):
            ...  # 此区间内仓储调用自动路由到 feishu 的会话库
    """

    class _Ctx:
        def __enter__(self) -> "_Ctx":
            self._tok = set_current_platform(platform_id)
            return self

        def __exit__(self, *exc: Optional[object]) -> bool:
            reset_current_platform(self._tok)
            return False

    return _Ctx()


# ── 跨模块平台 ContextVar 的统一同步点 ────────────────────────────────────
# lazy import：src/memory 与 src/platform、src/utils 之间存在双向依赖，模块级
# import 会成环，故在函数内取变量对象（拿到后是同一个单例，无重复开销）。


def _runtime_platform_var() -> "contextvars.ContextVar[str] | None":
    """返回 runtime 的 ``_active_platform_ctx``；在不含 src.platform 的精简环境返回 None。"""
    try:
        from src.platform.base import _active_platform_ctx

        return _active_platform_ctx
    except ImportError:
        return None


def _log_platform_var() -> "contextvars.ContextVar[str | None] | None":
    """返回日志归属的 ``_log_platform_ctx``；不可用时返回 None。"""
    try:
        from src.utils.logger import _log_platform_ctx

        return _log_platform_ctx
    except ImportError:
        return None


@contextlib.contextmanager
def platform_scope(platform_id: str, *, log: bool = True) -> Iterator[str]:
    """在作用域内把「当前平台」的**全部**上下文变量一次性对齐，退出时全部复位。

    这是消息链路边界（平台派发回调 / 防抖 flush Timer / 死信重放）应当使用的
    唯一入口，取代过去在每个边界手工 set 三个 ContextVar 的脆弱写法——漏设任意
    一个都会导致串图（用错平台的 store/dws）、空 platform（落幽灵库）或日志归属错。

    设置的变量：

    - ``current_platform_var``——仓储会话库路由（本模块，唯一真源）；
    - ``_active_platform_ctx``——runtime 组件路由（store/dws/poller/llm_agent）；
    - ``_log_platform_ctx``——日志归属（``log=False`` 可跳过，用于不希望把中性日志
      强行归属到某平台的场景）。

    子线程（``threading.Timer`` / ``ThreadPoolExecutor``）默认不继承父线程的
    ContextVar，在子线程入口处进入本作用域即可完整还原平台上下文。

    用法::

        with platform_scope("feishu"):
            ...  # 此区间内仓储、runtime 组件、日志归属全部指向 feishu
    """
    restore: list[tuple[contextvars.ContextVar, contextvars.Token]] = []

    rt_var = _runtime_platform_var()
    if rt_var is not None:
        restore.append((rt_var, rt_var.set(platform_id)))

    if log:
        log_var = _log_platform_var()
        if log_var is not None:
            restore.append((log_var, log_var.set(platform_id)))

    restore.append((current_platform_var, current_platform_var.set(platform_id)))

    try:
        yield platform_id
    finally:
        # 逆序复位：与进入顺序对称，避免 Token 与 var 错配（ContextVar 之间互相独立，
        # 但保持对称能让嵌套 scope 的行为最符合直觉）。
        for var, token in reversed(restore):
            var.reset(token)


def copy_platform_context() -> contextvars.Context:
    """快照当前上下文，供跨线程投递时还原平台上下文。

    ``ThreadPoolExecutor`` / ``threading.Timer`` 的 worker 线程默认**不继承**父线程的
    ContextVar，直接提交的任务会读到空 platform 并落到幽灵命名空间。用法::

        executor.submit(copy_platform_context().run, task)
    """
    return contextvars.copy_context()
