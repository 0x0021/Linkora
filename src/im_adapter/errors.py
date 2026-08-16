"""统一的 IM 适配器异常体系。

所有平台适配器（钉钉 dws / 飞书 / 企业微信）都应抛出这套通用异常，
业务层（poller、tools、摘要调度）只捕获 ``IMAdapter*`` 即可，无需感知具体平台。

各平台适配器内部把自家 CLI 错误文本映射为这些类，见 ``BaseIMAdapter._classify_error`` 钩子。

层级::

    IMAdapterError                     (基类)
      ├─ IMAdapterRetryableError       (网络超时/限频等，基类 run() 会自动退避重试)
      └─ IMAdapterNonRetryableError    (认证失败/参数错误/资源不存在，立即抛出不重试)
            └─ IMAdapterPermissionError (token 过期/无会话权限/组织未授权)

继承说明（2026-08-15 收敛）：
本模块的 ``IMAdapterError`` 现继承 ``src.exceptions.IMAdapterError``（即 ``LinkoraError`` 的
IM 族根），使 IM 适配器异常正式并入统一异常体系，入口可统一 catch、日志可分层过滤。
为避免与 ``src.exceptions`` 中的同名族根互相遮蔽，这里用别名 ``_LinkoraIMAdapterError`` 导入
族根，再让本模块的 ``IMAdapterError`` 继承之。7 个具体子类（Retryable/NonRetryable 及其
下游）的继承关系保持不变。
"""
from __future__ import annotations

from src.exceptions import IMAdapterError as _LinkoraIMAdapterError


class IMAdapterError(_LinkoraIMAdapterError):
    """IM 适配器基础异常：所有平台 CLI 错误的基类。"""
    pass


class IMAdapterRetryableError(IMAdapterError):
    """可重试错误：网络超时、临时故障、限频等。

    基类 ``run()`` 捕获后会按指数退避自动重试。
    """
    pass


class IMAdapterNonRetryableError(IMAdapterError):
    """不可重试错误：认证失败、参数错误、资源不存在等，立即抛出，不重试。"""
    pass


class IMAdapterPermissionError(IMAdapterNonRetryableError):
    """权限 / 认证失效错误：token 过期、无会话权限、组织未授权等，立即抛出，不重试。"""
    pass


class IMAdapterResourceNotFoundError(IMAdapterNonRetryableError):
    """资源不存在：文档 / 会话 / 用户 ID 无效或已被删除，立即抛出，不重试。"""
    pass


class IMAdapterRateLimitError(IMAdapterRetryableError):
    """限频错误：触发平台流控，基类 run() 会自动退避重试。"""
    pass


class IMAdapterUnsupportedTypeError(IMAdapterNonRetryableError):
    """文档 / 资源类型不被 CLI 支持（如 lark-cli docs +fetch 仅支持 docx，
    不支持 file/wiki 等类型）。立即抛出，不重试；调用方应给出友好提示。"""
    pass


class IMAdapterShutdownError(IMAdapterNonRetryableError):
    """子进程被信号终止（如 Ctrl+C 时整个进程组收到 SIGINT 杀掉 CLI，
    subprocess.returncode 为负数）。这通常发生在父进程退出阶段，属正常关机
    而非真实故障——基类 run() 会降级为 debug 日志，且不触发重试。"""
    pass
