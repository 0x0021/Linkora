from __future__ import annotations
from .engine_mixins_base import EngineMixinBase

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
from .reply_helpers import ReplyHelpersMixin, _citation_relevant_to_reply  # F1：回复增强子系统抽离到 reply_helpers，此处兼容 re-export
from .reply_shard import REPLY_SHARD_LIMIT_DEFAULT, shard_reply_text  # F15：超长回复分片
import functools
import logging
from src.paths import get_skills_root
from src.im_adapter.errors import IMAdapterRateLimitError  # F14：回复发送限频退避

from src.poller_utils import match_notification_signature, is_read_receipt_content

logger = logging.getLogger("src.platform.runtime")

# F15：分片之间的发送间隔（秒）。DWS CLI 是同步调用，理论上顺序有保证，
# 但服务端入库时间戳粒度可能相同导致客户端乱序展示，留一个极小的间隔更稳。
SHARD_SEND_INTERVAL_SECONDS = 0.2

# F14：回复发送退避相关回落默认（config 缺字段时）。一般不应走到这里。
REPLY_SEND_MIN_INTERVAL_DEFAULT = 0.2
REPLY_SEND_RATE_LIMIT_BACKOFF_DEFAULT = 60.0
# 限频信号文本嗅探（钉钉把 429/"rate limit exceeded" 归类为不可重试错误，
# 而非 IMAdapterRateLimitError，故需文本兜底才能识别流控）。
_RATE_LIMIT_HINTS = ("rate limit", "ratelimit", "429", "rate_limit",
                     "频控", "too many requests", "throttl", "quota exceeded")



class LifecycleMixin(EngineMixinBase):
    """运行时：lifecycle 相关方法（从 runtime.py 抽离，零行为变更）。"""
    def _init_runtime(self) -> None:
        """初始化运行时状态：防抖、并发控制、限速器、优雅退出等。"""
        self._running = False

        # 消息防抖合并机制
        self._pending_messages: dict[str, list] = {}
        self._pending_platform: dict[tuple, str] = {}  # (chat_id,sender_id) -> 平台 id（供定时器恢复上下文）
        self._pending_timers: dict[str, threading.Timer] = {}
        self._timer_lock = threading.Lock()

        # 防抖「纯数据」批次监控状态
        self._pending_first_seen: dict[tuple, float] = {}
        self._pending_incomplete_wait: dict[tuple, bool] = {}
        self._incomplete_delay_count = 0
        self._incomplete_extra_sec = 0.0
        self._incomplete_fired_with_request = 0
        self._incomplete_fired_without_request = 0

        # 回复冷却 & 并发控制
        # _replying_chats: chat_id -> 持锁令牌（每次 acquire 生成 uuid）。
        # 用令牌而非简单 set 登记，使释放时仅当令牌匹配才删，杜绝
        # 「看门狗强制释放陈旧锁 + 旧持有线程 finally 误删新锁」导致的同会话并发重复回复。
        self._replying_chats: dict[str, str] = {}
        self._replying_lock = threading.Lock()
        # 回复锁防死锁看门狗：chat_id -> 上锁时刻。单条回复处理若卡死超过阈值，
        # 下次锁竞争时强制释放，避免会话被「假正在回复中」永久阻塞。
        self._replying_since: dict[str, float] = {}
        # 回复锁竞争重试计数：chat_id -> 已重试次数（避免无限重入）。
        self._reply_lock_retries: dict[str, int] = {}
        # 平台级并发控制已迁移到 PlatformContext.reply_semaphore，
        # 每个平台独立维护回复槽位，避免全局信号量被跨平台滥用。
        # 瞬时发送失败退避：msg_key -> 退避截止时间戳，避免同一条消息每轮硬刷重发
        self._send_backoff_until: dict[str, float] = {}
        self._backoff_cleanup_counter: int = 0
        # F14：回复发送退避状态
        # 连续回复最小间隔护栏：上次发送时间戳 + 锁（跨线程安全）。
        self._last_reply_send_ts: float = 0.0
        self._reply_send_throttle_lock = threading.Lock()
        # 平台级限频护栏：命中限频后，至该时间戳前暂停所有回复发送（防同轮继续轰炸）。
        self._reply_rate_limited_until: float = 0.0
        # 指标计数器跨线程安全：多个 Timer/回复线程并发自增，用专用锁避免竞态丢计数。
        self._metrics_lock = threading.Lock()

        # 后台 LLM 任务限速器
        self._bg_throttle = BackgroundLLMThrottle(self.config.llm_throttle)
        self._last_extract_time: dict[str, float] = {}
        self._last_extract_time_lock = threading.Lock()
        # 记忆提取线程池（限制并发，防止突发流量创建过多线程）
        self._memory_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-extract")

        # 注册到全局状态
        set_app_instance(self)
        set_config_reload_callback(self.reload_config)

        # 优雅退出
        self._shutdown_event = threading.Event()
        self._bg_threads: list[threading.Thread] = []
    @property
    def _active_ctx(self) -> PlatformContext:
        """当前运行期平台上下文（由 _active_platform_ctx 决定，缺省回退主平台）。"""
        pid = _active_platform_ctx.get()
        ctx = self.platforms.get(pid)
        if ctx is None:
            return self.platforms["dingtalk"]
        return ctx
    @property
    def store(self) -> "SQLiteStore":
        """当前平台的 SQLiteStore（多平台隔离）。"""
        return self._active_ctx.store
    @store.setter
    def store(self, value):
        # 向后兼容：直接赋值视为设置主平台(dingtalk)的 store。
        self._ensure_primary().store = value
    @property
    def dws(self) -> "DwsAdapter":
        """当前平台的 IM 适配器（多平台隔离）。"""
        return self._active_ctx.dws
    @dws.setter
    def dws(self, value):
        self._ensure_primary().dws = value
    @property
    def poller(self) -> "MessagePoller":
        """当前平台的消息轮询器（多平台隔离）。"""
        return self._active_ctx.poller
    @poller.setter
    def poller(self, value):
        self._ensure_primary().poller = value
    @property
    def llm_agent(self) -> "LLMAgent":
        """当前平台的 LLM 智能体（多平台隔离，持有本平台 store）。"""
        return self._active_ctx.llm_agent
    @llm_agent.setter
    def llm_agent(self, value):
        self._ensure_primary().llm_agent = value
    def _ensure_primary(self) -> PlatformContext:
        """确保主平台(dingtalk)的 PlatformContext 存在（供 setter 与测试兼容使用）。"""
        if not hasattr(self, "platforms") or self.platforms is None:
            self.platforms = {}
        if "dingtalk" not in self.platforms:
            self.platforms["dingtalk"] = PlatformContext(
                id="dingtalk", display_name="钉钉", enabled=True, adapter_type="dingtalk",
            )
        return self.platforms["dingtalk"]
    def _make_platform_callback(self, platform_id: str):
        """构造平台感知的消息回调：进入时设置运行期平台上下文，退出时复位。

        各平台轮询器在独立线程运行，ContextVar 在线程内隔离，故并发派发不同平台
        消息时互不干扰（无需加锁）。
        """
        def cb(message):
            # 平台上下文统一入口（src.memory.platform_context 为唯一真源）：
            # 一次性对齐 runtime 组件路由 + 仓储会话库路由 + 日志归属三套上下文，
            # 退出时全部复位。链路内所有共享模块日志（LLM/规则/poller 核心等）
            # 因此在 Web 日志视图按平台精确归属，而非落为「中性」漏到全平台。
            from src.memory.platform_context import platform_scope
            with platform_scope(platform_id):
                self.handle_message(message)
        return cb
    def _build_dws(self) -> "DwsAdapter":
        """构造 DwsAdapter 实例（dry_run 等配置在构造时固化，不再运行时就地改）。

        热重载时通过整体替换 self.dws 引用（原子操作）生效，避免跨线程并发修改
        共享实例属性（旧实现 self.dws.dry_run = ... 在 web 回调线程与 poller/摘要线程
        间存在竞态，可能让 dry-run 模式误发消息）。
        """
        return DwsAdapter(
            cli_path=self.config.dws.cli_path,
            timeout=self.config.dws.timeout,
            retries=self.config.dws.retries,
            dry_run=self.config.dws.dry_run,
            profile=self.config.dws.profile,
            ai_tag_default=self.config.poller.ai_tag_enabled,
        )
    def reload_config(self) -> None:
        """热重载配置，无需重启服务。"""
        try:
            logger.info("正在热重载配置...")
            self.config = load_config(self.config_path)
            # 同步发布到共享单例，使 Web API 立即读到新配置（无需读磁盘）
            set_config(self.config)

            # 【启动自检 #1】热重载后重算白名单漂移，确保 Web 改配置后立即反映
            self._tool_whitelist_drift = self._compute_tool_whitelist_drift()

            # 更新各个组件的配置
            # dry_run 等配置在 DwsAdapter 构造时固化，故热重载时整体替换实例（原子引用替换，
            # 不影响其他线程已捕获的旧实例引用），彻底消除跨线程就地改共享属性的竞态。
            # 【Phase 3 多平台】遍历所有平台分别更新其 dws/poller/llm_agent/store，
            # 而非只改 self.* （self.* 是运行期上下文属性，不能整体赋值）。
            for _pid, _ctx in self.platforms.items():
                if _ctx.dws is not None:
                    _ctx.dws = self._build_adapter(_ctx.config) if _ctx.config else self._build_dws()
                if _ctx.poller is not None:
                    _ctx.poller.config = self.config.poller
                if _ctx.llm_agent is not None:
                    _ctx.llm_agent.config = self.config.llm
                    # 【Phase 3 修复】agent 持有的 skills_config 是构造时捕获的旧引用，
                    # reload 必须同步更新，否则前端改 skills.semantic_routing / combo_* 后不生效。
                    _ctx.llm_agent.skills_config = self.config.skills
                    # 【Persona 修复】前端改 persona_style_prompt / few_shot_examples 后，
                    # 热重载必须清掉 agent 内的 _style_prompt_cache，否则旧画像仍生效。
                    if getattr(_ctx.llm_agent, "_style_prompt_cache", None) is not None:
                        _ctx.llm_agent._style_prompt_cache = None
                if _ctx.store is not None:
                    _ctx.store.set_decisions_retention_days(self.config.storage.decisions_retention_days)
            self.llm_client.config = self.config.llm
            self.rule_engine.reload_config(self.config.rules)
            self.tool_router.config = self.config.tools
            self._bg_throttle.cfg = self.config.llm_throttle

            # 如果 embedding 配置变更（enabled/provider/offline/model 任一变化），
            # 重新初始化模型并重建 KBSearchTool。offline 开关变更也在此触发重载，
            # 无需重启即可生效。
            old_emb = self.embedding_client.config
            old_emb_enabled = self.embedding_client.enabled
            old_emb_sig = (
                old_emb_enabled,
                getattr(old_emb, "provider", ""),
                bool(getattr(old_emb, "offline", False)),
                getattr(old_emb, "model", ""),
            )
            self.embedding_client.config = self.config.embedding
            new_emb = self.config.embedding
            new_emb_sig = (
                new_emb.enabled,
                getattr(new_emb, "provider", ""),
                bool(getattr(new_emb, "offline", False)),
                getattr(new_emb, "model", ""),
            )
            if old_emb_sig != new_emb_sig:
                self.embedding_client.reload()
                self._rebuild_kb_search_tool()

            logger.info("配置热重载完成")
        except Exception as e:
            logger.error("配置热重载失败: %s", e)
