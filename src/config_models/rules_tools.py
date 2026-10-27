"""规则、工具与 OCR 后处理相关配置模型。

P2-7 第 2 步（2026-10-27）自 ``src/config_models/__init__.py`` 外移。
本组**无外部配置类依赖**（AST 实测），故注解解析行为与外移前一致。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
        "get_calendar_events", "create_todo", "list_todos", "complete_todo",
        "schedule_meeting",
        "recall_memory", "save_memory",
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
        "approval_get_form_schema",
        "approval_list_pending", "approval_list_tasks", "approval_list_initiated",
        "approval_list_executed", "approval_list_org", "approval_list_cc",
        "list_reports", "get_report_detail", "list_report_templates",
        "search_enterprise_content", "search_enterprise_behavior"
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
    # 工具执行的统一外层超时（秒）：防止单个工具卡死（无限循环 / 无超时的网络调用 /
    # 子进程挂起）永久阻塞 ToolRouter 共享线程（轮询主线程或 Web worker），进而拖垮
    # 整条消息处理链路。0/负数表示不启用外层护栏（退回各工具自带超时，如 SkillTool 的
    # 60s 子进程超时）。实现上在 daemon 子线程中执行并用 join(timeout) 等待；超时仅停止
    # 等待、不真正中断同步调用，故阈值应远大于正常耗时（默认 120s，留出子进程超时余量）。
    # 用 contextvars 把调用方平台隔离上下文带入子线程，避免超时执行丢失平台上下文。
    max_tool_seconds: float = 120.0
