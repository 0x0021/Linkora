"""Linkora 统一异常体系。

所有业务异常均继承自 LinkoraError，便于：
1. 在入口处统一 catch 并转换为用户友好的 HTTP 错误
2. 在日志中按层级过滤（只记录业务异常，忽略系统异常）
3. 在监控指标中按异常类型统计
"""
from __future__ import annotations


class LinkoraError(Exception):
    """所有 Linkora 业务异常的基类。"""

    def __init__(self, message: str = "", code: str | None = None, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.__class__.__name__
        self.context = context

    def __str__(self) -> str:
        if self.context:
            return f"{self.message} ({self.code}: {self.context})"
        return self.message


# ── 数据库层 ──────────────────────────────────────────────────────────


class DBError(LinkoraError):
    """数据库操作失败的基类。"""


class DBConnectionError(DBError):
    """数据库连接失败（连接池耗尽、文件损坏等）。"""


class DBWriteError(DBError):
    """数据库写入失败（约束冲突、事务失败等）。"""


class DBBusyError(DBError):
    """数据库忙（SQLite Busy Timeout）。"""


class DBSchemaError(DBError):
    """数据库 schema 不匹配（缺少列、表不存在等）。"""


# ── LLM 层 ────────────────────────────────────────────────────────────


class LLMError(LinkoraError):
    """LLM 调用失败的基类。"""


class LLMNetworkError(LLMError):
    """网络相关 LLM 错误（连接失败、超时等）。"""


class LLMRateLimitError(LLMError):
    """LLM 服务限流（429 / Rate Limit Exceeded）。"""


class LLMAuthError(LLMError):
    """LLM 认证失败（API Key 无效、过期等）。"""


class LLMContentError(LLMError):
    """LLM 内容安全拦截（敏感词、违规内容等）。"""


class LLMTimeoutError(LLMNetworkError):
    """LLM 调用超时。"""


# ── IM 适配器层 ───────────────────────────────────────────────────────


class IMAdapterError(LinkoraError):
    """IM 适配器错误的基类。"""


class IMAdapterPermissionError(IMAdapterError):
    """IM 权限不足（token 失效、账号被禁等）。"""


class IMAdapterRateLimitError(IMAdapterError):
    """IM 平台限流。"""


class IMAdapterTimeoutError(IMAdapterError):
    """IM 调用超时。"""


class IMAdapterNotFoundError(IMAdapterError):
    """IM 资源不存在（用户、群组、文档等）。"""


class IMAdapterNotSupportedError(IMAdapterError):
    """IM 功能不支持（如企微富媒体降级）。"""


# ── 工具层 ────────────────────────────────────────────────────────────


class ToolError(LinkoraError):
    """工具执行失败的基类。"""


class ToolValidationError(ToolError):
    """工具参数校验失败。"""


class ToolExecutionError(ToolError):
    """工具执行过程中出错。"""


class ToolPermissionError(ToolError):
    """工具权限不足（未授权访问敏感数据等）。"""


# ── 配置层 ────────────────────────────────────────────────────────────


class ConfigError(LinkoraError):
    """配置相关错误。"""


class ConfigMissingError(ConfigError):
    """必需配置项缺失。"""


class ConfigValidationError(ConfigError):
    """配置值校验失败。"""


# ── 消息处理层 ────────────────────────────────────────────────────────


class MessageError(LinkoraError):
    """消息处理错误的基类。"""


class MessageParseError(MessageError):
    """消息解析失败。"""


class MessageDuplicationError(MessageError):
    """消息去重检测为重复。"""


class MessageRateLimitError(MessageError):
    """消息发送受限。"""


# ── 路由层 ────────────────────────────────────────────────────────────


class RoutingError(LinkoraError):
    """意图路由/工具路由错误。"""


class IntentMatchError(RoutingError):
    """意图匹配失败或歧义。"""


class ToolNotFound(RoutingError):
    """请求的工具不存在。"""
