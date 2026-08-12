from __future__ import annotations

import logging
import secrets
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from src.audit import audit
from src.config import ToolsConfig

logger = logging.getLogger(__name__)


@dataclass
class ToolCallResult:
    tool_name: str
    args: dict
    success: bool
    result: Any
    error: str | None = None
    duration_ms: int = 0


@dataclass
class PendingConfirmation:
    """一次「需确认写操作」的待确认态。

    token 由 ToolRouter 生成，按 (session_key, token) 隔离存储；
    用户/LLM 携有效 token 再次调用同一工具时才会真正执行写操作。
    """

    token: str
    tool_name: str
    args: dict
    preview: str
    expires_at: float


class BaseTool(ABC):
    name: str
    description: str
    parameters: dict
    # 中文名（仅供外部展示/查询/文档使用，不参与 LLM 调用）
    display_name: str = ""
    # 30–50 字的简短中文描述（仅供外部展示/查询/文档使用，不参与 LLM 调用）
    short_description: str = ""
    # 意图关键词：当消息中包含这些词时，该工具被认为是"相关工具"
    # 子类可以覆盖这个字段，例如 ["天气", "气温", "下雨"]
    # 空列表表示不匹配任何特定意图（基础工具，始终包含）
    intent_keywords: list[str] = []
    # 意图类别（Phase 1 单一真源）：声明本工具服务哪些 domain.* 意图类别，
    # 路由时经 IntentRegistry.keywords_for_categories 解析出关键词。
    # 优先于 intent_keywords；未声明时回退到字面 intent_keywords（向后兼容）。
    intent_categories: list[str] = []
    # 平台归属：空列表 = 通用（全平台可见），["dingtalk"] = 钉钉专属，["feishu"] = 飞书专属 等。
    # 用于 Web 管理页面按平台过滤工具（意图&路由页、工具链路页），避免钉钉专属工具暴露给飞书/企微。
    platforms: list[str] = []
    # 是否需要「执行前二次确认」：True 时，ToolRouter 会在首次调用时拦截写操作，
    # 仅返回 confirm_required（含预览与令牌），携有效令牌再次调用才真正执行。
    # 用于不可逆/高责任写操作（如审批转交），防止误解析导致的越权变更。
    require_confirm: bool = False

    @abstractmethod
    def execute(self, args: dict) -> str | dict:
        pass

    def safe_execute(self, args: dict) -> str | dict:
        """模板方法：包 execute，把任何未捕获异常规范化为 {error:...}。

        设计动机：工具错误协议是「返回 {error} 而非抛异常」，但历史上各工具
        自行 try/except 风格不一，漏保护时异常会冒泡到路由层 except 分支。
        本方法把兜底下沉到基类——未来新增工具只需实现 execute 业务逻辑，
        无需手写 try/except，异常自动规范化为 {error} 且保留 traceback。
        既有工具已自行 try/except 的路径不受影响（先捕获者优先）。
        """
        try:
            return self.execute(args)
        except Exception as e:
            logger.exception("工具 %s 执行异常（safe_execute 兜底）: %s", self.name, e)
            return {"error": f"{self.name} 执行失败: {e}"}

    def build_confirmation_preview(self, args: dict) -> str:
        """生成「即将执行的写操作」的人类可读预览，用于确认阶段向用户展示。

        默认返回通用文案；高责任工具应覆写以提供精确的「将把 X 转给 Y」等信息。
        实现必须只读、且吞掉一切异常（预览失败不应阻塞确认流程）。
        """
        return f"即将执行工具 {self.name}，请确认。"

    def needs_confirm(self, args: dict) -> bool:
        """判断本次调用是否需要二次确认（默认仅看 require_confirm 类级开关）。

        高责任工具可覆写，实现「按参数条件确认」：例如 config_manage 仅在
        action=='update'（写盘）时要求确认，'view'（只读）直接放行。
        路由层以本方法返回值决定是否拦截，而非直接读 require_confirm。
        """
        return bool(getattr(self, "require_confirm", False))

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @property
    def effective_intent_keywords(self) -> list[str]:
        """路由用的有效意图关键词（Phase 1 单一真源解析点）。

        - 声明了 intent_categories → 经 IntentRegistry 解析为对应域类别的证据词；
        - 否则回退到字面 intent_keywords（如 KBSearchTool 的动态关键词、尚未迁移的工具）。
        这样关键词文本只维护在注册表一处，工具/技能仅声明"服务哪些类别"。
        """
        if getattr(self, "intent_categories", None):
            try:
                from src.intent import default_registry
                return default_registry.keywords_for_categories(self.intent_categories)
            except Exception:
                logger.debug("意图注册表解析失败，回退字面关键词（%d 个）", len(self.intent_keywords))
                return list(self.intent_keywords)
        return list(self.intent_keywords)

    def get_info(self) -> dict:
        """返回工具的元信息，供外部展示/查询/文档使用。
        注意：`name` 保持原始英文标识符不变（用于 LLM 调用的 tool_calls.name 匹配）。
        `display_name` 和 `short_description` 是仅供人看的中文别名与说明。"""
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "description": self.description,
            "short_description": self.short_description or self.description,
            "platforms": list(getattr(self, "platforms", []) or []),
        }


class RateLimiter:
    def __init__(self):
        self._calls: dict[str, list[float]] = {}
        self._session_calls: dict[str, list[float]] = {}  # key: "tool_name:chat_id"
        # 加锁: RateLimiter 单实例被主轮询/后台任务/Web 多线程共享,
        # 原 check() 对 _calls/_session_calls 的读写无锁, 并发时计数偶发丢失导致限频失效
        # (违背项目'禁止跨线程共享无保护状态'的工程规范)
        self._lock = threading.Lock()

    def check(self, tool_name: str, per_hour: int, chat_id: str | None = None) -> bool:
        with self._lock:
            now = time.time()
            window = 3600

            # 全局限流
            calls = self._calls.get(tool_name, [])
            calls = [t for t in calls if now - t < window]
            if len(calls) >= per_hour:
                return False
            calls.append(now)
            self._calls[tool_name] = calls

            # 会话级别限流（仅对 send_message 生效）
            if chat_id and tool_name == "send_message":
                session_key = f"{tool_name}:{chat_id}"
                session_calls = self._session_calls.get(session_key, [])
                session_calls = [t for t in session_calls if now - t < 300]  # 5分钟窗口
                if len(session_calls) >= 3:  # 同一会话5分钟内最多3次
                    return False
                session_calls.append(now)
                self._session_calls[session_key] = session_calls

            return True


class ToolRouter:
    # 连续失败阈值：工具连续失败达到此次数后日志从 WARNING 升级为 ERROR
    CONSECUTIVE_FAILURE_THRESHOLD = 3
    # 确认令牌有效期（秒）：超过则需重新发起操作。5 分钟足够完成一次人工确认。
    CONFIRM_TTL_SECONDS = 300
    # 单会话待确认上限（超出后清理最早过期项，防内存堆积）
    CONFIRM_MAX_PER_SESSION = 16

    def __init__(self, config: ToolsConfig):
        self.config = config
        self._tools: dict[str, BaseTool] = {}
        self.rate_limiter = RateLimiter()
        self._available = set(config.available) if config.enabled else set()
        # 来源追踪：工具名 → 'whitelist'（config.yaml 声明） / 'skill'（技能自动包装绕过白名单）
        # 用于审计与白名单漂移自检，区分「有意绕过白名单的技能工具」与「白名单声明的内建工具」。
        self._availability_sources: dict[str, str] = {
            name: "whitelist" for name in self._available
        }
        # 连续失败计数器：工具名 → 连续失败次数。首次成功能自动重置。
        self._consecutive_failures: dict[str, int] = {}
        # 待确认存储：session_key → {token → PendingConfirmation}
        # ToolRouter 被主轮询/后台任务/Web 多线程共享，需加锁保护。
        self._confirm_lock = threading.Lock()
        self._confirmations: dict[str, dict[str, PendingConfirmation]] = {}

    @property
    def consecutive_failures(self) -> dict[str, int]:
        """返回当前各工具的连续失败计数（只读）。外部用于 metrics 采集。"""
        return dict(self._consecutive_failures)

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        logger.info("已注册工具: %s", tool.name)

    def mark_available(self, name: str, *, source: str = "whitelist") -> None:
        """将工具加入「LLM 可调用」集合（替代直接 `self._available.add(...)`）。

        source 用于审计：
          - 'whitelist' : config.yaml 的 tools.available 声明的内建工具
          - 'skill'     : 技能自动包装（有意绕过白名单，因技能工具不在 tools.available 中）

        来源被追踪持有，供白名单漂移自检（compute_whitelist_drift）识别并排除这类
        有意绕过项，避免误报噪音削弱告警可信度。
        """
        self._available.add(name)
        self._availability_sources[name] = source

    def get_skill_sourced_tools(self) -> set[str]:
        """返回所有「技能自动包装」来源的工具名（有意绕过白名单）。"""
        return {n for n, s in self._availability_sources.items() if s == "skill"}

    def compute_whitelist_drift(self, whitelist: set[str]) -> dict:
        """计算白名单漂移（供启动自检 / Web 端点使用）。

        - 技能自动包装工具（source='skill'）有意绕过 tools.available 白名单，
          从 missing_in_whitelist 中排除，避免误报噪音；单独以
          skill_auto_wrapped 字段保留可见性。
        - 纯内建工具若不在白名单仍按「缺失」告警（P0-1 防护）。
        """
        registered = set(self._tools.keys())
        skill_tools = self.get_skill_sourced_tools()
        missing_in_whitelist = sorted((registered - whitelist) - skill_tools)
        return {
            "registered_count": len(registered),
            "whitelist_count": len(whitelist),
            "missing_in_whitelist": missing_in_whitelist,
            "stale_in_whitelist": sorted(whitelist - registered),
            "skill_auto_wrapped": sorted(skill_tools),
        }

    def unregister(self, name: str) -> None:
        """移除已注册工具（热重载 Embedding 禁用时调用）。"""
        self._tools.pop(name, None)
        logger.info("已移除工具: %s", name)

    def get_schemas(self) -> list[dict]:
        if not self.config.enabled:
            return []
        schemas = []
        for name in self._available:
            tool = self._tools.get(name)
            if tool:
                schemas.append(tool.to_openai_schema())
        return schemas

    def get_all_info(self) -> list[dict]:
        """返回所有已注册工具的元信息列表（含 display_name / short_description）。
        用于 Web 端展示，不参与 LLM 调用。"""
        infos = []
        for tool in self._tools.values():
            infos.append(tool.get_info())
        return infos

    def get_available_tool_names(self) -> list[str]:
        """返回所有已注册且已启用的工具名称列表。"""
        if not self.config.enabled:
            return []
        return [name for name in self._available if name in self._tools]

    def filter_schemas_by_names(self, tool_names: list[str]) -> list[dict]:
        """按工具名称列表返回对应的 OpenAI schema。"""
        if not self.config.enabled:
            return []
        schemas = []
        for name in tool_names:
            tool = self._tools.get(name)
            if tool:
                schemas.append(tool.to_openai_schema())
        return schemas

    def execute(self, tool_name: str, args: dict,
                session_key: str | None = None) -> ToolCallResult:
        if tool_name not in self._available:
            return ToolCallResult(
                tool_name=tool_name, args=args, success=False,
                result=None, error=f"Tool '{tool_name}' not in whitelist"
            )

        tool = self._tools.get(tool_name)
        if not tool:
            return ToolCallResult(
                tool_name=tool_name, args=args, success=False,
                result=None, error=f"Tool '{tool_name}' not registered"
            )

        # —— 确认令牌路径：携有效令牌才真正执行写操作 ——
        # 用 session_key（通常为 chat_id）隔离，避免跨会话令牌串用。
        confirm_token = (args or {}).get("confirm_token")
        if confirm_token:
            pending = self._take_pending(session_key or "", confirm_token, tool_name)
            if pending is None:
                return ToolCallResult(
                    tool_name=tool_name, args=args, success=False, result=None,
                    error="确认令牌无效或已过期，请重新发起操作",
                )
            # 用确认时锁定的原始参数执行，避免确认后被篡改（令牌路径不二次触发门控）
            return self._run_tool(tool_name, tool, pending.args, session_key)

        rate_cfg = self.config.rate_limit.get(tool_name, {})
        if rate_cfg:
            per_hour = rate_cfg.get("per_hour", 0)
            if per_hour:
                # 对 send_message 提取 chat_id 用于会话级别限流
                chat_id = args.get("chat_id") if tool_name == "send_message" else None
                if not self.rate_limiter.check(tool_name, per_hour, chat_id):
                    return ToolCallResult(
                        tool_name=tool_name, args=args, success=False,
                        result=None, error=f"Rate limit exceeded for '{tool_name}'"
                    )

        # —— 需确认工具：拦截写操作，仅返回 confirm_required（含预览+令牌）——
        # 用 needs_confirm(args) 而非直接读 require_confirm，支持「按参数条件确认」
        # （如 config_manage 仅 update 需确认、view 放行）。
        if tool.needs_confirm(args):
            preview = self._safe_preview(tool, args)
            token = self._gen_token()
            self._store_pending(session_key or "", tool_name, args, preview, token)
            logger.info("[确认门控] 工具 %s 已拦截，等待令牌确认（会话=%s）",
                        tool_name, session_key or "<none>")
            return ToolCallResult(
                tool_name=tool_name, args=args, success=True,
                result={
                    "status": "confirm_required",
                    "confirm_token": token,
                    "preview": preview,
                    "hint": "已将操作预检完成，请向用户确认；用户确认后再次调用本工具"
                            "并传入 confirm_token 方可执行。",
                },
                duration_ms=0,
            )

        return self._run_tool(tool_name, tool, args, session_key)

    def _run_tool(self, tool_name: str, tool: BaseTool, args: dict,
                  session_key: str | None = None) -> ToolCallResult:
        """真正执行工具并构建 ToolCallResult（含连续失败计数与耗时）。

        经 tool.safe_execute 执行：未捕获异常已在工具层规范化为 {error}，
        此处 except 分支仅作最终防线（safe_execute 自身异常时兜底）。
        """
        start = time.time()
        try:
            result: Any = tool.safe_execute(args)
            duration = int((time.time() - start) * 1000)
            # 检查工具自身是否通过返回 dict 中的 error 字段报告失败
            # 仅当 error 字段存在且值为非空字符串时才判定失败
            is_error = isinstance(result, dict) and bool(result.get("error"))
            if is_error:
                full_error = result.get("error", "") or ""
                self._consecutive_failures[tool_name] = self._consecutive_failures.get(tool_name, 0) + 1
                consecutive = self._consecutive_failures[tool_name]
                if consecutive >= self.CONSECUTIVE_FAILURE_THRESHOLD:
                    logger.error(
                        "工具 %s 已连续失败 %d 次（阈值=%d），最新错误: %s",
                        tool_name, consecutive, self.CONSECUTIVE_FAILURE_THRESHOLD,
                        full_error[:200]
                    )
                else:
                    logger.warning("工具 %s 执行完成但返回错误: %s", tool_name, full_error[:100])
                audit("tool_execution", tool_name, "failure",
                      session_key=session_key, target=tool_name,
                      detail=f"duration_ms={duration} error={full_error[:120]}")
            else:
                # 成功：重置连续失败计数器
                self._consecutive_failures.pop(tool_name, None)
                logger.info("工具 %s 执行完成，耗时 %d 毫秒", tool_name, duration)
                audit("tool_execution", tool_name, "success",
                      session_key=session_key, target=tool_name,
                      detail=f"duration_ms={duration}")
            return ToolCallResult(
                tool_name=tool_name, args=args, success=not is_error,
                result=result if not is_error else None,
                error=result.get("error", "") if is_error else "",
                duration_ms=duration
            )
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            self._consecutive_failures[tool_name] = self._consecutive_failures.get(tool_name, 0) + 1
            consecutive = self._consecutive_failures[tool_name]
            logger.error("工具 %s 执行异常（连续失败 %d 次）: %s", tool_name, consecutive, e, exc_info=True)
            audit("tool_execution", tool_name, "error",
                  session_key=session_key, target=tool_name,
                  detail=f"duration_ms={duration} exc={type(e).__name__}")
            return ToolCallResult(
                tool_name=tool_name, args=args, success=False,
                result=None, error=str(e), duration_ms=duration
            )

    # —— 确认门控辅助 ——

    def _safe_preview(self, tool: BaseTool, args: dict) -> str:
        """安全生成确认预览（吞掉一切异常，失败回退通用文案）。"""
        try:
            return tool.build_confirmation_preview(args)
        except Exception as e:
            logger.warning("[确认] 生成预览失败，回退通用文案: %s", e)
            return f"即将执行工具 {tool.name}，请确认。"

    @staticmethod
    def _gen_token() -> str:
        return secrets.token_hex(8)

    def _store_pending(self, session: str, tool_name: str, args: dict,
                       preview: str, token: str) -> None:
        pending = PendingConfirmation(
            token=token, tool_name=tool_name, args=dict(args),
            preview=preview, expires_at=time.time() + self.CONFIRM_TTL_SECONDS,
        )
        with self._confirm_lock:
            sess = self._confirmations.setdefault(session, {})
            sess[token] = pending
            # 容量保护：超出上限清理最早过期的项
            if len(sess) > self.CONFIRM_MAX_PER_SESSION:
                overflow = sorted(sess.items(), key=lambda kv: kv[1].expires_at)
                for k, _ in overflow[:len(sess) - self.CONFIRM_MAX_PER_SESSION]:
                    sess.pop(k, None)

    def _take_pending(self, session: str, token: str,
                      tool_name: str) -> PendingConfirmation | None:
        """取出并核销待确认项；无效/过期/工具不匹配返回 None。"""
        with self._confirm_lock:
            sess = self._confirmations.get(session)
            if not sess:
                return None
            pending = sess.get(token)
            if pending is None:
                return None
            if pending.expires_at < time.time():
                sess.pop(token, None)
                return None
            if pending.tool_name != tool_name:
                return None
            sess.pop(token, None)
            if not sess:
                self._confirmations.pop(session, None)
            return pending
