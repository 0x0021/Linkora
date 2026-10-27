"""记忆、技能与检索（RAG）相关配置模型。

P2-7 第 2 步（2026-10-27）自 ``src/config_models/__init__.py`` 外移。
本组**无外部配置类依赖**（AST 实测），故注解解析行为与外移前一致。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
        # 动态（信号驱动）摘要：取代原固定 1 小时周期轮询（H2-B RollingSummaryScheduler）
        "dynamic": {
            "enabled": True,
            "check_interval_seconds": 60,   # 轻量评估心跳（仅 DB 查询，不调 LLM）
            "quiet_minutes": 10,            # 会话静默达到该时长且有新内容 → 摘要
            "min_messages": 3,             # 自上次摘要以来的未摘要消息数下限
            "max_messages_per_chat": 100,   # 未摘要消息达上限 → 立即摘要（防无限增长）
            "max_age_hours": 24,           # 距上次摘要超该时长且有新内容 → 刷新
            "scan_days": 7,                # 扫描最近 N 天内有更新的会话
        },
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
    # 摘要化记忆（2026-09-23）：自动提取的事实先入 memory_pending 缓冲，
    # 由「记忆汇总调度器」周期性把同组 pending 事实 + 已有摘要经 LLM 合并为整合摘要。
    # 取代原先「每条事实单独存一行」的逐条记忆模式。
    summary_merge: dict = Field(default_factory=lambda: {
        "enabled": True,
        "interval_minutes": 10,        # 汇总调度器检查周期（分钟）
        "max_facts_per_merge": 50,    # 单组合并的事实上限（防止 prompt 过长）
        "min_facts_to_merge": 1,      # 至少多少条 pending 才触发合并
    })



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



class RagConfig(BaseModel):
    chunk_size: int = 500  # 分块软目标/上限参考（字符数）；语义分块优先在此长度附近断块
    chunk_overlap: int = 50  # 分块重叠大小（字符数）
    chunk_hard_max: int | None = None  # 安全天花板（字符数）；None=派生为 chunk_size*2。
    # 仅拦截病态超长单元（巨型无标点段落/URL/哈希），达到时仍优先语义边界断开。
    # 建议设在 embedding 模型有效字符容量之下，杜绝模型侧截断。
    llm_clean_enabled: bool = True  # 入库文档是否用 LLM 做语义清洗（正则仅作预清洗/回退）
    llm_clean_max_chars: int = 8000  # 单次 LLM 清洗字符上限；超长文档按段落分片，超单段上限的段回退正则
