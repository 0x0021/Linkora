"""配置数据模型（Pydantic）——拆分自 config.py。

config.py 保留加载/校验/多平台 seed 逻辑，本模块只含：
- 默认路径常量
- 全部配置模型（AppConfig 等 26 个）
- 模型解析/重建辅助（_resolve_model_cls / _ensure_models_rebuilt / _ConfigState）
"""
from __future__ import annotations

import logging
import threading
import typing
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from src.paths import data_path, get_data_dir, get_log_dir

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = str(get_data_dir())
DEFAULT_STORAGE_PATH = str(data_path("linkora.db"))
DEFAULT_TMP_IMAGES_DIR = str(data_path("tmp_images"))
DEFAULT_BACKUP_DIR = str(data_path("backups"))


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


class PollerConfig(BaseModel):
    interval_seconds: int = 5
    unread_conversation_count: int = 20
    messages_per_conversation: int = 20
    history_window: int = 20
    history_days: int = 3  # 历史消息取最近 N 天；与 config.yaml 对齐，避免环境未加载 yaml 时掉回 1 天导致跨天失忆
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
    # 长尾会话（非未读）按会话限频抓取的最小间隔（秒）：避免每个置顶/db缓存会话
    # 每轮(默认5s)都打一次 chat_message_list。未读会话不受影响（实时优先）。
    # list-all 主通道仍每轮保底抓取全部新消息，故不会漏消息。0 = 关闭限频。
    min_conversation_poll_interval_seconds: int = 60


class KeywordRule(BaseModel):
    match: str
    reply: str


class RulesConfig(BaseModel):
    enabled: bool = True
    # ReDoS 防护：用户/DB 可配置的正则(黑白名单、关键词规则)匹配单次超时秒数。
    # 使用 regex 库的 timeout 机制中断灾难性回溯，超时则跳过该规则(fail-safe)。
    # 0 或负数表示禁用超时防护(不推荐)。
    regex_timeout_seconds: float = 1.0
    blacklist: dict[str, list[str]] = Field(default_factory=lambda: {"users": [], "groups": []})
    whitelist: dict[str, Any] = Field(default_factory=lambda: {"enabled": False, "users": [], "groups": []})
    keywords: list[KeywordRule] = Field(default_factory=list)
    # 停用词表：用于模糊匹配时过滤无意义的 token，防止误匹配
    # 支持逗号分隔的字符串列表（每行一组词，可加注释分类）
    stop_words: list[str] = Field(default_factory=list)
    # 高频关键词黑名单：从「高频关键词」词云中强制剔除的词（如 dingtalkclient / mobilelink 等机器生成 token）。
    # 可在此免改代码添加，是抑制无意义词的长效机制。
    keyword_denylist: list[str] = Field(default_factory=lambda: [
        "dingtalkclient", "mobilelink", "dingtalk", "mobile",
        "client", "link", "oauth", "callback", "redirect",
        "android", "ios", "robot", "webbot", "miniapp",
    ])

    # 意图过滤配置：识别并跳过无业务价值的消息
    intent_filter: dict[str, Any] = Field(default_factory=lambda: {
        "enabled": True,
        "thank_you": [
            "谢谢", "感谢", "辛苦了", "辛苦", "谢了", "多谢", "太感谢了",
            "谢谢老板", "谢谢领导", "谢谢经理", "辛苦老板", "辛苦领导",
            "谢老板", "谢领导", "感恩", "感激", "谢谢了", "谢谢啊",
            "谢谢啦", "多谢了", "辛苦了啊", "辛苦了啦",
        ],
        "acknowledge": [
            "收到", "好的", "明白", "知道了", "了解", "清楚了",
            "OK", "ok", "Ok", "好", "行", "没问题", "可以",
            "好滴", "好嘞", "好哒", "好的好的", "明白了", "知道了",
            "清楚了", "了解了", "收到收到", "收到啦", "收到了",
        ],
        "closing": [
            "再见", "拜拜", "晚安", "拜拜了", "下次聊", "下次再说",
            "先这样", "就这样", "结束吧", "收工", "下班了",
            "先忙了", "先忙啦", "忙了", "先走了", "先撤了",
        ],
        "polite": [
            "你好", "您好", "嗨", "Hi", "hi", "Hello", "hello",
            "在吗", "在不在", "有人吗", "请问", "打扰了", "不好意思",
            "抱歉", "抱歉打扰", "打扰一下", "请问一下", "方便吗",
        ],
        # 以下三个社交子型为后续扩充：默认仅给少量种子词，运维可在 config.yaml
        # 经 rules.intent_filter.<key> 追加关键词（去重合并语义）。
        "compliment": [
            "厉害", "太强了", "太棒了", "牛啊", "可以啊", "可以呀",
            "优秀", "靠谱", "给力", "绝了", "666", "yyds",
        ],
        "smalltalk": [
            "在忙吗", "忙不忙", "最近好吗", "最近怎么样", "吃饭了吗",
            "吃了吗", "周末有空吗", "还好吗",
        ],
        "emotion": [
            "哈哈", "哈哈哈", "嘿嘿", "无语", "醉了", "服了", "emo", "笑死",
        ],
        # 混合消息检测：如果消息中同时包含业务内容和感谢，阈值用于判断是否跳过
        # 纯感谢消息的长度阈值（超过此长度可能包含业务内容）
        "pure_thank_max_length": 20,
        # 纯确认收到消息的最大长度（超过此长度大概率含业务内容，不应跳过）
        "pure_ack_max_length": 10,
        # 消息中业务关键词占比阈值（低于此阈值则认为主要是感谢）
        "business_ratio_threshold": 0.3,
        # 业务关键词：用于判断消息是否包含业务内容
        "business_keywords": [
            "问题", "错误", "故障", "报错", "异常", "失败", "不行",
            "怎么", "如何", "为什么", "什么", "哪里", "谁", "多少",
            "帮我", "麻烦", "需要", "想要", "请求", "申请",
            "配置", "设置", "安装", "部署", "调试", "排查", "解决",
            "查询", "搜索", "查看", "确认", "核实", "验证",
            "审批", "流程", "工单", "订单", "任务", "项目",
            "账号", "密码", "权限", "登录", "注册", "开通",
            "链接", "地址", "网址", "入口", "页面", "功能",
            "数据", "报表", "统计", "分析", "报告",
            "时间", "日期", "期限", "截止", "过期",
            "金额", "费用", "价格", "预算", "成本",
            "邮件", "通知", "提醒", "公告", "消息",
            "会议", "日程", "安排", "预约", "计划",
            "文档", "资料", "文件", "附件", "下载", "上传",
            "接口", "API", "服务", "服务器", "系统", "平台",
            "股票", "行情", "上市", "发行", "交易", "IPO", "股价",
            "情况", "总结", "说明", "介绍", "对比", "推荐",
        ],
    })


class StorageConfig(BaseModel):
    type: str = "sqlite"
    path: str = DEFAULT_STORAGE_PATH
    backup_enabled: bool = True
    backup_dir: str = DEFAULT_BACKUP_DIR
    backup_interval_hours: int = 24
    backup_max_count: int = 7
    backup_on_start: bool = True
    decisions_retention_days: int = 30  # 决策追踪表留存天数（0/负数视为不清理）
    messages_retention_days: int = 90  # 消息记录留存天数（0/负数视为不清理）
    doc_sync_interval_hours: int = 1  # 钉钉文档自动同步间隔（小时），0/负数禁用


class PlatformRagConfig(BaseModel):
    """平台级 KB（RAG）覆盖配置。所有字段可选，None 表示沿用全局 rag 值。

    典型场景：飞书文档较长需要更大的 chunk_size，企微消息短可缩小分块。
    """

    chunk_size: int | None = None
    chunk_overlap: int | None = None
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


class OcrPostprocessConfig(BaseModel):
    """OCR 后处理管线配置。

    每个步骤可独立开关，控制 OCR 文本清洗的粒度。
    在 poller → OCR → message_loop 链路中，OCR 结果投喂 LLM 前执行。
    """

    enabled: bool = True  # 总开关
    min_chars: int = 5  # 有效字符数阈值，低于此值跳过不投喂 LLM
    enabled_steps: dict[str, bool] = Field(default_factory=lambda: {
        "remove_invisible": True,   # 零宽/不可见控制字符剔除
        "dedup_punctuation": True,  # 连续重复标点压缩（>2 → 1）
        "remove_fillers": True,     # 口语填充词/语气词剔除
        "normalize_layout": True,   # 合并空行、去除首尾空白
        "cjk_spacing": False,       # 中文/英文/数字间加空格（默认关，避免改变原文排版）
    })


class LoggingConfig(BaseModel):
    level: str = "info"
    file: str = str(get_log_dir() / "linkora.log")
    max_size_mb: int = 50
    max_backups: int = 7


class LlmAdvancedConfig(BaseModel):
    max_chars_daily_chat: int = 50  # 日常闲聊最大字数
    max_chars_tech_issue: int = 100  # 技术问题最大字数
    hard_truncation_chars: int = 300  # 硬性截断字符数（安全网，高于软约束上限 max_chars_tech_issue，兜底极端超长）
    # ---------- RAG 自动注入门控 ----------
    # 自动 RAG 注入会把知识库片段塞进 system prompt。若无条件注入，AI 会把弱相关
    # 文档（如办公点 IP 表、配置清单）当成「需复述的事实」附在无关回答后（例：问天气却
    # 复述打印机清单）。三层防护：
    #   1) rag_auto_inject=false 彻底关闭自动注入（最稳，按需由 LLM 主动调 kb_search 工具）
    #   2) rag_intent_only=true 仅当 query 含「文档/知识查询意图」时才注入，闲聊/天气/问候等不注入
    #   3) rag_min_similarity / rag_max_results 收紧召回门槛，避免弱相关内容进 prompt
    rag_auto_inject: bool = True  # 是否自动注入 RAG 相关知识到 system prompt
    rag_intent_only: bool = True  # 仅当 query 含文档/知识查询意图时才注入（闲聊/天气/问候等不注入）
    rag_min_similarity: float = 0.6  # 自动注入的最低相似度门槛（原 0.3 过低，易命中弱相关）
    rag_max_results: int = 1  # 自动注入最多返回几条；越少越省 token，关键信息通常在最相关的前 1 条
    rag_max_content_chars: int = 800  # 单条注入知识的展示上限；过长会挤占历史消息窗口，触发 429
    rag_empty_fallback_enabled: bool = True  # 空-RAG 三级递进兜底总开关：知识库无匹配时，按降阈值重搜→引导追问→强制兜底三级处理
    rag_empty_fallback_reply: str = "知识库中暂未收录相关信息。"  # 第3级兜底回复文案
    # ---------- 三级递进 RAG 空结果处理 ----------
    rag_fallback_min_similarity: float = 0.20  # 第1级降级重搜的最低相似度阈值
    rag_fallback_max_results: int = 2  # 第1级降级重搜的最大结果数（不搜太多）
    rag_max_retry_rounds: int = 1  # 最多允许几轮引导追问（超过则强制走第3级兜底）
    # ---------- Prompt 长度控制 ----------
    max_input_tokens: int = 12000  # 单条请求最大输入 token（含 system + history + user）；超限则从最早历史开始截断
    # ---------- H5/H6 历史分级注入（注入 LLM 的近期完整条数） ----------
    # 原 _apply_history_tiering 硬编码 6；现经配置暴露，未配置时默认 6 保持向后兼容。
    # 调小（如 4）可直接削减注入 LLM 的近期完整消息条数、省 token；
    # 同时因 history_window >= max_recent 才触发摘要，调小也是激活 H2-A 异步摘要的前提。
    history_tiering_recent: int = 6
    # ---------- H2-A 后台异步摘要（state machine 写回） ----------
    # 总开关：是否启用后台异步摘要（True=主回复链路只快读缓存，摘要计算在 daemon 线程写回）。
    summary_async_enabled: bool = True
    # 缓存摘要新鲜度窗口（秒）：超过则视为过期，降级为 recent 仅并触发补算。
    summary_max_age_seconds: int = 600
    # 缓存摘要采用阈值：缓存摘要须覆盖当前 older 段的 >= 该比例才采用（避免旧摘要漏掉新 older 导致失忆）。
    summary_min_coverage_ratio: float = 0.6
    # 透传给 summarize_conversation 的 max_messages（0=不限制，由调用方裁剪 older）。
    summary_max_messages: int = 0
    # older 段至少 N 条才压摘要（沿用原 len(older) >= 2；不足则仅注入 recent，避免无意义的单条摘要）。
    summary_min_older: int = 2
    # ---------- 低置信度转人工/草稿（Feature A） ----------
    # 当 RAG 未命中（或最佳相似度低于阈值）且消息属"知识/问题类"查询时，
    # 不硬答、不答"未找到"，而是把草稿推送给主人审签（人工接管）。
    low_confidence_handoff_enabled: bool = True
    low_confidence_threshold: float = 0.35  # RAG 最佳相似度低于此值视为低置信（默认放宽以减少草稿审签打扰）
    # ---------- RAG 引文溯源 + 置信度产品化（Phase 2） ----------
    # 在真实回复末尾可选追加「依据：《标题》（相关度88%）」页脚，让回答可溯源。
    # 默认关闭（灰度）；开启后仅在 RAG 命中且相关度达阈值时追加，低置信不追加。
    citation_enabled: bool = False        # 引文页脚总开关，默认关
    citation_in_group: bool = False       # 群聊是否也附引文，默认关（更克制）
    citation_high_threshold: float = 0.75  # ≥此值标「依据」，[low,high) 标「参考」
    citation_low_threshold: float = 0.50  # 低于此值不追加页脚
    citation_max_items: int = 2           # 页脚最多列几条引文
    # 引文页脚是否展示内部相关性分数（相关度XX%）。属实现细节，默认隐藏
    # 避免把系统指标（相似度）泄露给最终用户；溯源仍靠《文档名》呈现。
    citation_show_score: bool = False
    # 是否剥离 LLM 自行生成的「—— 依据：《doc》（相关度XX%）」引文（历史自污染回灌）。
    # 默认开：LLM 若从历史坏回复学会输出引文格式，属提示词/推理泄漏，应由
    # reply_helpers 页脚作为唯一真相源；统一剥离避免双引文 + 内部分数泄漏。
    citation_hide_generated: bool = True
    # ---------- BGE 本地离线重排（Phase 2 · P2-6，默认关） ----------
    # 对 RAG 召回候选做 cross-encoder 重排，仅调整顺序（reorder-only，
    # 不改原相似度 score），提升注入质量；默认关，开启需本地有模型权重。
    rerank_enabled: bool = False          # 重排总开关，默认关
    rerank_model: str = "BAAI/bge-reranker-base"  # 重排模型
    rerank_offline: bool = False          # 纯离线加载（local_files_only）
    rerank_top_k: int = 10                # 参与重排的候选窗口（None/0=全部）
    rerank_timeout: float = 2.0           # 重排超时（秒），超时降级原始顺序


class LlmConfig(BaseModel):
    provider: str = "openai-compatible"
    base_url: str = "https://api.openai.com/v1"
    # 允许为空/None：本地或免密代理可不填；远程服务才需要真实 key。
    # 运行期 LLMClient 已用 `api_key or "dummy"` 兜底，None 不会崩。
    api_key: str | None = None
    model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 512
    timeout: int = 60
    max_tool_rounds: int = 5
    # 工具收敛护栏：当连续 N 轮 LLM 都在调用工具（尤其搜索）却仍未综合作答时，
    # 强制收敛——移除“继续检索类”工具并注入“基于已有结果直接作答”的系统提示，
    # 防止免费/弱模型陷入“换关键词反复搜”循环，直到 max_tool_rounds 耗尽才降级。
    # 0 = 关闭护栏（完全依赖 max_tool_rounds）。默认 3 表示：第 3 轮若仍在调工具则收敛。
    converge_after_tool_rounds: int = 3
    system_prompt: str = (
        "你是 {user_name} 的 {platform} 数字分身，代表 {user_name} 回复消息。"
        "语气、口吻与表达习惯以注入的【主人沟通风格】段落为准，请贴合 {user_name} 本人的真实风格沟通，不要套用任何固定人设。"
    )
    advanced: LlmAdvancedConfig = Field(default_factory=LlmAdvancedConfig)
    # —— 同服务商模型池（免费额度轮换）——
    # 与主模型共用 base_url / api_key 的备选模型列表（不含主模型 model 本身）。
    # 主模型 429/超时/失败时，按列表顺序在同池内逐个轮换（每次调用一次），
    # 仍失败则降级到跨服务商的 fallback_*（见下）。用于把同一服务商的多个免费模型
    # 额度都用起来（例如 mimo-v2-5:free、kimi-k2-7-code:free、hy3:free、deepseek-v4-flash:free）。
    # 顺序即优先级：排在前面的先被尝试。
    model_pool: list[str] = Field(default_factory=list)
    # 备用模型（跨服务商兜底，主模型+模型池全部失败后切换）
    # 单备用模型（旧）：直接填 fallback_model。
    # 备用模型池（新）：fallback_model_pool 内逐个轮换（共用 fallback_base_url/fallback_api_key）。
    # 两者并存时优先用池；池为空则回退到单 fallback_model。
    fallback_model: str = ""
    fallback_model_pool: list[str] = Field(default_factory=list)
    fallback_api_key: str | None = None
    fallback_base_url: str = ""
    # 第二层备用模型（跨服务商兜底，fallback 全部失败后切换）
    secondary_fallback_model: str = ""
    secondary_fallback_model_pool: list[str] = Field(default_factory=list)
    secondary_fallback_api_key: str | None = None
    secondary_fallback_base_url: str = ""
    # few-shot 本人语气样例（用于让数字分路口吻更像本人）
    few_shot_examples: list[dict] = Field(default_factory=list)
    # 主人沟通风格（数字分身口吻）：手动覆盖注入 system prompt；
    # 留空则由 sqlite_store 的 style_profiles 自动画像（启动时从主人历史消息抽取）兜底。
    persona_style_prompt: str = ""
    # 主人沟通风格按平台覆盖：key 为 platform_id（dingtalk/feishu/wecom），
    # value 为该平台专属的口吻覆盖。优先级高于全局 persona_style_prompt。
    # 例：{"feishu": "飞书场景更活泼口语", "wecom": "企业微信场景更正式"}
    # 多平台隔离：各平台独立 DB 已隔离自动画像；此处再支持手动覆盖也按平台区分。
    persona_style_prompts: dict[str, str] = Field(default_factory=dict)
    # 画像历史版本最大保留数：每次写入新版本后滚动删除超量旧版本，保留最新的 N 个。
    persona_history_max_versions: int = 10
    # 动态 few-shot（按场景检索主人历史原话，注入 system prompt，让真实回复更像本人）。
    # 默认关闭：保持历史静态 few_shot_examples 行为（build_system_prompt 取 [:1] 截断样例）。
    # 开启后：每次生产回复基于当前消息做场景相似检索，从主人历史里取最像的 N 条
    # (user→assistant) 配对注入，替代固定样例。算法由 dynamic_few_shot_method 控制：
    #   - "trigram": 纯文本 trigram 相似粗筛（零延迟，语义弱）
    #   - "embedding": 基于 embedding 余弦相似精排（需 query_vec，每条消息已在 agent 处算过）
    #   - "hybrid": trigram 粗筛 + embedding 精排（默认，兼顾召回与语义）
    dynamic_few_shot: bool = False
    dynamic_few_shot_n: int = 4
    dynamic_few_shot_method: str = "hybrid"
    # 回测评委口径：默认严格（字面口吻重合度，口语极简回复易低分）；
    # 开启 loose 后改为评「意图匹配 + 风格类别一致」，容忍措辞差异，
    # 还原度回测更接近真实观感（避免主人极简口语被过度惩罚）。默认关。
    backtest_judge_loose: bool = False
    # 模型单价自定义（USD / 百万 token）。key 为模型标识子串（小写），
    # value = {"input": float, "output": float}。用于覆盖/补充内置价目表
    # （src.llm.history._MODEL_PRICING），让内置表未收录的收费模型、或想调整
    # 单价的模型也能正确估算成本。优先级高于内置表。
    # 例：{"gpt-4o": {"input": 5.0, "output": 15.0}, "my-custom-model": {"input": 1.0, "output": 2.0}}
    model_pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    # 回复硬截断上限覆盖：0=沿用 advanced.hard_truncation_chars(默认300)；
    # >0 时覆盖普通回复的截断上限（用于按主人风格放宽/收紧，避免极简主人被
    # 300 字上限无意义截断，或灌水主人被放过）。结构化卡片仍放宽到 1300。
    brevity_hard_cap: int = 0
    # —— 重试与退避（P0-2）：主模型瞬时故障时的有限重试 ——
    # 可重试故障：429 限流 / 超时 / 5xx 服务端错误 / 连接错误 / 网关错误。
    # 不可重试（直接进 DLQ）：401/403 鉴权失败、400 格式错误等。
    max_retries: int = 3  # 主模型单轮最大重试次数（含首次；耗尽后切备用模型）
    base_backoff: float = 2.0  # 退避基数（秒）：第 n 次等待 base_backoff * 2**n


class LlmThrottleConfig(BaseModel):
    """后台 LLM 任务（对话摘要 / 记忆提取）的限速与空闲降频配置。

    目的：免费 LLM 额度有严格频次限制，后台任务若无节流会在启动/空闲时
    持续轰炸接口（已观察到 deepseek-v4-flash:free 触发 429 rate_limit_exceeded）。
    """
    enabled: bool = True
    # 两次后台 LLM 调用之间的最小间隔（秒）。活跃时以此为准，避免突发 burst。
    background_min_interval_seconds: int = 20
    # 多久无真实消息处理算"空闲"。空闲时后台任务进一步降频。
    idle_threshold_seconds: int = 300
    # 空闲时两次后台 LLM 调用的最小间隔（秒），应明显大于活跃间隔。
    idle_min_interval_seconds: int = 180
    # 主模型触发 429/超时后，后台任务暂停的时长（秒）。保护免费额度。
    rate_limit_backoff_seconds: int = 600
    # 同一会话记忆提取的最小冷却（秒）：避免每条回复都额外调一次 LLM。
    extract_memory_cooldown_seconds: int = 600
    # 本轮新增内容低于此字符数则不提取记忆（纯短句无意义）。
    extract_memory_min_new_chars: int = 30
    # 单个调度周期内最多处理的摘要会话数（安全上限，防止一次性排空）。
    max_summaries_per_cycle: int = 20
    # 摘要时最多取的非归档近期消息条数（控制 prompt token）。
    summary_history_limit: int = 40


class ToolsConfig(BaseModel):
    enabled: bool = True
    # 是否允许技能引擎把「未显式声明 allowed_tools」的技能自动包装为标准 Tool，
    # 使其绕过 tools.available 白名单被 LLM 调用（技能工具名不在 tools.available 中，
    # 故必须特例放行，属有意设计）。设为 False 则强制严格白名单：技能不再被自动包装为
    # Tool（仅保留其 system prompt 注入），运维可借此收紧攻击面。
    allow_skill_tools: bool = True
    # 是否将【全部】已启用工具的 schema 每轮都暴露给 LLM。
    # True（默认）：LLM 始终能看到所有工具，由其自行判断是否调用。
    #   配合现代大上下文窗口，token 成本可忽略，且能消除关键字路由“藏起工具”导致的“工具没用”问题。
    # False：退回到 _filter_tools_by_intent 的精确关键字过滤（仅 BASE/FALLBACK + 命中关键字的工具可见）。
    expose_all_tools: bool = True
    # 工具按需暴露策略（取代 expose_all_tools 的二值开关，粒度更细）：
    #   - "smart"  （默认，推荐）：明确意图用 intent_keywords 精准暴露相关工具
    #     （零额外 LLM 调用、省 token、减少乱调）；关键词无法确定意图时回退全量
    #     让主模型自选，保证不漏工具。不加剧免费模型 429 限频。
    #   - "all"    ：每轮全量暴露所有工具（= 旧 expose_all_tools=True 行为）。
    #   - "keyword"：纯 intent_keywords 过滤，无命中时回退少量 FALLBACK 工具
    #     （= 旧 expose_all_tools=False 行为，语义盲区最大）。
    # 兼容：若 config 显式设了 expose_all_tools 但未设 tool_routing_mode，则按
    # expose_all_tools 映射（True→all / False→keyword）；两者都未设时默认 "smart"。
    tool_routing_mode: str = "smart"
    # Phase 2 语义路由：子串快路径未命中时，用本地 embedding 语义相似度兜底补充相关工具，
    # 覆盖同义/错别字/口语化表达。embedding 不可用时（未启用或 kb_search 未注册）自动降级为纯子串匹配。
    semantic_routing: bool = True
    # 工具语义命中阈值（余弦相似度，0~1），高于此值视为语义相关。
    semantic_tool_threshold: float = 0.42
    # 是否注册 RAG 知识库检索工具 kb_search。设为 False 时 register_builtin_tools
    # 直接跳过注册（连带热重载重建也不再注册），用于完全关闭 RAG 能力。
    # 注意：这是关闭 kb_search 的【唯一】入口——register_builtin_tools 不读
    # tools.available（available 只在向 LLM 暴露 schema 时做白名单过滤，工具仍已注册）。
    # 此字段此前只被 runtime_setup 以 hasattr 探测、却从未在模型中声明，导致用户在
    # config.yaml 写 tools.kb_search_enabled: false 被 pydantic 静默丢弃、开关恒失效。
    kb_search_enabled: bool = True
    available: list[str] = Field(default_factory=lambda: [
        "kb_search", "send_message", "search_doc", "get_doc_content", "search_contact",
        "get_calendar_events", "create_todo", "recall_memory", "save_memory",
        "web_search", "get_weather", "system_status", "message_stats",
        "keyword_rules", "config_manage",
        "get_unread", "get_conversation_info", "search_messages",
        "get_my_profile", "list_orgs", "get_current_org",
        "transfer_approval",
        "get_attendance", "send_ding",
        "upload_image",
        "list_minutes", "get_minutes",
        "wiki_space_list", "wiki_space_search", "wiki_node_list", "wiki_node_search",
        "approval_list_forms", "approval_search_forms", "approval_get_detail",
        "approval_list_pending", "approval_list_tasks", "approval_list_initiated",
        "approval_list_executed"
    ])
    rate_limit: dict[str, dict] = Field(default_factory=lambda: {
        "send_message": {"per_hour": 30},
        "create_todo": {"per_hour": 20},
        "web_search": {"per_hour": 50},
        "get_weather": {"per_hour": 30},
        "get_unread": {"per_hour": 60},
        "get_conversation_info": {"per_hour": 60},
        "search_messages": {"per_hour": 60},
        "get_my_profile": {"per_hour": 30},
        "list_orgs": {"per_hour": 30},
        "get_current_org": {"per_hour": 30},
        # 转交是写操作，限得更紧
        "transfer_approval": {"per_hour": 10},
        "get_attendance": {"per_hour": 20},
        "send_ding": {"per_hour": 20},
        "upload_image": {"per_hour": 30},
    })
    # 禁止 AI 主动联系第三方（默认开启）：在工具编排层硬拦截所有"向当前对话之外
    # 的人/会话"发起的主动外联。
    #   - send_ding：一律拦截（DING 本质是跨会话强提醒，必属外联）。
    #   - send_message：仅允许发往"当前对话"（message.chat_id），任何发往其他会话
    #     /第三方单聊的调用都被拒绝，并返回提示让 LLM 改为口头转述。
    # 这是硬护栏（等价于工具不可用），关闭需显式设 False；开启时系统提示也会同步
    # 告知模型"不要主动联系第三方"，避免浪费一轮工具调用。
    # 注意：不做白名单（用户明确不要联系第三方），因此无例外列表。
    block_outbound_to_third_party: bool = True


class EmbeddingConfig(BaseModel):
    enabled: bool = False
    provider: str = "local"
    base_url: str = ""
    api_key: str = ""
    hf_token: str = ""
    model: str = "BAAI/bge-small-zh-v1.5"
    top_k: int = 5
    # 是否纯离线：True 时禁止联网下载，仅用本地缓存；False 时允许按需下载（带进度）
    offline: bool = False
    # 心跳保活间隔（秒）：模型就绪后周期性 dummy 推理，防止长时间闲置被卸载。
    # 下限 30s（start_heartbeat 内强制），0/负数视为使用默认 300s。
    heartbeat_interval: float = 300.0


class MemoryConfig(BaseModel):
    cleanup: dict = Field(default_factory=lambda: {
        "enabled": True,
        "max_age_days": 90,
        "min_similarity_threshold": 0.3,
        "check_interval_days": 7,
    })
    retrieval: dict = Field(default_factory=lambda: {
        "min_similarity": 0.6,
    })
    conversation_summary: dict = Field(default_factory=lambda: {
        "enabled": True,
        "max_messages_per_conversation": 50,
        "summary_interval_hours": 24,
        "summary_ratio": 0.4,
    })
    # F18 RAG 检索规模化：向量索引类型与幽灵向量自动清理。
    # vector_index_type: "flat"（默认，IndexFlatIP 精确 O(N) 暴力检索）
    #   或 "hnsw"（IndexHNSWFlat 近似检索，规模增长时提速显著）。
    #   默认 flat 保证存量部署行为零变化；记忆规模增长后显式切 hnsw 即可。
    vector_index_type: str = "flat"
    # HNSW 的 efConstruction / efSearch（越大召回越高、构建与查询越慢）。仅 hnsw 生效。
    vector_index_hnsw_ef: int = 64
    # 幽灵向量（remove 后底层 faiss 向量未删）占有效向量的比例超过此阈值时，
    # 索引在下次 add 时自动用内存缓存重建、回收底层空间。0 表示禁用自动重建
    # （始终由调用方显式 rebuild）。默认 0.3（沿用历史 MAX_FAILED_RATIO 语义）。
    vector_phantom_rebuild_ratio: float = 0.3
    # 是否在索引内缓存归一化后的 embedding，支撑自动重建与精确重建。
    # 开启会增加内存（≈ dim×4×N 字节）。关闭后 maybe_rebuild 退化为无操作。
    vector_cache_embeddings: bool = True



class SkillsConfig(BaseModel):
    """技能引擎配置。

    技能文件位于项目根目录下 data/skills/{name}/SKILL.md（兼容 .agents/skills/ 旧路径）。
    每个 SKILL.md 声明 intent_keywords（意图关键词）和 weight（权重 0-1），
    由智能引擎根据用户消息意图自动调度激活。

    - enabled: 是否启用技能引擎
    - auto_activate: 是否允许关键词自动激活技能（关闭后仅手动「用 XX 技能」方式激活）
    - hot_reload: 是否启用热加载（后台轮询 skills 目录变更，无需重启即可加载新 skill）。
      生产默认关闭——避免运行时自动加载 skills 目录中的新文件（供应链/越权执行风险）；
      开发态可显式设为 true。
    - hot_reload_interval: 热加载轮询间隔（秒），越小越灵敏但耗 CPU
    """
    enabled: bool = True
    auto_activate: bool = True
    # Phase 2 语义路由：技能评分由纯关键词（hits×weight）升级为
    # max(关键词命中, 语义相似度×weight)，覆盖口语/同义改写。embedding 不可用时自动回退纯关键词。
    semantic_routing: bool = True
    # 技能语义命中阈值（余弦相似度，0~1）。
    semantic_skill_threshold: float = 0.40
    # Phase 3 组合激活：允许 score 接近平局（top1 - top2 <= combo_gap）的多个
    # composable 技能被同时激活，使复合意图能编排多技能。仅当技能显式声明
    # composable: true 时生效；关闭 combo_enabled 则退回纯单激活（行为同 Phase 2）。
    combo_enabled: bool = True
    combo_gap: float = 0.12
    hot_reload: bool = False  # 生产安全默认：关闭运行时自动加载 skills 目录新文件；开发态显式开启
    hot_reload_interval: float = 15.0
    # 模块级热重载（开发态）：监控 src/tools/、src/llm/style.py 等无状态模块的 .py 变更，
    # 自动 importlib.reload() + 重建工具注册表。生产必须关闭（有 importlib 副作用风险）。
    module_hot_reload: bool = False
    module_hot_reload_interval: float = 5.0
    # AI 意图词生成：启用后可通过接口让 LLM 分析每个 SKILL.md 自动生成意图词。
    # 默认关闭——避免意外消耗 LLM 额度；需用时显式开启或走手动触发接口。
    ai_intent_generation_enabled: bool = False


class SkillHubConfig(BaseModel):
    """SkillHub 市场（skill 榜单）相关配置。

    - auto_install: 是否允许 Web 运行时自动拉取并执行 skillhub 安装脚本。
      **默认关闭**。开启后仅当 skillhub CLI 未安装时，才会下载白名单内的安装脚本
      并做 SHA256 钉值校验后以非 shell 方式执行（见 web/dependencies._ensure_skillhub_cli）。
      普通 API 请求默认绝不触发远端代码拉取；生产建议由 Dockerfile/部署脚本预装 CLI。
    """

    auto_install: bool = False


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



class RagConfig(BaseModel):
    chunk_size: int = 500  # 分块大小（字符数）
    chunk_overlap: int = 50  # 分块重叠大小（字符数）


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

    @model_validator(mode="after")
    def _enforce_non_empty_auth_password(self) -> "WebConfig":
        """fail-closed：开启认证但密码为空时拒绝构造（启动即退出）。

        空密码下 `_auth_check` 用 hmac.compare_digest("", "") == True，任意请求
        可用空密码登入，等同裸奔。故 auth_enabled=True 且密码为空属不安全配置，
        在 WebConfig 构造期即抛出，使进程无法以该配置启动。
        auth_enabled=False（信任内网/反代场景）时空密码允许。
        """
        if self.auth_enabled and not (self.auth_password or "").strip():
            raise ValueError(
                "auth_enabled=True 但 auth_password 为空，拒绝启动（安全默认）："
                "请在 config.yaml 的 web.auth_password 设置非空密码"
            )
        return self


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


class AppConfig(BaseModel):
    dws: DwsConfig = Field(default_factory=DwsConfig)
    platforms: list[PlatformConfig] = Field(default_factory=list)  # 多平台隔离；空 → 自动 seed 出 dingtalk
    poller: PollerConfig = Field(default_factory=PollerConfig)
    oa_approval: OaApprovalConfig = Field(default_factory=OaApprovalConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ocr_postprocess: OcrPostprocessConfig = Field(default_factory=OcrPostprocessConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    dead_letter: DeadLetterConfig = Field(default_factory=DeadLetterConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    skillhub: SkillHubConfig = Field(default_factory=SkillHubConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    llm_throttle: LlmThrottleConfig = Field(default_factory=LlmThrottleConfig)


def _resolve_model_cls(annotation: Any) -> type[BaseModel] | None:
    """从字段注解里取出嵌套的 pydantic 模型类（兼容 Optional[Model] / Model）。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


class _ConfigState:
    """配置状态（替代 global 变量）。"""

    def __init__(self) -> None:
        self._last_validated_sig: str | None = None
        self._models_rebuilt = False
        self._models_rebuild_lock = threading.Lock()

    @property
    def last_validated_sig(self) -> str | None:
        return self._last_validated_sig

    @last_validated_sig.setter
    def last_validated_sig(self, value: str) -> None:
        self._last_validated_sig = value

    @property
    def models_rebuilt(self) -> bool:
        return self._models_rebuilt

    @models_rebuilt.setter
    def models_rebuilt(self, value: bool) -> None:
        self._models_rebuilt = value

    @property
    def models_rebuild_lock(self) -> threading.Lock:
        return self._models_rebuild_lock


_config_state = _ConfigState()


_MODELS_REBUILT = False
_MODELS_REBUILD_LOCK = threading.Lock()


def _ensure_models_rebuilt() -> None:
    """PEP 563 延迟解析：Pydantic v2 需在首次使用前 rebuild 以解析前向引用。

    不能在模块顶层调用 model_rebuild()（Python 3.14 + annotationlib 会因
    ForwardRef 创建时机导致命名空间不完整），故延迟到 load_config 首调用时懒执行。
    使用双重检查锁定避免并发首次调用时重复 rebuild。
    """
    if _config_state.models_rebuilt:
        return
    with _config_state.models_rebuild_lock:
        if _config_state.models_rebuilt:
            return
        PlatformConfig.model_rebuild()
        AppConfig.model_rebuild()
        _config_state.models_rebuilt = True
