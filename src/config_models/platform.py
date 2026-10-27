"""平台级配置模型（钉钉、适配器覆盖、轮询、存储、平台级 RAG/LLM/工具覆盖）。

P2-7 第 3 步（2026-10-27）自 ``src/config_models/__init__.py`` 外移。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.config_models._const import (
    DEFAULT_BACKUP_DIR,
    DEFAULT_STORAGE_PATH,
    DEFAULT_TMP_IMAGES_DIR,
)


class DwsConfig(BaseModel):
    cli_path: str = "dws"
    timeout: int = 30
    retries: int = 2
    dry_run: bool = True
    profile: str = ""



class AdapterOverrideConfig(BaseModel):
    """适配器专属覆盖配置，用于 PlatformConfig 中按平台覆盖默认适配器参数。

    钉钉平台对应 DwsConfig 字段；飞书/企微对应各自 CLI 的 cli_path/dry_run 等。
    所有字段均为可选（None 表示沿用全局默认值），仅覆盖显式设置的项。
    """
    cli_path: str | None = None
    timeout: int | None = None
    retries: int | None = None
    dry_run: bool | None = None
    profile: str | None = None
    # 企微平台凭证（仅持久化用）。注意：当前企微适配器经 wecom-cli 扫码登录拉消息，
    # 并不消费这些字段——它们是为「企微自建应用回调模式」预留的配置。UI 写回落地这些字段，
    # 避免此前 _apply_wecom_platform 空壳导致 Web 提交的企微凭证被静默丢弃、重启即丢。
    wecom_corp_id: str | None = None
    wecom_corp_secret: str | None = None
    wecom_agent_id: str | None = None
    wecom_token: str | None = None
    wecom_encoding_aes_key: str | None = None



class PollerConfig(BaseModel):
    interval_seconds: int = 5
    unread_conversation_count: int = 20
    messages_per_conversation: int = 20
    history_window: int = 20
    history_days: int = 3  # 历史消息取最近 N 天；与 config.yaml 对齐，避免环境未加载 yaml 时掉回 1 天导致跨天失忆
    # 轮询侧「新消息」年龄门槛（小时），与 history_days 相互独立。
    # history_days 语义是「取多少天的历史作为上下文/RAG 语料」（7 天合理）；
    # 而这里判定的是「一条消息够不够新、要不要触发 AI 回复」。
    # 两者混用会出事故：去重表一旦漏标（handler 抛错时不标记、服务中断、
    # 历史同步路径异常），list-all 会在 history_days 窗口内把几天前的老消息
    # 当新消息重放，与当前新消息合并成同一批投喂，LLM 把新报障认成旧话题的
    # 收尾（2026-08-31 事故：6 天前的旧截图被重放，AI 对当天新故障回
    # 「收到，问题已解决就好。」）。
    # 取 min(history_days*24, 本值) 生效；0 表示不额外收紧（仅用 history_days）。
    # 24h 兼顾「服务离线一夜后仍能补回消息」与「绝不重放隔天老消息」。
    poll_new_message_max_age_hours: int = 24
    # 会话间隔切分：相邻两条历史消息间隔超过此分钟数时，认为是新的对话轮次，
    # 只保留最近一段连续对话，避免把陈年无关旧话题（如两周前的闲聊）带入当前上下文。
    # 0 表示禁用切分（回退到纯 days+window 取数）。默认 360 分钟(6h)：当天连续聊算一段，隔夜/隔天自动断开。
    history_session_gap_minutes: int = 360
    merge_window_seconds: int = 60  # 合并同一人在短时间内的连续消息
    # 高级配置（需要频繁调整的）
    max_processed_msg_ids: int = 500  # 跨轮次去重内存缓存大小（仅 LRU 容量上限；
                                     # 跨进程去重的 TTL 由 sqlite_store.cleanup_processed_msgs 负责）
    list_all_time_window_minutes: int = 30  # list-all 时间窗口（分钟）
    list_all_first_run_minutes: int = 5  # 首次运行时间窗口（分钟）
    # 飞书每轮轮询走白名单模式（只拉已知相关会话 + 单次最近活跃探嗅），
    # 不再全量翻页。此间隔控制「全量扫描」频率：每隔这么久做一次完整 +chat-list
    # 翻页以发现长期未活跃但重新活跃的新会话。0 = 永不自动全量扫描（纯白名单+探嗅）。
    list_all_full_scan_interval_minutes: int = 60
    empty_poll_protection_minutes: int = 5  # 空转保护时间（分钟）
    # list-all 分页硬上限（每页 limit 条，limit 由调用方传，默认 100）：单轮最多拉
    # max_pages * limit 条。原硬编码 20 在"宽时间窗 + 活跃群"下极易触顶导致漏消息；
    # 提到 50 并把上限本身做成可配，便于按实际消息量调。0 = 不限制（谨慎使用）。
    list_all_max_pages: int = 50
    # list-all 实时轮询扫描窗口上限（天）。作用：把"首次运行/增量游标"可能算出的
    # 超宽时间窗（如误配 22 天）钳制到最近 N 天，避免每轮重扫历史全部消息、永远撞
    # 分页上限刷警告。深度历史回填请走 sync_history，不要靠实时轮询循环。
    list_all_max_window_days: int = 14
    # P1-E 背压：防止重启/突发时一次性派发大量消息打爆 LLM/接口
    max_dispatch_per_cycle: int = 30  # 单轮轮询最多派发多少条给 handler（0=不限制，兼容旧行为）
    max_concurrent_replies: int = 4  # 同时进行的回复（LLM 调用）上限（背压）
    reply_concurrency_timeout_seconds: int = 30  # 等待并发槽位超时（秒）；超时降级串行，不丢弃消息
    # list-all 主通道空轮探针：连续 N 轮 list-all 一条新消息都没拉到就告警，
    # 便于直观确认机器人确实在收消息（也提示可能的登录/权限异常）。0 = 关闭探针
    list_all_empty_alert_rounds: int = 6
    skip_msg_types: list[str] = Field(default_factory=lambda: [
        "system", "app", "file", "call", "read_receipt",
        "calendar", "schedule", "voice", "video", "feedCard"
    ])  # 跳过的消息类型（image 不再默认跳过，改由 image_ocr_enabled 控制；read_receipt 为已读回执类通知；calendar/schedule 为日程/会议邀请卡片；voice/video/feedCard 为语音/视频/图文卡片，均无需 AI 回复）
    # 注：oa 已从默认跳过列表移除——别人转来的 OA 审批需由 bot 识别并处理
    #（系统 OA 通知的发送方会被 _is_system_sender 判为 system 类型，仍会被跳过，不会误触）。
    # 历史死项已清理：action_card（类型映射后落值为 app，已由 app 覆盖）、
    # meetting（拼写错误且从未命中，日程会议类已由 calendar/schedule 覆盖）。
    # P2-G 优雅回退类型：bot 无法直接处理的媒体消息，不硬跳过而是送达 main 回复
    # 引导文字（"请发文字"），而非静默忽略。逻辑上 skip 会减去该列表（见
    # poller_core_parse._effective_skip_types / runtime 分发）。
    # 当前 voice/video 已并入 skip_msg_types 彻底静默跳过，故此处留空。
    graceful_fallback_msg_types: list[str] = Field(default_factory=list)
    # 图片消息处理：启用后自动下载截图并 OCR 提取文字，让 LLM 能"看懂"图片内容
    image_ocr_enabled: bool = True
    image_temp_dir: str = DEFAULT_TMP_IMAGES_DIR  # OCR 图片持久化目录（按聊天对象子目录组织）
    # 目标组织（多组织环境下只轮询该组织的会话，避免反复触发跨组织权限验证/弹窗）。
    # 留空 = 自动使用当前登录的 DWS profile 所属组织；否则填 dws profile list 里的 corpId。
    # 跨组织会话会被持久化跳过（加入无效会话黑名单），不再每轮重试。
    target_org_corp_id: str = ""
    # 处理完用户消息后，自动将该会话中该消息及之前的所有消息标记为已读。
    # 避免机器人收到消息后会话列表一直显示未读红点。默认开启；
    # 若需要保留未读状态（例如人工后续跟看），可设为 false。
    mark_read_after_process: bool = True
    # 单条回复的字符上限（F15 超长回复分片）。钉钉/飞书/企业微信的文本与
    # markdown 消息上限约 4096，超限会被平台静默截断或直接拒绝。超过此值的
    # 回复会自动切成多片、带「（i/n）」续发标记顺序发出。默认 4000 留余量；
    # 0 或负数回落到代码内置默认值（不是「不分片」）。
    reply_shard_limit: int = 4000
    # P0 Stream 长连接（v1.0.59+）：启用后以 dws event +listen-im 实时接收消息，
    # 替代 5s 全量轮询，API 调用量降 90%+。默认关闭（保留轮询为主通道，兼容存量行为）。
    stream_enabled: bool = False
    stream_kinds: list[str] = Field(default_factory=lambda: ["all-direct", "all-group"])
    # F14 回复发送退避（防平台高频限流）：
    # 连续两条回复发送之间的最小间隔（秒）。同一轮轮询若连发多条回复，平台
    # （钉钉/飞书/企业微信）会按频率流控，超阈值被静默丢弃。设最小间隔后，两条
    # 回复间隔不足时自动补足 sleep。正常单条回复（与上次发送间隔远大于此值）不增加任何延迟。
    # 0 或负数 = 不限制（向后兼容，完全无额外延迟）。
    reply_send_min_interval: float = 0.2
    # 命中平台限频（IMAdapterRateLimitError / 含 "rate limit"/"429" 等流控信号）后的退避时长
    # （秒）：其间暂停处理本轮剩余回复，避免在已被限流时继续高频触达、加剧限流与失败刷屏。默认 60 秒。
    reply_send_rate_limit_backoff_seconds: float = 60.0
    # === 通知抑制（重新设计：结构优先 + 窄签名）===
    # 旧方案 skip_keywords 用「内容子串」近似「是否为通知」，会误伤真人消息
    # （例：张三会话里的 U9C 报错通知以张三身份推送，结构层面与人类消息一致，无法靠内容区分）。
    # 新方案分层：
    #   1) 结构层（本类之上）：msgType ∈ skip_msg_types、发送者属系统/机器人/应用账号
    #      （_is_system_sender / 系统账号名单）已被静默跳过——绝大多数通知走这里。
    #   2) 窄签名层（本字段）：仅作为「以真人身份推送的纯文本通知」的最后安全网，
    #      匹配固定模板 / 报错栈等【机器生成物】，绝不匹配人类散文。保持锚定、精确。
    #      例：r"^\d{4}-\d{2}-\d{2}\d+发货通知单[：:]" 可精确命中某 ERP 推送。
    #      默认空：无需静默的机器通知请交给 LLM 处理（如 U9C 报错由 AI 提炼要点）。
    #      发现某个纯文本通知想静音时，再加一条窄正则到此列表，切勿用宽泛裸词。
    skip_notification_patterns: list[str] = Field(default_factory=list)
    # 显式静默的发送者 ID（机器人/三方应用账号），结构性静音，强于按名匹配。
    # 与「系统账号名单」互补：名单是按名模糊匹配，本字段是按 senderId 精确匹配。
    skip_notification_sender_ids: list[str] = Field(default_factory=list)
    reply_cooldown_seconds: int = 60  # 同一会话回复冷却时间（秒），防止短时间内反复回复
    # === 真人在场冷却（human-in-the-loop，防 AI 穿插真人对话）===
    # 本会话最近 N 秒内若出现「真人手动发出的消息」（sender_id 命中当前用户且 is_bot=0，
    # 即排除 AI 分身代发的回复），则抑制 AI 自动回复，让真人主导对话。
    # 真人离开超过 N 秒、对方又发来新消息时，AI 自动接管。
    # 0 或负数 = 不启用（沿用旧行为：仅 _has_user_taken_over 的被动接管检测）。
    # 默认 600 秒（10 分钟）：贴合「真人和对方正在互动」的场景，避免机器人反复插嘴。
    owner_present_cooldown_seconds: int = 600
    # 注：旧的 reply_single_only_when_unread（"已读不回复"闸门）已移除，
    # bot 现在对每条新消息都正常回复（见 poller 主流程）。
    # 发送消息时是否携带 AI 标记（dws --ai-tag，在铉铉中显示“由AI发送”标签）。
    # True=携带（默认，合规透明）；False=不携带，消息与本人手发无区别。
    ai_tag_enabled: bool = True
    # === 已读闸门（human-in-the-loop 补充，重接 DWS 已读信号）===
    # 若 DWS 判定本会话“已读(无未读)”，则抑制 AI 自动回复（人工已看/已处理）。
    # 新到的未读消息会让会话重新进入未读列表 → 不抑制（照常回复），以此规避历史
    # 事故“bot 回复后会话移出未读、对方追问又不回填导致漏回(为什么不回复我)”。
    # 仅当所在环境 DWS 未读状态失真、出现漏回时，设 False 关闭本闸门。
    suppress_when_owner_read: bool = True
    # 已读反悔窗口（秒）：AI 回复生成好后、真正发出前，先等这么久，再向 DWS 拉取
    # 一次最新未读状态。若这段时间内你已把会话标记已读，则直接放弃发送——
    # 保证“我已读的会话绝对不再往外发自动回复”。0 = 不等待（仅依赖生成期间的实时复核）。
    # 注意：此窗口会让每条自动回复至少延迟该秒数才发出，按“宁可晚一点、绝不误发”取舍。
    read_grace_seconds: int = 10
    first_run_ignore_older_than_minutes: int = 10  # 首次运行/重启后忽略超过N分钟的老消息（0=不忽略）
    # 会话黑名单（不遍历）容错：群聊权限错误需连续失败达到该次数才加入黑名单，
    # 避免一次瞬时抖动（token 刷新间隙 / CLI 偶发 / 限流被钉钉报成 AUTH_PERMISSION_DENIED）
    # 就把活跃群（如用户有权限的部门群）误杀。0 = 立即拉黑（旧行为，不推荐）。
    blacklist_min_consecutive_failures: int = 3
    # 黑名单自愈对账间隔（轮数）：用 list-top + 直接探测双路判断会话是否已恢复访问。
    blacklist_reconcile_every: int = 10
    # 黑名单自愈每轮直接探测上限（轮转分批，避免黑名单较多时一次性打爆 DWS 接口）
    reconcile_probe_batch_size: int = 5
    # 置顶/最近会话列表缓存 TTL（秒）：会话列表极少变化，无需每轮(默认5s)都打 DWS 接口。
    # 新会话仍能被 unread / list-all / db 缓存层发现，缓存不影响消息收发。
    top_convs_cache_ttl_seconds: int = 120
    # 未读会话列表 TTL 缓存（秒）：未读列表唯一用途是给「实时优先 forced_ids」提供输入，
    # 而新消息由 list-all 主通道每轮全量拉取（快通道即时派发），缓存短暂陈旧不丢消息。
    # 命中缓存期间直接复用上次结果，把每轮 1 次的未读列表调用降到约 1/TTL 轮一次；
    # TTL 内新出现的未读会话仍会被 list-all 发现并派发，只是不享「跳过长尾限频」的
    # 优先权（最长延迟一个长尾限频窗口）。0 = 关闭缓存（每轮都拉，旧行为）。
    unread_convs_cache_ttl_seconds: int = 30
    # 长尾会话（非未读）按会话限频抓取的最小间隔（秒）：避免每个置顶/db缓存会话
    # 每轮(默认5s)都打一次 chat_message_list。未读会话不受影响（实时优先）。
    # list-all 主通道仍每轮保底抓取全部新消息，故不会漏消息。0 = 关闭限频。
    min_conversation_poll_interval_seconds: int = 60
    # 每轮 per-conversation 抓取的并发 worker 数（受控并发）。
    # 串行轮询 102 个会话单轮 ~70s，过长会击穿上方 min_conversation_poll_interval_seconds
    # 窗口（轮询周期 > 限频窗口 → 长尾限频永不生效）。并发化把单轮耗时压到 ~70/N 秒，
    # 让 60s 时间窗口限频自然生效，从源头削减 chat 接口请求量。1 或 0 = 退化为串行（旧行为）。
    poll_concurrency: int = 4
    # 长尾/静默会话（非未读）的轮次级节流：每 N 轮才抓取一次（N=1 → 隔轮抓）。
    # 与时间窗口限频叠加，且**不依赖轮询速度**——无论串行多慢都不会被击穿，
    # 是「长尾限频形同虚设」的根因修复。0 = 关闭（每轮都抓，靠时间窗口限频降级）。
    # 未读/forced 会话不受此限（实时优先），list-all 主通道每轮仍全量拉，外部好友不丢。
    min_conversation_poll_rounds: int = 1
    # 未读（forced）会话抓取下限间隔（秒）：未读会话仍优先于长尾，但不再"每轮必打"。
    # per-conversation 抓取只是健壮性冗余通道（群消息/@我 的保底路径），新消息由
    # list-all 主通道每轮全量拉取并经快通道即时派发；给 forced 抓取一个下限间隔，
    # 把活跃时段"未读会话每轮全量重抓"的风暴压掉一半以上（钉钉 RATE_LIMIT_ERROR
    # 与管理后台调用日志的主要来源），消息本身不丢、不慢（主通道 20s 一轮）。
    # 0 = 关闭下限（未读会话每轮必抓，旧行为）。
    forced_fetch_min_interval_seconds: int = 30
    # 平台级限频退避冷却（秒）：当某平台 chat 接口返回 RATE_LIMIT_ERROR 时，
    # 全局暂停该平台 chat 抓取冷却秒数（不推进轮询游标、不丢消息），避免继续猛打
    # 已限流的接口、加剧限流风暴。冷却到期下一轮自动恢复。0 = 关闭退避（旧行为，不推荐）。
    chat_rate_limit_cooldown_seconds: int = 60
    # 单聊对方信息解析失败的负缓存 TTL（秒）：chat_conversation_info /
    # contact_user_search 对某会话解析失败（外部好友 / 跨组织 / 瞬断 / 搜索无命中）
    # 后，TTL 内不再重复打这两个接口，避免每轮对不可解析会话空转重试。
    # 消息仍由 list-all 主通道投递，对方 openDingTalkId 由消息 sender 字段兜底
    # （见 _process_conv_messages 反写），不影响收发。权限类失败另有
    # _metadata_unavailable 内存集合做本次运行内跳过，此处兜住瞬断/无命中类失败。
    # 0 = 关闭负缓存（失败后下轮即重试，旧行为）。
    conv_info_negative_cache_ttl_seconds: int = 300



class StorageConfig(BaseModel):
    type: str = "sqlite"
    path: str = DEFAULT_STORAGE_PATH
    backup_enabled: bool = True
    backup_dir: str = DEFAULT_BACKUP_DIR
    backup_interval_hours: int = 24
    backup_max_count: int = 5
    backup_on_start: bool = True
    decisions_retention_days: int = 30  # 决策追踪表留存天数（0/负数视为不清理）
    messages_retention_days: int = 90  # 消息记录留存天数（0/负数视为不清理）
    doc_sync_interval_hours: int = 1  # 钉钉文档自动同步间隔（小时），0/负数禁用
    # SQLite busy_timeout（毫秒）。写库撞锁时的等待窗口。
    # 【P0 2026-10-05】此前硬编码 5000，在双进程架构（web + worker 同时写同一批库）
    # 下实测每分钟 20~44 次 "database is locked"，轮询器大量丢会话。
    # 调到 20s 让短时锁竞争「等过去」而不是直接失败。
    # ⚠️ 治标不治本：跨进程锁等待再久也是等待。根治须用
    #    `scripts/run_linkora.py --single-process` 合并为单进程（both 模式）。
    # ⚠️ 注意：对 SQLITE_BUSY_SNAPSHOT（陈旧读快照）**无效**，那是立即失败的，
    #    只能靠丢弃连接重建（store.write_with_retry）恢复。
    busy_timeout_ms: int = 20000



class PlatformRagConfig(BaseModel):
    """平台级 KB（RAG）覆盖配置。所有字段可选，None 表示沿用全局 rag 值。

    典型场景：飞书文档较长需要更大的 chunk_size，企微消息短可缩小分块。
    """

    chunk_size: int | None = None
    chunk_overlap: int | None = None
    chunk_hard_max: int | None = None  # 安全天花板（字符数）；None=派生为 chunk_size*2
    embedding_model: str | None = None



class PlatformLLMConfig(BaseModel):
    """平台级 LLM 覆盖配置。所有字段可选，None 表示沿用全局 llm 值。

    典型场景：飞书/企微使用专属 provider/model/api_key，不配则继承全局顶层 llm。
    """

    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    fallback_model: str | None = None
    fallback_api_key: str | None = None
    fallback_base_url: str | None = None



class PlatformToolsConfig(BaseModel):
    """平台级 Tools 覆盖配置。所有字段可选，None 表示沿用全局 tools 值。

    典型场景：飞书/企微单独开关某些工具（如 search_enabled / file_ops_enabled），
    不配则继承全局顶层 tools。
    """

    search_enabled: bool | None = None
    file_ops_enabled: bool | None = None
    enabled: bool | None = None



class PlatformConfig(BaseModel):
    """多平台隔离：每个 IM 平台一个独立配置（独立适配器 / 独立数据库 / 独立轮询器）。

    向后兼容：若 config.yaml 无 ``platforms`` 段，``load_config`` 会用全局
    ``dws`` / ``storage`` / ``poller`` 自动 seed 出一个 ``dingtalk`` 平台，
    钉钉数据库路径与行为完全不变。
    """

    id: str  # dingtalk / feishu / wecom
    display_name: str = ""
    enabled: bool = True
    adapter_type: Literal["dingtalk", "feishu", "wecom"] = "dingtalk"
    storage: StorageConfig = Field(default_factory=StorageConfig)
    poller: PollerConfig = Field(default_factory=PollerConfig)
    # 适配器专属配置（钉钉→DwsConfig 字段；飞书/企微→cli_path/dry_run 等）
    adapter: AdapterOverrideConfig = Field(default_factory=AdapterOverrideConfig)
    # 平台级 KB 覆盖配置（可选；不配则沿用全局 rag）
    rag: PlatformRagConfig | None = None
    # 平台级 LLM 覆盖配置（可选；不配则沿用全局 llm）
    llm: PlatformLLMConfig | None = None
    # 平台级 Tools 覆盖配置（可选；不配则沿用全局 tools）
    tools: PlatformToolsConfig | None = None

    model_config = {"extra": "forbid"}

    def model_post_init(self, __context: Any) -> None:
        if not self.display_name:
            self.display_name = self.id
        # 未显式配置 path 时，回退到默认库路径（./data/linkora.db）。
        # 显式设置的 path 一律尊重，不再按平台 id 派生 <id>-ai.db（旧命名已统一为 linkora）。
        if not self.storage.path:
            self.storage.path = DEFAULT_STORAGE_PATH
