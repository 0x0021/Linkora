"""系统级配置模型（日志、安全、死信、主动摘要、摘要回填、Web、OA 审批）。

P2-7 第 3 步（2026-10-27）自 ``src/config_models/__init__.py`` 外移。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.paths import get_log_dir


class LoggingConfig(BaseModel):
    level: str = "info"
    file: str = str(get_log_dir() / "linkora.log")
    max_size_mb: int = 50
    max_backups: int = 7



class SafetyConfig(BaseModel):
    sensitive_words: list[str] = Field(default_factory=list)
    default_fallback: str = "抱歉，我暂时无法回答这个问题。"
    media_fallback_text: str = "请发文字，我暂时无法处理语音/视频消息，谢谢理解～"



class DeadLetterConfig(BaseModel):
    """死信队列（P0-2）：LLM/发消息彻底失败时，将原始消息落库而非静默丢弃。

    触发 = 主模型重试耗尽 且 备用模型也失败（或两者均不可达）。
    落库后管理台可查看并重放（replay），避免消息石沉大海。
    重试次数/退避见 LlmConfig.max_retries / base_backoff。
    """
    enabled: bool = True  # 是否启用 DLQ（关闭则退回旧行为：仅回 fallback 文本）



class ProactiveConfig(BaseModel):
    """主动触达（P4-13）：每日定时把近期对话摘要主动推送给主人。

    默认关闭（enabled=False）。开启后由后台 daemon 线程在每日 hour:minute
    （本地时区）触发一次：汇总最近 lookback_hours 内、至多 max_conversations 个
    有缓存摘要的对话，生成中文摘要经钉钉推送给 owner（1:1）。复用既有对话缓存
    摘要，不额外消耗 LLM。

    发送目标二选一：owner_user_id（钉钉 user_id）或 owner_open_dingtalk_id
    （钉钉 open_id）。两者皆空则调度器不启动（fail-safe）。
    """

    enabled: bool = False
    hour: int = 9  # 每日触发小时（0-23，本地时区）
    minute: int = 0  # 每日触发分钟（0-59）
    owner_user_id: str = ""  # 钉钉 user_id（与 owner_open_dingtalk_id 二选一）
    owner_open_dingtalk_id: str = ""  # 钉钉 open_dingtalk_id
    lookback_hours: int = 24  # 回溯窗口（小时），仅纳入窗口内有更新的对话
    max_conversations: int = 10  # 单次摘要最多纳入的对话数
    max_summary_chars: int = 200  # 单条对话摘要截断长度（字符）



class SummaryBackfillConfig(BaseModel):
    """摘要连续性补跑（P4-14）：启动检测停机时长，按自然日补齐遗漏窗口的摘要。

    默认开启。进程中断期间 RollingSummary / ProactiveDigest 不会写缓存也不会补跑
    错过的那一天；本机制在启动时读 ``meta.last_run_at``（主库），与「现在」算出停机天数，
    把遗漏窗口切成若干自然日，对每天有消息的会话现场调 LLM 生成当日摘要，写回
    ``conversation_summaries``（与正常周期同源同格式），使「当天 / 近七天」摘要连续完整。
    与 ProactiveDigest 共用同一份缓存，故补跑后主动推送的近七天视图自然补全。

    失败全部非致命：DB/LLM 异常仅记日志，绝不拖垮主回复链路；补跑后刷新 last_run_at 防误判。
    """

    enabled: bool = True  # 是否启用启动期摘要补跑（默认开）
    max_backfill_days: int = 14  # 最多补跑最近 N 天（防止极端停机刷爆 LLM）
    min_messages_per_chat: int = 3  # 单会话某日不足此条数则跳过（避免噪声摘要）
    # 摘要生成的时间上限（省 token）：早于「现在 - N 天」的旧自然日不再补生成摘要。
    # 0 表示不启用该上限（按 max_backfill_days 处理全部遗漏窗口）。与 H2-A 滚动摘要
    # 调度器共用同一语义，确保「过去 N 天之前的旧消息」整体不再消耗 LLM 额度。
    max_summary_age_days: int = 7



class WebConfig(BaseModel):
    port: int = 8080
    # 监听地址。安全默认仅本机回环；如需从其他设备访问应经反代并加认证，
    # 或显式置 "0.0.0.0"（不推荐公网直曝）。
    host: str = "127.0.0.1"
    # 安全默认值：开启认证。历史上默认关闭会在公网暴露管理后台，属高危配置。
    # 本地未配置 config.yaml 时也不应裸奔。部署方如需关闭须显式置 false。
    auth_enabled: bool = True
    auth_username: str = "admin"
    # 安全策略：默认密码为空，启动时若 auth_enabled 为 true 且密码为空则强制报错，
    # 防止部署方忘记修改默认密码导致管理后台裸奔。请在 config.yaml 中显式设置。
    auth_password: str = ""
    # JWT 令牌签名密钥。为空时运行时会生成本进程临时随机密钥（重启失效），
    # 仅适合本地开发；生产环境务必设为固定高熵值，否则令牌可被伪造/重放风险升高。
    jwt_secret: str = ""

    @model_validator(mode="after")
    def _enforce_non_empty_auth_password(self) -> "WebConfig":
        """fail-closed：开启认证但密码为空或仍为已知默认值时拒绝构造（启动即退出）。

        空密码下 `_auth_check` 用 hmac.compare_digest("", "") == True，任意请求
        可用空密码登入，等同裸奔。同理，已知默认值（如 "please-change-me"）等于
        公开口令，任何人可登入管理后台，故一并拒绝。
        在 WebConfig 构造期即抛出，使进程无法以该不安全配置启动。
        auth_enabled=False（信任内网/反代场景）时空密码允许（但默认值仍拒绝，
        因其代表「未真正配置」）。
        """
        # 已知弱/默认口令：恢复出厂或遗漏配置时会写死这些值，等同公开，必须拒绝。
        # 注意：先 strip() 再判定，空白口令（"   "）与空口令同等视为未配置。
        #
        # 【分级策略】弱口令（admin/password/changeme/…）在任何绑定下都拒绝启动；
        # 而「出厂默认口令」是 v0.5.4 刻意设计的开箱即用默认值（见
        # docs/CHANGELOG.md v0.5.4）——它保证 cp 模板即可登录，不因默认拒绝
        # 造成「没默认密码、登不进去」。但该值公开在 git 跟踪的 example 里，
        # 因此仅在**服务对外暴露**时（host 非回环/非私网）才 fail-closed：
        # 本地/内网使用保留开箱体验，公网暴露则强制先改密码。
        _WEAK_DEFAULT_PASSWORDS = (
            "please-change-me", "changeme", "admin", "password",
            "REPLACE_WITH_YOUR_STRONG_PASSWORD",
        )
        # config.yaml.example 随附的出厂默认口令（公开入库，仅本地/内网放行）
        _FACTORY_DEFAULT_PASSWORD = "Admin@P0sw0rd"
        _pw = (self.auth_password or "").strip()
        if self.auth_enabled:
            if not _pw or _pw in _WEAK_DEFAULT_PASSWORDS:
                raise ValueError(
                    "auth_enabled=True 但 auth_password 为空/空白或为已知弱口令，拒绝启动（安全默认）："
                    "请在 config.yaml 的 web.auth_password 设置高强度密码（建议 PBKDF2 哈希，"
                    "生成：python -c \"from web.auth_middleware import hash_password; "
                    "print(hash_password('你的密码'))\"）"
                ) from None
            if _pw == _FACTORY_DEFAULT_PASSWORD and self._is_publicly_exposed():
                raise ValueError(
                    "web.host 绑定到对外可路由地址（当前 "
                    f"{self.host}）但 web.auth_password 仍为公开在 "
                    "config.yaml.example 中的出厂默认口令，拒绝启动：任何人都能登录管理后台。"
                    "请改为自有强密码（推荐 PBKDF2 哈希：python -c \"from web.auth_middleware "
                    "import hash_password; print(hash_password('你的密码'))\"）；"
                    "若仅本机/内网使用，请把 web.host 保持为 127.0.0.1"
                ) from None
        return self

    def _is_publicly_exposed(self) -> bool:
        """判断 web.host 是否为对外可路由地址（回环/私网/本地名视为本地或内网使用）。

        空 host 由运行期按 127.0.0.1 处理（见 web 启动），等同仅本地。
        """
        host = (self.host or "").strip()
        if not host:
            return False
        # 显式「监听所有网卡」= 对外暴露
        if host in ("0.0.0.0", "::", "*"):  # noqa: S104 — 显式通配绑定
            return True
        # 本地主机名：解析到回环，与 127.0.0.1 等价
        if host.lower() in ("localhost", "localhost.localdomain", "ip6-localhost"):
            return False
        try:
            import ipaddress

            ip = ipaddress.ip_address(host)
        except ValueError:
            # 其余主机名（含 bind.example.com 等）：无法静态判定，一律按暴露处理
            return True
        return not (ip.is_loopback or ip.is_private)



class OaApprovalConfig(BaseModel):
    """OA 审批转发处理策略。

    - 别人在钉钉里转给你的审批（msgType=oa）默认视为「催审批」，
      直接回固定话术、不调 LLM（省 token，也避免啰嗦）。
    - 若消息明显是「针对审批的提问」（含问号/怎么/为什么等标记），则交给 LLM 正常处理。
    """

    enabled: bool = True
    # 催审批固定回复话术：直接告诉对方等待，不调 LLM
    urge_reply_text: str = "请稍候，审批正在处理中，请耐心等待。"
    # 含以下标记的 OA 审批消息视为「提问」而非「催审批」，转交 LLM 处理
    question_markers: list[str] = Field(default_factory=lambda: [
        "?", "？", "怎么", "为什么", "为何", "什么情况", "什么意思",
        "合理吗", "对吗", "对不对", "帮我看", "帮我分析", "分析一下",
        "查一下", "看看", "哪", "如何",
    ])
    # 含以下标记的 OA 审批消息视为「动作指令」（如原审批人离职需转交），
    # 交给 LLM 调用审批工具（transfer_approval 等）处理，而非固定话术
    action_markers: list[str] = Field(default_factory=lambda: [
        "转给", "转交", "转由", "移交", "交接", "离职", "换人",
        "代批", "帮忙批", "改成", "转移给",
    ])
