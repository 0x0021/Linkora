"""内置工具自动发现与注册（P2-12）。

设计目标：把 main.py 里 35+ 处手写 `self.tool_router.register(XxxTool(self.dws, ...))`
收敛为「声明清单 + 自动注入」。

- `BUILTIN_TOOL_MANIFEST`：集中声明所有内置工具类（单一真源）。
  新增工具只需在此追加一行类名，构造函数依赖由 `build_tool` 按参数名自动从
  `services` 注入，**无需手写任何 register 调用**。
- `build_tool`：用 `inspect` 读取 `__init__` 签名，按参数名从 services 表注入；
  有默认值且 services 无对应项的参数（如 WebSearchTool 的 `timeout`）沿用默认。
- `register_builtin_tools`：遍历 manifest 注册；`kb_search` 受 `enable_kb_search`
  开关控制，且注册后通过 `POST_REGISTER_HOOKS` 绑定共享 EmbeddingClient
  （Phase 2 语义路由依赖）。
"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from src.tools.base import BaseTool, ToolRouter

# —— 各工具模块（集中导入，新增工具在此追加对应 import）——
from src.tools.business import (
    GetAttendanceTool,
    SendDingTool,
    TransferApprovalTool,
)
from src.tools.calendar import GetCalendarEventsTool, CreateTodoTool
from src.tools.chat import SendMessageTool
from src.tools.contact import SearchContactTool
from src.tools.conversation import (
    GetUnreadTool,
    GetConversationInfoTool,
    SearchMessagesTool,
)
from src.tools.doc import SearchDocTool, GetDocContentTool
from src.tools.kb_search import KBSearchTool
from src.tools.management import (
    SystemStatusTool,
    MessageStatsTool,
    KeywordRulesTool,
    ConfigManageTool,
)
from src.tools.media import ImageUploadTool
from src.tools.memory import RecallMemoryTool, SaveMemoryTool
from src.tools.minutes import ListMinutesTool, GetMinutesTool
from src.tools.org import GetMyProfileTool, ListOrgsTool, GetCurrentOrgTool
from src.tools.wiki import (
    WikiSpaceListTool,
    WikiSpaceSearchTool,
    WikiNodeListTool,
    WikiNodeSearchTool,
)
from src.tools.oa_approval import (
    ApprovalListFormsTool,
    ApprovalSearchFormsTool,
    ApprovalGetDetailTool,
    ApprovalListPendingTool,
    ApprovalListTasksTool,
    ApprovalListInitiatedTool,
    ApprovalListExecutedTool,
)
from src.tools.web_search import WebSearchTool
from src.tools.weather import WeatherTool

logger = logging.getLogger(__name__)

# 内置工具声明清单（顺序仅供日志/可读性，不影响功能）。
# 新增工具：在此追加类名即可，依赖注入自动完成。
BUILTIN_TOOL_MANIFEST: list[type[BaseTool]] = [
    # 消息 / 文档 / 联系人
    SendMessageTool,
    SearchDocTool,
    GetDocContentTool,
    SearchContactTool,
    # 日历 / 待办
    GetCalendarEventsTool,
    CreateTodoTool,
    # 记忆
    RecallMemoryTool,
    SaveMemoryTool,
    # 联网 / 天气
    WebSearchTool,
    WeatherTool,
    # 管理 / 统计
    SystemStatusTool,
    MessageStatsTool,
    KeywordRulesTool,
    ConfigManageTool,
    # 会话增强
    GetUnreadTool,
    GetConversationInfoTool,
    SearchMessagesTool,
    # 组织 / 架构
    GetMyProfileTool,
    ListOrgsTool,
    GetCurrentOrgTool,
    # 钉钉业务（审批 / 考勤 / DING）
    TransferApprovalTool,
    GetAttendanceTool,
    SendDingTool,
    # AI 听记 / 会议纪要（只读，支撑会议类自动回复）
    ListMinutesTool,
    GetMinutesTool,
    # 钉钉知识库（只读，支撑「找知识库/文档」类自动回复）
    WikiSpaceListTool,
    WikiSpaceSearchTool,
    WikiNodeListTool,
    WikiNodeSearchTool,
    # 钉钉 OA 审批查询（只读，支撑「有哪些审批表单/待我审批/审批进度」类自动回复）
    ApprovalListFormsTool,
    ApprovalSearchFormsTool,
    ApprovalGetDetailTool,
    ApprovalListPendingTool,
    ApprovalListTasksTool,
    ApprovalListInitiatedTool,
    ApprovalListExecutedTool,
    # 媒体上传
    ImageUploadTool,
    # RAG 知识库（受 enable_kb_search 开关控制）
    KBSearchTool,
]

# 注册后钩子：工具名 -> 回调(tool, services)，用于注册完成后的特殊绑定。
POST_REGISTER_HOOKS: dict[str, Callable[[BaseTool, dict], None]] = {}


def build_tool(cls: type[BaseTool], services: dict) -> BaseTool | None:
    """按 `__init__` 参数名从 services 自动注入依赖，返回实例；无法构建返回 None。

    约定：services 的 key 与各工具构造函数的参数名一致（如 dws / store /
    self_user_id / min_similarity / db_path / embedding_client / embedding_config /
    config）。缺省且有默认值的参数沿用默认值；缺省且为必需参数则构建失败并跳过。

    注意：仅检视类自身定义的 `__init__`（含 base 模块里的），不向 object 上溯，
    避免把 `object.__init__(*args, **kwargs)` 当成必需参数。
    """
    init = cls.__init__
    # 找到真正定义 __init__ 的类（跳过 object，否则会误读 *args/**kwargs）
    owner = next(
        (c for c in cls.__mro__ if "__init__" in c.__dict__), None
    )
    if owner is None or owner is object:
        # 无自有 __init__ → 无参构造
        try:
            return cls()
        except Exception as e:  # noqa: BLE001
            logger.error("[Tools] 构建工具 %s 失败: %s", getattr(cls, "name", cls.__name__), e)
            return None
    try:
        sig = inspect.signature(init)
    except (TypeError, ValueError):
        logger.warning(
            "[Tools] 无法解析 %s 的构造函数签名，跳过", getattr(cls, "name", cls.__name__)
        )
        return None

    kwargs: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if pname == "self":
            continue
        if pname in services:
            kwargs[pname] = services[pname]
        elif param.default is inspect.Parameter.empty:
            logger.warning(
                "[Tools] 工具 %s 的必需参数 %s 无对应服务，跳过",
                getattr(cls, "name", cls.__name__), pname,
            )
            return None
        # 有默认值且 services 中无对应项 → 沿用默认值（如 timeout=10）

    try:
        return cls(**kwargs)
    except Exception as e:  # noqa: BLE001 - 构建失败需可观测，但不应中断注册流程
        logger.error("[Tools] 构建工具 %s 失败: %s", getattr(cls, "name", cls.__name__), e)
        return None


def bind_kb_search_embedding(tool: BaseTool, services: dict | None = None) -> None:
    """将 kb_search 持有的 EmbeddingClient 绑定为全局共享实例（Phase 2 语义路由）。

    供初始注册（POST_REGISTER_HOOKS）与热重载重建（_rebuild_kb_search_tool）共用，
    避免两处重复绑定逻辑漂移。
    """
    from src import semantic as semantic_index

    ec = getattr(tool, "embedding_client", None)
    if ec:
        semantic_index.set_embedding_client(ec)
        semantic_index.invalidate_all()
    logger.info(
        "[RAG] 知识库搜索工具已注册（embedding_enabled=%s）",
        bool(ec and getattr(ec, "enabled", False)),
    )


POST_REGISTER_HOOKS["kb_search"] = bind_kb_search_embedding


def get_builtin_tool_platforms() -> dict[str, list[str]]:
    """返回所有内置工具的 {工具名: platforms} 映射（单一真源）。

    供 Web API 按平台过滤意图&路由映射时使用，避免在 intent.py 中硬编码平台信息
    或循环导入工具模块。
    """
    return {
        getattr(cls, "name", cls.__name__): list(getattr(cls, "platforms", []) or [])
        for cls in BUILTIN_TOOL_MANIFEST
    }


def register_builtin_tools(
    router: ToolRouter,
    services: dict,
    *,
    enable_kb_search: bool = True,
) -> list[str]:
    """自动发现并注册所有内置工具，返回已注册工具名列表。

    Args:
        router: 目标 ToolRouter 实例。
        services: 依赖服务表（key=构造函数参数名）。
        enable_kb_search: 是否注册 kb_search（受 config.tools.kb_search_enabled 控制）。
    """
    registered: list[str] = []
    for cls in BUILTIN_TOOL_MANIFEST:
        name = getattr(cls, "name", None) or cls.__name__
        if name == "kb_search" and not enable_kb_search:
            logger.info("[Tools] KB 搜索已禁用，跳过 kb_search 注册")
            continue

        tool = build_tool(cls, services)
        if tool is None:
            continue

        router.register(tool)
        registered.append(tool.name)

        # 注册后钩子（如 kb_search 绑定共享 embedding 客户端）
        hook = POST_REGISTER_HOOKS.get(tool.name)
        if hook:
            try:
                hook(tool, services)
            except Exception as e:  # noqa: BLE001
                logger.error("[Tools] 后置钩子执行失败 (%s): %s", tool.name, e)

    return registered
