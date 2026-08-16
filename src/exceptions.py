"""Linkora 统一异常体系（根类 + 各族族根）。

所有业务异常均继承自 ``LinkoraError``，便于：
1. 在入口处统一 catch 并转换为用户友好的 HTTP 错误
2. 在日志中按层级过滤（只记录业务异常，忽略系统异常）
3. 在监控指标中按异常类型统计

**归属规则（ownership）**——本模块只持有根类 ``LinkoraError`` 与各族「族根」，
具体子类由各族 owner 模块单一持有，本模块**不得**另立同名具体类：

- 数据库层 ``DB*``            → 本模块（owner 即本模块）
- LLM 层 ``LLM*``             → ``src/llm/exceptions.py``
- IM 适配器层 ``IMAdapter*``   → ``src/im_adapter/errors.py``（本模块仅留族根 ``IMAdapterError``）
- 工具层 ``Tool*``            → 本模块
- 配置层 ``Config*``          → 本模块
- 消息层 ``Message*``         → 本模块
- 路由层 ``Routing*``         → 本模块

历史教训：曾有一套与 ``src/im_adapter/errors.py`` 同名的 ``IMAdapter*`` 具体子类并列
存在于本模块，导致 ``from src.exceptions import IMAdapterTimeoutError`` 拿到一个
**永不 raise** 的类，对应 ``except`` 子句在求值时即抛 ``NameError``、并击穿整个 try
（参见 ``src/im_adapter/wecom.py`` 的生产事故）。具体异常唯一 owner 化后，杜绝此类复发。
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
    """IM 适配器错误的基类（IM 族根）。

    本模块只持有各族「族根」与根类 ``LinkoraError``；IM 族的具体子类（权限/限频/
    超时/资源不存在/不支持等）由 ``src/im_adapter/errors.py`` 单一 owner 持有。

    归属规则（防「同名不同族」事故）：
    - DB/LLM/Tool/Config/Message/Routing 各族的具体子类由各族 owner 模块定义；
    - IM 族具体子类 → ``src/im_adapter/errors.py``；
    - LLM 族具体子类 → ``src/llm/exceptions.py``；
    新增具体异常**不得**在本模块另立同名类，否则会与 owner 模块的同名类分居两处，
    导致从错误模块导入到永不 raise 的类、except 子句永久静默失效
    （参见 ``src/im_adapter/wecom.py`` 的历史生产事故）。
    """


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
