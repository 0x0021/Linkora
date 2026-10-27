"""LLM 与向量嵌入相关配置模型。

P2-7 第 2 步（2026-10-27）自 ``src/config_models/__init__.py`` 外移。
本组**无外部配置类依赖**（AST 实测），故注解解析行为与外移前一致。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    # ---------- RAG 严格问答模式（智能问答，默认关） ----------
    # 开启后：所有问答请求强制先检索知识库，回答只允许来自检索到的知识库内容，
    # 不使用通用大模型知识；知识库无相关内容时直接返回 rag_strict_no_hit_reply
    #（不调用 LLM、不推理、不编造）。关闭时完全恢复原有问答逻辑。
    # 详见 src/llm/rag_strict.py 与 docs/rag.md「严格问答模式」。
    rag_strict_mode: bool = False  # 严格问答模式总开关
    rag_strict_min_similarity: float = 0.50  # 严格模式召回相似度门槛（覆盖 rag_min_similarity）
    rag_strict_max_results: int = 3  # 严格模式最多注入几条知识片段
    rag_strict_no_hit_reply: str = "知识库中暂未收录相关内容，我无法凭已有信息作答。"  # 未命中时的固定回复
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
    citation_enabled: bool = False        # 引文页脚总开关，默认关（生产 config.yaml 已置 true 开启）
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
    rerank_enabled: bool = False          # 重排总开关，默认关（opt-in 高级特性：开启需 BGE 权重、引入推理开销，有意默认关）
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
    # 退避 jitter：0=固定退避；>0 时在 [base_backoff, base_backoff*2^(n+1)] 内随机取值，
    # 避免多个并发请求在同一时刻重试（惊群效应）。免费 LLM 建议 0.3-0.5。
    backoff_jitter: float = 0.3
    # 429 限频后模型冷却秒数。若服务端返回 Retry-After 头则优先使用。
    rate_limit_cooldown: float = 30.0
    # 超时/瞬时故障重试耗尽后，给该模型的短冷却期（秒），防止下一轮继续砸同一个模型。
    timeout_cooldown: float = 10.0
    # 全局 LLM 并发上限（所有 LLMClient 实例共享）。0=不限制；免费/低并发模型建议 3-5。
    max_concurrency: int = 0



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
    # 本地推理设备：auto / cpu / mps / cuda。
    #   auto（默认）：Apple Silicon→mps，有 CUDA→cuda，否则 cpu。
    #   cpu：强制 CPU 推理。MPS 后端首次分配即常驻约 1GB 驱动显存且
    #        torch.mps.empty_cache() 实测无法回收；bge-small 这类小模型在 CPU 上
    #        单条仅约 20ms，内存吃紧时设 cpu 可省下约 2GB 常驻内存。
    device: str = "auto"
    # 心跳保活间隔（秒）：模型就绪后周期性 dummy 推理，防止长时间闲置被卸载。
    # 下限 30s（start_heartbeat 内强制），0/负数视为使用默认 300s。
    heartbeat_interval: float = 300.0
