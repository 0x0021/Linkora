from __future__ import annotations
from .engine_mixins_base import EngineMixinBase
from ._timeout import run_with_timeout

from .base import *  # noqa: F403  (base re-exports 所有 src 顶层符号 + tracker/Message 等)
from .base import _active_platform_ctx  # 显式下划线符号
import logging
from src.paths import get_skills_root
from src.utils.security import mask_oid


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

# 【P0-2026-08-08】提取 openDingTalkId 的总超时秒数（每次 CLI 调用另限时 10s）。
# 由 _resolve_own_open_dingtalk_id 内的超时保护使用（run_with_timeout）；提为模块级
# 常量便于测试注入小超时，避免直接等满 60s。
OPEN_DINGTALK_ID_RESOLVE_TIMEOUT = 60



class SetupMixin(EngineMixinBase):
    """运行时：setup 相关方法（从 runtime.py 抽离，零行为变更）。"""
    def _rebuild_kb_search_tool(self) -> None:
        """热重载后重建 KBSearchTool，使其持有最新 embedding 配置（P2 修复）。"""
        try:
            kb_enabled = self.config.tools.kb_search_enabled
            # 先移除旧实例（若禁用则不再注册）
            self.tool_router.unregister("kb_search")
            if not kb_enabled:
                logger.info("[RAG] 热重载：KB 搜索已禁用，已移除 kb_search 工具")
                return
            embedding_config = self.config.embedding if hasattr(self.config, "embedding") else None
            # 【Phase 3 多平台】KB 搜索工具绑定到主平台(dingtalk)库：工具注册表为全平台
            # 共享单例，RAG 检索以主平台知识库为准（各平台独立知识库隔离留待后续细化）。
            self.tool_router.register(
                KBSearchTool(store=self.platforms["dingtalk"].store, embedding_config=embedding_config)
            )
            logger.info(
                "[RAG] 热重载：kb_search 工具已重建（embedding_enabled=%s）",
                embedding_config.enabled if embedding_config else False,
            )
            # Phase 2：重建后重新绑定共享 embedding 客户端（实例已变）
            from src.tools.registry import bind_kb_search_embedding
            kb_tool = self.tool_router._tools.get("kb_search")
            if kb_tool:
                bind_kb_search_embedding(kb_tool)
        except Exception as e:
            logger.error("[RAG] 热重载重建 kb_search 失败: %s", e)
    def _setup_embedding(self) -> None:
        # background=True：本地模型在后台线程下载/加载，Web 服务先起，下载进度可经
        # /api/embedding-status 轮询；模型就绪前 embed() 优雅降级返回 []。
        self.embedding_client = EmbeddingClient(self.config.embedding, background=True)
        logger.info("嵌入: %s",
                    "已启用（后台加载中）" if self.embedding_client.enabled else "已禁用")
        # 预热：后台线程等待模型就绪并做一次 dummy 推理，缩短首请求冷启动，
        # 不阻塞启动；耗时记入日志（[嵌入] 预热完成，首次推理耗时 X.XXs）。
        if self.embedding_client.enabled:
            threading.Thread(target=self.embedding_client.warmup, daemon=True).start()
            # 心跳保活（#7）：预热后启动周期 dummy 推理，防止模型长时间闲置被释放；
            # 与预热线程顺序无关，心跳自身会等待模型就绪再推理。
            self.embedding_client.start_heartbeat(self.config.embedding.heartbeat_interval)
    def _build_tool_services(self, self_id: str | list[str]) -> dict:
        """构造工具依赖服务表，供 registry.build_tool 按参数名自动注入。

        新增服务项时在此扩展；工具构造函数参数名需与 key 一致。
        """
        retrieval = getattr(self.config.memory, "retrieval", None) \
            if getattr(self.config, "memory", None) else None
        min_similarity = retrieval.get("min_similarity", 0.0) if isinstance(retrieval, dict) else 0.0
        return {
            "dws": self.dws,
            "store": self.store,
            "self_user_id": self_id,
            "min_similarity": min_similarity,
            "db_path": self.config.storage.path,
            "embedding_client": self.embedding_client,
            "embedding_config": getattr(self.config, "embedding", None),
            "config": self.config,
        }
    def _setup_tools(self) -> None:
        self.tool_router = ToolRouter(self.config.tools)
        # 【护栏 P0-3】传机器人自己的 openDingTalkId 和 userId 作为 self_user_id，
        # SendMessageTool 会以此为依据拒绝「发往自己会话」的调用，防 LLM 幻觉回声。
        # 【H10修复】同时传入两种 ID，确保无论 chat_id 用哪种格式都能匹配。
        self_ids = [x for x in [self.current_open_dingtalk_id, self.current_user_id] if x]
        services = self._build_tool_services(self_ids)

        # RAG 知识库搜索工具默认启用，除非配置明确禁用（ToolsConfig.kb_search_enabled）
        kb_search_enabled = self.config.tools.kb_search_enabled

        # P2-12：内置工具自动发现注册（声明在 src/tools/registry.py 的
        # BUILTIN_TOOL_MANIFEST，依赖按签名自动注入），替代原 35+ 处手工 register。
        from src.tools.registry import register_builtin_tools
        registered = register_builtin_tools(
            self.tool_router, services, enable_kb_search=kb_search_enabled
        )
        logger.info("[Tools] 自动注册完成：%d 个内置工具", len(registered))

        # 【启动自检 #1】config.yaml 的 tools.available 白名单 vs 代码实际注册工具
        # 防止「加了工具却忘改 yaml → yaml 整段覆盖静默屏蔽」的 26→23 式漂移。
        self._tool_whitelist_drift = self._compute_tool_whitelist_drift()
        _drift = self._tool_whitelist_drift
        if _drift["missing_in_whitelist"]:
            logger.warning(
                "⚠️ [配置自检] config.yaml 的 tools.available 静默屏蔽了 %d 个已注册工具: %s "
                "（如非故意，请同步 config.yaml 与 config.py 的 tools.available）",
                len(_drift["missing_in_whitelist"]), _drift["missing_in_whitelist"])
        if _drift["stale_in_whitelist"]:
            logger.warning(
                "⚠️ [配置自检] config.yaml 的 tools.available 含 %d 个无对应工具的条目(疑似拼写/已移除): %s",
                len(_drift["stale_in_whitelist"]), _drift["stale_in_whitelist"])
        if not _drift["missing_in_whitelist"] and not _drift["stale_in_whitelist"]:
            logger.info("[配置自检] tools.available 白名单与已注册工具一致（%d 个）",
                        _drift["registered_count"])

    def _rebuild_builtin_tools(self) -> None:
        """模块热重载后重建工具注册表（post-reload 回调）。

        清空旧注册、重新执行 register_builtin_tools，使工具模块的代码变更
        （如 weather.py 新增字段）立即生效，无需重启 bot。
        """
        if not hasattr(self, "tool_router") or self.tool_router is None:
            return
        try:
            self_ids = [x for x in [self.current_open_dingtalk_id, self.current_user_id] if x]
            services = self._build_tool_services(self_ids)
            kb_search_enabled = self.config.tools.kb_search_enabled

            from src.tools.registry import register_builtin_tools
            # 清空后重建（register_builtin_tools 内部会先 clear 再 register）
            registered = register_builtin_tools(
                self.tool_router, services, enable_kb_search=kb_search_enabled
            )
            logger.info(
                "[DevHotReload] 工具表已重建：%d 个内置工具（模块热重载触发）",
                len(registered),
            )
            # 重新计算白名单漂移
            self._tool_whitelist_drift = self._compute_tool_whitelist_drift()
        except Exception as e:
            logger.error("[DevHotReload] 工具表重建失败: %s", e, exc_info=False)

    def _setup_llm(self) -> None:
        self.llm_client = LLMClient(self.config.llm)

        # 初始化技能引擎
        skill_manager: SkillManager | None = None
        if self.config.skills.enabled:
            skill_manager = SkillManager(
                get_skills_root(),
                poll_interval=self.config.skills.hot_reload_interval,
            )
            skill_manager.reload()
            logger.info("[Skills] 技能引擎已加载，共 %d 个技能", len(skill_manager.list_names()))

            # 启动热加载监控（后台轮询 skills 目录变更）
            if self.config.skills.hot_reload:
                skill_manager.start_watcher()
                logger.info("[Skills] 热加载已启用（间隔 %.0fs）", self.config.skills.hot_reload_interval)

            # 开发态模块热重载（监控 src/tools/、src/llm/style.py 等 .py 变更，
            # 自动 importlib.reload() + 重建工具注册表）。生产必须关闭。
            if self.config.skills.module_hot_reload:
                from src.dev_hot_reload import ModuleHotReloader

                self._module_reloader = ModuleHotReloader(
                    project_root=get_skills_root().parent.parent,  # data/skills -> project root
                    poll_interval=self.config.skills.module_hot_reload_interval,
                    enabled=True,
                )
                # 注册 post-reload 回调：工具模块变更后重新注册所有内置工具
                self._module_reloader.register_post_reload_callback(
                    "rebuild_tools",
                    self._rebuild_builtin_tools,
                )
                self._module_reloader.start_watcher()
                logger.info(
                    "[DevHotReload] 模块热重载已启用（间隔 %.1fs）",
                    self.config.skills.module_hot_reload_interval,
                )
            else:
                self._module_reloader = None

            # 自动为未声明显式 allowed_tools 的技能生成 Tool 包装器
            # 使 LLM 可通过标准 tool_call 调用技能的 CLI 入口（如 python scripts/search.py "query"）
            from src.skills.tool_wrapper import SkillTool
            auto_wrapped = 0
            for skill in skill_manager.list_all():
                if not skill.allowed_tools and skill.enabled:
                    wrapper = SkillTool(skill)
                    # 跳过纯 Prompt 技能（无 CLI 入口不可执行，保留其 system prompt 注入即可）
                    if not wrapper.has_cli_entry:
                        continue
                    # tools.allow_skill_tools=false 时强制严格白名单：技能不包装为 Tool，
                    # 仅保留 system prompt 注入，运维可借此收紧攻击面。
                    if not self.config.tools.allow_skill_tools:
                        logger.info(
                            "[Skills] 技能 %s 具备 CLI 入口，但 tools.allow_skill_tools=false，"
                            "跳过自动包装为 Tool（仅保留 system prompt 注入）", skill.name)
                        continue
                    self.tool_router.register(wrapper)
                    # 技能工具有意绕过 tools.available 白名单（其名不在白名单中），
                    # 经受控、可审计的 mark_available 加入，来源标记为 'skill'，供漂移自检识别排除。
                    self.tool_router.mark_available(wrapper.name, source="skill")
                    skill.allowed_tools = [wrapper.name]
                    auto_wrapped += 1
            if auto_wrapped:
                logger.info(
                    "[Skills] 自动包装了 %d 个技能为 Tool（有意绕过白名单，受 tools.allow_skill_tools 管控）",
                    auto_wrapped)

        # 供 _build_platform_context 为其他平台复用同一技能引擎（共享单例）。
        self._skill_manager = skill_manager

        # 主平台(dingtalk)的 LLM 智能体写入对应 PlatformContext（多平台隔离）。
        # 主平台即底模来源，无需 fallback（自身就是回退目标）。
        self.platforms["dingtalk"].llm_agent = LLMAgent(
            config=self.config.llm,
            client=self.llm_client,
            tool_router=self.tool_router,
            user_name=self.current_user_name,
            user_dept=self.current_user_dept,
            org_name=self.current_user_org,
            user_title=self.current_user_title,
            store=self.store,
            skill_manager=skill_manager,
            skills_config=self.config.skills,
            platform_id="dingtalk",
            # few-shot 按平台隔离：主平台读自身 DB 中的样例（与画像同库）
            few_shot_examples=self.store._few_shot_repo.get_few_shot_examples(),
        )
        # H2-A：主平台(dingtalk)也在首次装配时接线一个后台异步摘要调度器。
        dingtalk_scheduler = SummaryScheduler(agent=self.platforms["dingtalk"].llm_agent, store=self.store)
        self.platforms["dingtalk"].llm_agent._summary_scheduler = dingtalk_scheduler
        dingtalk_scheduler.start()
        self.platforms["dingtalk"].summary_scheduler = dingtalk_scheduler
        logger.info("[H2-A] 主平台 dingtalk 后台异步摘要调度器已启动")

        # 启动期：从主人历史消息抽取沟通风格画像（Feature B，best-effort 非阻塞）
        self._refresh_style_profile()
    def _should_handoff_low_confidence(self, message, reply_text) -> bool:
        """判断是否应把本条回复转人工（不自动硬答）。

        触发：单聊 + 配置开启 + RAG 有【弱命中】（best_score 非 None 且低于阈值）。
        弱命中代表"检索到可能相关但不可靠"，交主人审签草稿，避免答错砸信誉。
        未命中（confidence=None，通常走 web/闲聊）不触发，避免误拦截天气等正常查询。
        """
        try:
            adv = self.config.llm.advanced
            if not getattr(adv, "low_confidence_handoff_enabled", False):
                return False
            if message.chat_type == "group":
                return False
            if getattr(reply_text, "already_sent", False):
                return False
            conf = getattr(reply_text, "confidence", None)
            if conf is None:
                return False
            threshold = getattr(adv, "low_confidence_threshold", 0.5)
            return conf < threshold
        except Exception:
            logger.warning("[resilience] silent exception in _should_handoff_low_confidence", exc_info=True)
            return False
    def _notify_owner_draft(self, message, reply_text) -> None:
        """把 AI 草稿推送主人审签（优先落库，再推 DM 通知）。

        流程：草稿先写入 SQLite（message_drafts 表）→ 再用 DWS 发简洁通知给主人。
        若 DM 推送失败，不影响草稿已落库，仅记录 warning。
        """
        try:
            platform = _active_platform_ctx.get()
            question = (message.content or "").strip()
            answer = (reply_text.text or "").strip()
            conf = getattr(reply_text, "confidence", None)
            threshold = getattr(self.config.llm.advanced, "low_confidence_threshold", 0.5)
            best_chunk = getattr(reply_text, "best_chunk", None)

            # 1) 先落库草稿
            draft_id = self.store._draft_repo.add_draft(
                platform=platform,
                chat_id=message.chat_id,
                chat_name=message.chat_name or "",
                chat_type=message.chat_type or "single",
                sender_id=message.sender_id or "",
                sender_name=message.sender_name or "",
                user_message=question,
                ai_reply=answer,
                rag_confidence=conf,
                rag_threshold=threshold,
                rag_best_chunk=best_chunk,
            )

            # 2) 推 DM 简洁通知给主人
            try:
                oid = getattr(self, "_owner_open_dingtalk_id", None)
                if not oid:
                    info = self.dws.contact_user_get_self()
                    oid = info.get("openDingTalkId") or info.get("open_id") or ""
                    if oid:
                        self._owner_open_dingtalk_id = oid
                if not oid:
                    logger.warning("[转人工] 取不到主人 openDingTalkId，跳过 DM 通知（草稿已落库 draft_id=%s）", draft_id)
                else:
                    conf_pct = f"{conf:.0%}" if conf is not None else "N/A"
                    notify_msg = (
                        f"【需要你确认】收到一条低置信消息（RAG相关度 {conf_pct}）\n"
                        f"来自：{message.sender_name or '未知'}（{message.chat_name or message.chat_id}）\n"
                        f"问：「{question}」\n"
                        f"拟回复：「{answer}」\n\n"
                        "请在 Web 管理台「草稿审阅」中审批或修改后发送。"
                    )
                    self.dws.chat_message_send(open_dingtalk_id=oid, text=notify_msg)
                    logger.info("[转人工] 已推送草稿通知 draft_id=%s 给主人", draft_id)
            except Exception as e:
                logger.warning("[转人工] DM 推送失败（草稿已落库 draft_id=%s）: %s", draft_id, e)
        except Exception as e:
            logger.warning("[转人工] 草稿落库失败: %s", e)
        finally:
            # 无论推送成败 / 落库成败，都标记原消息已处理
            try:
                msg_key = message.msg_id or (message.raw.get("alt_id") if isinstance(message.raw, dict) else "")
                if msg_key:
                    self.store._conversation_repo.update_last_replied_msg_id(message.chat_id, msg_key)
                    self.poller._mark_msg_processed(msg_key, message.chat_id)
            except Exception as de:
                logger.warning("[转人工] 标记已处理失败: %s", de)
    def _load_current_user(self) -> dict:
        try:
            # 优先本地 profile（零网络，不弹窗）
            profile = self.dws._get_current_profile_local()
            base = {}
            if profile and profile.get("userId"):
                logger.info("从本地 profile 读取当前用户: %s@%s",
                            profile.get("userName", ""),
                            profile.get("corpName", ""))
                base = {
                    "userId": profile.get("userId", ""),
                    "userName": profile.get("userName", ""),
                    "orgName": profile.get("corpName", ""),
                    "dept": "",
                    "title": "",
                }

            # 补拉企业通讯录拿真实部门（profile 文件不带 dept 字段）
            # 仅在 profile 缺 userId 时整体依赖 API；否则 API 失败不影响 profile 已得信息
            try:
                info = self.dws.contact_user_get_self(timeout=30)
                emp = info.get("orgEmployeeModel", {})
                if emp:
                    dept = (emp.get("depts", [{}])[0] if emp.get("depts") else {}).get("deptName", "")
                    # 抽取职位/岗位字段（钉钉 orgEmployeeModel 字段名不固定，多候选容错）
                    title = (emp.get("title") or emp.get("position")
                             or emp.get("jobTitle") or emp.get("jobName") or "")
                    if not base:
                        return {
                            "userId": emp.get("userId", ""),
                            "userName": emp.get("orgUserName", ""),
                            "orgName": emp.get("orgName", ""),
                            "dept": dept,
                            "title": title,
                        }
                    if not base.get("dept"):
                        base["dept"] = dept
                    if not base.get("title"):
                        base["title"] = title
            except Exception as api_err:
                err_str = str(api_err)
                if "TOKEN_VERIFIED_FAILED" in err_str or "该组织尚未开启 CLI 数据访问权限" in err_str:
                    logger.warning("个人钉钉模式：无法获取企业用户信息，跳过部门补全")
                else:
                    logger.debug("补拉用户部门失败（使用 profile 兜底）: %s", api_err)

            return base or {"userId": "", "userName": "个人用户", "orgName": "", "dept": "", "title": ""}
        except Exception as e:
            err_str = str(e)
            if "TOKEN_VERIFIED_FAILED" in err_str or "该组织尚未开启 CLI 数据访问权限" in err_str:
                logger.warning("个人钉钉模式：无法获取企业用户信息，跳过用户详情加载")
            else:
                logger.error("无法获取当前用户: %s", e)
            return {"userId": "", "userName": "个人用户", "orgName": "", "dept": "", "title": ""}
    def _resolve_own_open_dingtalk_id(self) -> str | None:
        """通过拉取最近消息，从自己发的消息中提取 openDingTalkId。

        contact_user_get_self 不返回 openDingTalkId，只能从消息记录中反推。
        每次 CLI 调用限时 10s，总超时 60s，超时后返回 None 不阻塞启动。
        """
        def _do_resolve():
            try:
                import datetime
                data = self.dws.chat_message_list_all(
                    start=(datetime.datetime.now() - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
                    end=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    limit=50,
                    timeout=10,
                    max_pages=20,
                )
                # _get_result 会解包一层 result
                raw = data.get("conversationMessagesList", []) if "conversationMessagesList" in data else data
                if isinstance(raw, list):
                    conv_list = raw
                else:
                    conv_list = []
                logger.debug("[初始化] 拉取到 %d 个会话用于提取 openDingTalkId", len(conv_list))
                # 遍历所有单聊会话的所有消息，找自己发的
                for conv in conv_list:
                    for msg in conv.get("messages", []):
                        sender = msg.get("sender", "")
                        sid = msg.get("senderId") or msg.get("senderOpenDingTalkId") or ""
                        # 优先用唯一 ID 匹配（避免组织内重名时误取他人 openDingTalkId）：
                        # 消息的 senderId 即当前登录用户的 userId；openDingTalkId 直接相等。
                        if sid and (sid == self.current_user_id or sid == self.current_open_dingtalk_id):
                            oid = msg.get("senderOpenDingTalkId", "")
                            if oid:
                                logger.info("[初始化] 从消息中提取到自己的 openDingTalkId: %s", mask_oid(oid))
                                return oid
                        # 兜底：仅当无法用 ID 判定时，才用姓名匹配
                        if sender and sender == self.current_user_name:
                            oid = msg.get("senderOpenDingTalkId", "")
                            if oid:
                                logger.info("[初始化] 从消息中提取到自己的 openDingTalkId(姓名兜底): %s", mask_oid(oid))
                                return oid
                logger.warning("[初始化] 无法从消息中提取 openDingTalkId（遍历了 %d 个会话），自我检测可能不准确", len(conv_list))
                return ""
            except Exception as e:
                logger.warning("[初始化] 提取 openDingTalkId 失败: %s", e)
                return None

        # 【P0-2026-08-08】同 primary.py：用共享原语 run_with_timeout 做超时保护，
        # 显式 executor + shutdown(wait=False, cancel_futures=True)，超时/异常后
        # 立即返回，不阻塞启动（避免 `with` 块里 shutdown(wait=True) 让超时保护失效）。
        result, timed_out, raised = run_with_timeout(
            _do_resolve, timeout=OPEN_DINGTALK_ID_RESOLVE_TIMEOUT, thread_name="resolve-oid")
        if timed_out or raised:
            logger.warning(
                "[初始化] _resolve_own_open_dingtalk_id 超时/异常，降级跳过，"
                "自我检测可能不准确")
            return None
        return result
    def _filter_sensitive_words(self, text: str) -> str | None:
        words = self.config.safety.sensitive_words
        if not words:
            return text
        for w in words:
            if w and w in text:
                logger.warning("检测到敏感词: %s", w)
                return None
        return text
