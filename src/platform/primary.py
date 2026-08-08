from __future__ import annotations
from .engine_mixins_base import EngineMixinBase
from ._timeout import run_with_timeout

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # `from .base import *` 并不导出 BaseIMAdapter，_build_adapter 的返回注解需显式
    # 声明来源（惰性注解，运行时不求值，无循环导入风险）。
    from src.im_adapter.base_adapter import BaseIMAdapter

logger = logging.getLogger(__name__)

# 【P0-2026-08-08】SQLite connect + integrity_check + schema 迁移超时秒数。
# 由 _init_primary_components 内的超时保护使用（run_with_timeout）；提为模块级常量
# 便于测试注入小超时，避免直接等满 30s。
_DB_INIT_TIMEOUT = 30


class PrimaryMixin(EngineMixinBase):
    _metrics_lock = threading.Lock()

    _INCOMPLETE_STRUCT_RE = re.compile(r"SX-\d+|工号|员工号|部门|手机号|1[3-9]\d{9}")

    _INCOMPLETE_REQUEST_VERBS = (
        "请", "麻烦", "帮我", "申请", "开通", "注册", "办理", "新增", "创建",
        "添加", "开一下", "弄一下", "怎么", "如何", "需要", "我要", "给我",
    )

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.platforms: dict[str, PlatformContext] = {}
        # 提前初始化当前用户信息字段，避免 _init_user 内调用
        # _resolve_own_open_dingtalk_id() 时访问尚未赋值的 current_open_dingtalk_id
        # 触发 AttributeError（表现为启动 WARNING「提取 openDingTalkId 失败」）。
        self.current_user_id = ""
        self.current_user_name = ""
        self.current_user_dept = ""
        self.current_user_org = ""
        self.current_user_title = ""
        self.current_open_dingtalk_id = ""
        self._init_config()
        logger.info("[初始化] 步骤 1/8: 加载配置... 完成")
        # 先构造主平台(dingtalk)的 store/dws，使后续 _init_rules/_init_llm/_init_scheduler
        # 经 self.store/self.dws 属性（默认解析到主平台上下文）正确初始化。
        self._init_primary_components()
        logger.info("[初始化] 步骤 2/8: 初始化核心组件... 完成")
        self._init_user()
        logger.info("[初始化] 步骤 3/8: 加载用户信息... 完成")
        self._init_rules()
        logger.info("[初始化] 步骤 4/8: 初始化规则引擎... 完成")
        self._init_tools_and_llm()
        logger.info("[初始化] 步骤 5/8: 初始化工具和 LLM... 完成")
        self._init_scheduler()
        logger.info("[初始化] 步骤 6/8: 初始化调度器... 完成")
        # 为配置中其余已启用平台构建独立 store/dws/poller/llm_agent（多平台隔离）。
        self._init_secondary_platforms()
        logger.info("[初始化] 步骤 7/8: 初始化辅助平台... 完成")
        self._init_runtime()
        logger.info("[初始化] 步骤 8/8: 初始化运行时状态... 完成")

    def _init_config(self) -> None:
        """加载配置、日志初始化、发布配置单例。"""
        try:
            self.config = load_config(self.config_path)
        except (ValidationError, ValueError) as e:
            # fail-closed：不安全配置（如开启认证但密码为空）拒绝启动
            logger.error(
                "配置校验失败，进程拒绝启动（安全默认）：%s", e
            )
            sys.exit(1)
        set_config(self.config)
        setup_logger(
            level=self.config.logging.level,
            log_file=self.config.logging.file,
            max_size_mb=self.config.logging.max_size_mb,
            max_backups=self.config.logging.max_backups,
        )
        logger.info("正在初始化灵桥(Linkora)...")

    def _platform_config(self, platform_id: str):
        """取某平台的 PlatformConfig 快照（多平台隔离）。"""
        for p in self.config.platforms:
            if p.id == platform_id:
                return p
        return None

    def _init_primary_components(self) -> None:
        """初始化主平台(dingtalk)的 SQLite 库与 DWS 适配器（多平台隔离的默认上下文）。

        其 store/dws 同时作为 self.store/self.dws 属性的默认解析目标，保证所有既
        有单平台代码路径（规则引擎、工具、LLM、轮询器）在零改动下继续工作。
        """
        logger.info("[启动] _init_primary_components: 开始初始化主平台组件")

        # ── Step 1: 加载平台配置 ──
        logger.debug("[启动] Step 1/6: 加载 dingtalk 平台配置")
        pcfg = self._platform_config("dingtalk")

        # ── Step 2: 构造 PlatformContext ──
        logger.debug("[启动] Step 2/6: 构造 PlatformContext")
        primary = PlatformContext(
            id="dingtalk",
            display_name=pcfg.display_name if pcfg else "钉钉",
            enabled=True,
            adapter_type="dingtalk",
            config=pcfg,
            reply_semaphore=threading.Semaphore(
                max(1, getattr(self.config.poller, 'max_concurrent_replies', 4)),
            ),
        )

        # ── Step 3: SQLiteStore 初始化（含超时保护） ──
        logger.debug("[启动] Step 3/6: 初始化 SQLiteStore (%s)", self.config.storage.path)
        from src.memory.store_factory import get_store
        # 先绑局部变量再挂到 ctx 上：闭包捕获局部的 store 而非 primary.store，
        # 既避免"ctx.store 在别处被置 None 后闭包里空指针"的竞态，
        # 也让 SQLiteStore | None 的可选性在此处收敛掉。
        store = get_store(self.config.storage.path)
        primary.store = store

        def _db_init():
            store.init_db()
            store.set_decisions_retention_days(
                self.config.storage.decisions_retention_days,
            )
            return True

        # 【P0-2026-08-08】用共享原语 run_with_timeout 做超时保护：显式 executor +
        # shutdown(wait=False, cancel_futures=True)，超时/异常后立即返回，不阻塞
        # 启动流程；后台 init_db worker 跑完自行退出（避免 `with` 块里
        # shutdown(wait=True) 让超时保护形同虚设的已复现问题）。
        result, timed_out, raised = run_with_timeout(
            _db_init, timeout=_DB_INIT_TIMEOUT, thread_name="db-init")
        if timed_out or raised:
            logger.error(
                "[启动] Step 3/6 超时/失败: SQLiteStore 初始化跳过"
                "（后续首次访问 conn 时将 lazy-init 重试）"
            )
            # 重置标志位以便主线程 lazy-init 重试
            primary.store._schema_initialized = False
        else:
            logger.info("[启动] Step 3/6 完成: SQLiteStore 初始化成功")

        # ── Step 4: 绑定决策追踪器 ──
        logger.debug("[启动] Step 4/6: 绑定决策追踪器到 SQLiteStore")
        tracker.set_sqlite_store(primary.store)

        # ── Step 5: 构造 DWS 适配器（纯构造，无网络/CLI 调用） ──
        logger.debug("[启动] Step 5/6: 构造 DwsAdapter（无网络/CLI，纯属性赋值）")
        try:
            primary.dws = self._build_dws()
        except Exception as e:  # noqa: BLE001
            # 主平台 DWS 构造失败不应中止整个启动：降级为无 DWS，
            # 后续依赖 DWS 的读消息/工具会在各自路径内 [resilience] 兜底。
            logger.error(
                "[resilience] 主平台 DwsAdapter 构造失败（降级为无 DWS，"
                "后续相关操作将自行兜底）: %s", e
            )
            primary.dws = None

        # ── Step 6: 注册主平台 ──
        self.platforms["dingtalk"] = primary
        logger.info("[启动] _init_primary_components: 主平台组件初始化完成")

    def _init_user(self) -> None:
        """加载当前登录用户信息。"""
        user_info = self._load_current_user()
        self.current_user_id = user_info.get("userId", "")
        self.current_user_name = user_info.get("userName", "")
        self.current_user_dept = user_info.get("dept", "")
        self.current_user_org = user_info.get("orgName", "")
        self.current_user_title = user_info.get("title", "")
        # 末尾 `or ""` 做归一化：_resolve_own_open_dingtalk_id() 可能返回 None，
        # 而所有下游消费点（runtime_dispatch / runtime_inbound / memory 等）
        # 一律按 truthy 判断，None 与 "" 语义等价——统一成 str 后即可去掉
        # 沿途各处的 `or ""` 防御，也让类型收敛为非 Optional。
        self.current_open_dingtalk_id = (
            user_info.get("openDingTalkId") or self._resolve_own_open_dingtalk_id() or ""
        )
        _oid = self.current_open_dingtalk_id
        _uid = self.current_user_id
        _dept = self.current_user_dept or ""
        # 脱敏：openDingTalkId / userId 属敏感标识，日志仅保留首尾各 2 位；
        # 部门名短，仅保留首尾各 1 位，避免明文落盘（CWE-532）。
        _oid_display = f"{_oid[:2]}***{_oid[-2:]}" if len(_oid) > 4 else "***"
        _uid_display = f"{_uid[:2]}***{_uid[-2:]}" if len(_uid) > 4 else "***"
        _dept_display = f"{_dept[:1]}**{_dept[-1:]}" if len(_dept) > 2 else ("**" if _dept else "-")
        logger.info("当前用户: %s (userId: %s, openDingTalkId: %s, 部门: %s)",
                    self.current_user_name, _uid_display, _oid_display, _dept_display)

    def _merge_platform_title(self, candidate: str) -> None:
        """跨平台聚合 owner 职位：仅当当前为空时采用（钉钉主平台优先）。"""
        candidate = (candidate or "").strip()
        if candidate and not getattr(self, "current_user_title", ""):
            self.current_user_title = candidate

    def _init_rules(self) -> None:
        """初始化规则引擎。"""
        self.rule_engine = RuleEngine(self.config.rules, db_store=self.store)

    def _init_tools_and_llm(self) -> None:
        """初始化 Embedding、工具注册并做启动校验，最后初始化 LLM。"""
        self._setup_embedding()
        self._setup_tools()
        validate_tool_action_coverage(self.config.tools.available)
        from src.intent import default_registry
        for _tname, _tool in self.tool_router._tools.items():
            _cats = getattr(_tool, "intent_categories", None)
            if _cats:
                default_registry.validate_tool_intent_categories(_tname, _cats)
        self._setup_llm()

    def _init_scheduler(self) -> None:
        """初始化主平台(dingtalk)的消息轮询器、文档同步调度器和数据库备份。"""
        self.platforms["dingtalk"].poller = MessagePoller(
            config=self.config.poller,
            dws=self.dws,
            store=self.store,
            current_user_id=self.current_open_dingtalk_id or self.current_user_id,
            current_user_name=self.current_user_name,
            current_user_user_id=self.current_user_id,
            rule_engine=self.rule_engine,
            platform_id="dingtalk",
        )
        self.doc_sync_scheduler = DocSyncScheduler(
            dws=self.dws,
            db_path=self.store.db_path,
            sync_interval_seconds=self.config.storage.doc_sync_interval_hours * 3600,
            embedding_client=self.embedding_client,
        )
        self.db_backups: dict[str, DatabaseBackup] = {}
        if self.config.storage.backup_enabled:
            dt_backup = DatabaseBackup(
                db_path=self.config.storage.path,
                backup_dir=self.config.storage.backup_dir,
                interval_hours=self.config.storage.backup_interval_hours,
                max_backups=self.config.storage.backup_max_count,
                backup_on_start=self.config.storage.backup_on_start,
            )
            self.db_backups["dingtalk"] = dt_backup

        # 多平台备份协调器在启动阶段（所有平台就绪后）构建，见 _start_backup_scheduler()
        self.db_backup: "DatabaseBackupCoordinator | None" = None

    def _init_secondary_platforms(self) -> None:
        """为配置中除 dingtalk 外的每个已启用平台构建独立运行期组件（多平台隔离）。

        单个平台初始化失败（CLI 缺失/DB 异常/LLM 构造异常等）经 init_platform_safe
        隔离：仅记录 [resilience] 并跳过该平台，绝不拖垮主平台与其他平台的启动。
        """
        from src.platform.resilience import init_platform_safe

        for pcfg in self.config.platforms:
            if pcfg.id == "dingtalk":
                continue
            if not pcfg.enabled:
                logger.info("[多平台] 平台 %s(%s) 未启用，跳过运行期初始化", pcfg.id, pcfg.display_name)
                continue

            def _register(ctx, _pcfg=pcfg):
                self.platforms[_pcfg.id] = ctx
                # 决策追踪器多平台隔离：为每个平台注册独立 store
                tracker.add_platform_store(_pcfg.id, ctx.store)
                logger.info(
                    "[多平台] 已初始化平台 %s(%s)，适配器 %s，数据库 %s",
                    _pcfg.id, _pcfg.display_name, _pcfg.adapter_type, ctx.store.db_path,
                )
                if self.config.storage.backup_enabled:
                    backup = DatabaseBackup(
                        db_path=_pcfg.storage.path,
                        backup_dir=self.config.storage.backup_dir,
                        interval_hours=self.config.storage.backup_interval_hours,
                        max_backups=self.config.storage.backup_max_count,
                        backup_on_start=self.config.storage.backup_on_start,
                    )
                    self.db_backups[_pcfg.id] = backup

            init_platform_safe(
                pcfg.id, pcfg.display_name,
                build=lambda _p=pcfg: self._build_platform_context(_p),
                register=_register,
            )

    def _start_backup_scheduler(self) -> None:
        """启动多平台数据库备份协调器。

        单个后台 daemon 线程按队列串行备份所有平台库（一个完成再下一个），
        启动过程不被备份阻塞；各库已按文件派生独立跨进程锁名，互不排斥，
        彻底消除旧实现“多平台同时抢同一把锁被非阻塞跳过”的误报与漏备。
        """
        storage = getattr(self.config, "storage", None)
        if not getattr(storage, "backup_enabled", False):
            logger.info("数据库备份已禁用（backup_enabled=false 或配置缺失），跳过启动")
            return
        backups = getattr(self, "db_backups", {})
        if not backups:
            logger.warning("无可用数据库备份实例，跳过备份协调器启动")
            return
        self.db_backup = DatabaseBackupCoordinator(
            backups=list(backups.values()),
            interval_hours=getattr(storage, "backup_interval_hours", 24),
            backup_on_start=getattr(storage, "backup_on_start", True),
            stagger_seconds=2.0,
        )
        self.db_backup.start()
        logger.info(
            "[备份] 已启动 %d 个平台数据库备份协调器（异步排队，不阻塞启动）",
            len(backups),
        )

    def _build_platform_context(self, pcfg) -> PlatformContext:
        """按 PlatformConfig 构造单个平台的完整运行期组件集合。"""
        from src.memory.store_factory import get_store
        store = get_store(pcfg.storage.path)
        store.init_db()
        logger.info("[%s] 数据库 schema 初始化完成: %s", pcfg.id, pcfg.storage.path)
        store.set_decisions_retention_days(self.config.storage.decisions_retention_days)
        dws = self._build_adapter(pcfg)
        poller_cfg = pcfg.poller or self.config.poller

        platform_user_id = self.current_open_dingtalk_id or self.current_user_id
        platform_user_user_id = self.current_user_id
        if pcfg.adapter_type == "feishu":
            try:
                self_info = dws.contact_user_get_self()
                if isinstance(self_info, dict):
                    feishu_user_id = self_info.get("user_id") or self_info.get("id") or ""
                    if feishu_user_id:
                        platform_user_id = feishu_user_id
                        platform_user_user_id = feishu_user_id
                        logger.info("[%s] 已获取飞书用户 ID: %s", pcfg.id, feishu_user_id[:30])
                    else:
                        logger.info("[%s] 飞书未返回 user_id，将使用 bot 应用 ID 进行自我消息过滤", pcfg.id)
                    # 聚合飞书侧的职位/岗位字段（飞书 user dict 含 title）
                    self._merge_platform_title(self_info.get("title") or "")
                else:
                    logger.warning("[%s] 飞书用户信息返回非 dict: %s", pcfg.id, type(self_info).__name__)
            except Exception as e:
                logger.warning("[%s] 获取飞书用户信息失败，自我消息检测可能受限: %s", pcfg.id, e)
                # 飞书获取自身 ID 失败时不应回退到钉钉 ID（永远不匹配飞书 sender），
                # 置空以让 poller 层启用更宽松的过滤而非用错误 ID 做精确匹配。
                platform_user_id = ""
                platform_user_user_id = ""

        # 镜像飞书：企微也需解析自身 user_id，否则用钉钉 id 做自我过滤会失效
        # （bot 可能回自己的消息）。失败时同样置空，不回退到钉钉 ID。
        if pcfg.adapter_type == "wecom":
            try:
                self_info = dws.contact_user_get_self()
                if isinstance(self_info, dict):
                    wecom_user_id = self_info.get("user_id") or self_info.get("id") or ""
                    if wecom_user_id:
                        platform_user_id = wecom_user_id
                        platform_user_user_id = wecom_user_id
                        logger.info("[%s] 已获取企业微信用户 ID: %s", pcfg.id, wecom_user_id[:30])
                    else:
                        logger.info("[%s] 企业微信未返回 user_id，将使用空 ID 进行自我消息过滤", pcfg.id)
                    # 聚合企微侧的职位/岗位字段（企微 user 对象含 position）
                    self._merge_platform_title(self_info.get("position") or self_info.get("title") or "")
                else:
                    logger.warning("[%s] 企业微信用户信息返回非 dict: %s", pcfg.id, type(self_info).__name__)
            except Exception as e:
                logger.warning("[%s] 获取企业微信用户信息失败，自我消息检测可能受限: %s", pcfg.id, e)
                platform_user_id = ""
                platform_user_user_id = ""

        poller = MessagePoller(
            config=poller_cfg,
            dws=dws,
            store=store,
            current_user_id=platform_user_id,
            current_user_name=self.current_user_name,
            current_user_user_id=platform_user_user_id,
            rule_engine=self.rule_engine,
            platform_id=pcfg.id,
        )
        llm_agent = LLMAgent(
            config=self.config.llm,
            client=self.llm_client,
            tool_router=self.tool_router,
            user_name=self.current_user_name,
            user_dept=self.current_user_dept,
            org_name=self.current_user_org,
            user_title=getattr(self, "current_user_title", ""),
            store=store,
            skill_manager=self._skill_manager,
            skills_config=self.config.skills,
            platform_id=pcfg.id,
            # 跨平台画像隔离：非主平台无画像时回退到主平台(dingtalk)底模
            fallback_store=self.store,
            # few-shot 按平台隔离：读取本平台 DB 中的样例（与画像同库）
            few_shot_examples=store._few_shot_repo.get_few_shot_examples(),
        )
        # H2-A：为每个平台（独立 LLMAgent + 独立 SQLiteStore）接线一个后台异步摘要调度器。
        # 两步接线避免 agent↔scheduler 循环依赖：先建 agent，再建 scheduler(agent)，最后回赋值。
        summary_scheduler = SummaryScheduler(agent=llm_agent, store=store)
        llm_agent._summary_scheduler = summary_scheduler
        summary_scheduler.start()
        logger.info("[H2-A] 平台 %s 后台异步摘要调度器已启动", pcfg.id)
        return PlatformContext(
            id=pcfg.id,
            display_name=pcfg.display_name,
            enabled=True,
            adapter_type=pcfg.adapter_type,
            store=store,
            dws=dws,
            poller=poller,
            llm_agent=llm_agent,
            config=pcfg,
            reply_semaphore=threading.Semaphore(
                max(1, getattr(poller_cfg, 'max_concurrent_replies', self.config.poller.max_concurrent_replies)),
            ),
            summary_scheduler=summary_scheduler,
        )

    def _build_adapter(self, pcfg) -> "BaseIMAdapter":
        """按平台类型构造对应 IM 适配器（多平台隔离）。

        优先使用 pcfg.adapter 配置，缺失时 fallback 到全局 dws 配置。
        CLI 二进制缺失时记录明确告警，适配器以 dry_run 模式降级运行，
        避免进程因 FileNotFoundError 直接崩溃（MEDIUM-10）。
        """
        dws_cfg = self.config.dws
        adapter_cfg = pcfg.adapter
        if pcfg.adapter_type == "feishu":
            cli_path = adapter_cfg.cli_path or shutil.which("lark-cli") or "/opt/homebrew/bin/lark-cli"
            if not os.path.isfile(cli_path) and not shutil.which("lark-cli"):
                logger.warning("[%s] lark-cli 未找到（%s），适配器将以 dry_run 降级运行", pcfg.id, cli_path)
                return FeishuCliAdapter(
                    cli_path=cli_path, timeout=adapter_cfg.timeout or dws_cfg.timeout,
                    retries=adapter_cfg.retries if adapter_cfg.retries > 0 else dws_cfg.retries,
                    dry_run=True, profile=adapter_cfg.profile or dws_cfg.profile,
                )
            return FeishuCliAdapter(
                cli_path=cli_path, timeout=adapter_cfg.timeout or dws_cfg.timeout,
                retries=adapter_cfg.retries if adapter_cfg.retries > 0 else dws_cfg.retries,
                dry_run=adapter_cfg.dry_run,
                profile=adapter_cfg.profile or dws_cfg.profile,
            )
        if pcfg.adapter_type == "wecom":
            cli_path = adapter_cfg.cli_path or shutil.which("wecom-cli") or "wecom-cli"
            if not os.path.isfile(cli_path) and not shutil.which("wecom-cli"):
                logger.warning("[%s] wecom-cli 未找到（%s），适配器将以 dry_run 降级运行", pcfg.id, cli_path)
                return WecomCliAdapter(
                    cli_path=cli_path, timeout=adapter_cfg.timeout or dws_cfg.timeout,
                    retries=adapter_cfg.retries if adapter_cfg.retries > 0 else dws_cfg.retries,
                    dry_run=True, profile=adapter_cfg.profile or dws_cfg.profile,
                )
            return WecomCliAdapter(
                cli_path=cli_path, timeout=adapter_cfg.timeout or dws_cfg.timeout,
                retries=adapter_cfg.retries if adapter_cfg.retries > 0 else dws_cfg.retries,
                dry_run=adapter_cfg.dry_run,
                profile=adapter_cfg.profile or dws_cfg.profile,
            )
        # 默认 dingtalk
        return self._build_dws()
