# 配置参考

所有配置集中在 `config.yaml`，Web 后台修改后自动落盘和热重载。敏感配置（如 API Key）支持环境变量覆盖。

## 配置分组

| 分组 | 关键参数 | 说明 |
|---|---|---|
| **`platforms`** ★ | `id` / `display_name` / `enabled` / `adapter_type` / `storage` / `poller` / `adapter` | **多平台隔离主配置段**：每个 IM 平台独立适配器 / 数据库 / 轮询器；运行期以本段为准。钉钉默认库 `./data/linkora.db`；飞书/企微仅在显式配置 `storage.path` 时使用各自独立库（如 `./data/feishu-ai.db`） |
| `dws` | `cli_path` / `profile` / `retries` / `timeout` / `dry_run` | dws CLI 行为（legacy 兼容段，`config_manage` 读取，与 platforms 中 dingtalk 保持一致） |
| `poller` | `interval_seconds` / `merge_window_seconds` / `reply_cooldown_seconds` / `max_dispatch_per_cycle` / `max_concurrent_replies` / `history_window` / `history_days` / `history_session_gap_minutes` / `target_org_corp_id` / `blacklist_*` / `reconcile_probe_batch_size` / `image_ocr_enabled` / `image_temp_dir` / `list_all_empty_alert_rounds` / `min_conversation_poll_interval_seconds` / `top_convs_cache_ttl_seconds` | 轮询与背压（**运行期以 platforms.dingtalk.poller 为准**，root 级 poller 为 legacy 兼容段） |
| `llm` | `provider` / `model` / `base_url` / `api_key` / `persona_style_prompt` / `model_pool` / `fallback_model_pool` | 主模型 + 同池/跨池备用模型；`persona_style_prompt` 为可选语气覆盖（留空则自动从主人历史消息抽取风格画像） |
| `llm.advanced` | `max_chars_daily_chat` / `max_chars_tech_issue` / `hard_truncation_chars` / `rag_auto_inject` / `rag_min_similarity` / `rag_max_results` / `rag_max_content_chars` / `low_confidence_handoff_enabled` / `low_confidence_threshold` / `history_tiering_recent` / `summary_async_enabled` / `summary_max_age_seconds` / `summary_min_coverage_ratio` / `summary_min_older` | 长度控制；RAG 自动注入门控（默认 `rag_min_similarity=0.6`、`rag_max_results=1`、`rag_max_content_chars=800`）与条数限制；低置信度转人工开关与阈值；历史分层阈值；异步摘要配置 |
| `llm_throttle` | `enabled` / `background_min_interval_seconds` / `idle_min_interval_seconds` / `rate_limit_backoff_seconds` / `extract_memory_*` / `max_summaries_per_cycle` | 后台 LLM 任务（摘要/记忆提取）限速与空闲降频，保护免费额度 |
| `embedding` | `enabled` / `provider` / `model` / `top_k` / `base_url` / `api_key` / `hf_token` / `offline` | BGE 向量模型（local / openai 两种 provider） |
| `memory.cleanup` / `memory.retrieval` / `memory.conversation_summary` / `memory.vector_*` | `max_age_days` / `min_similarity_*` / `max_messages_per_conversation` / `summary_interval_hours` / `summary_ratio` / `vector_index_type` / `vector_index_hnsw_ef` / `vector_phantom_rebuild_ratio` / `vector_cache_embeddings` | 记忆清理 / 检索 / 对话摘要压缩 / 向量索引类型与幽灵向量清理 |
| `rules` | `enabled` / `blacklist` / `whitelist` / `keywords` / `stop_words` / `keyword_denylist` / `intent_filter` / `regex_timeout_seconds` | 规则引擎 + 意图过滤（跳过无业务价值消息）+ ReDoS 防护 |
| `tools` | `enabled` / `available` / `rate_limit` / `allow_skill_tools` / `expose_all_tools` / `kb_search_enabled` / `tool_routing_mode` / `semantic_routing` / `semantic_tool_threshold` / `block_outbound_to_third_party` | 工具白名单、速率限制与按需暴露（smart/all/keyword）；`kb_search_enabled`（默认 true）控制是否注册 RAG 检索工具 `kb_search`；`block_outbound_to_third_party`（默认 true）硬拦截 AI 主动联系第三方 |
| `rag` | `chunk_size` / `chunk_overlap` | 知识库分块（默认 500 / 50）；检索走向量 0.6 + BM25 0.4 混合重排序 |
| `skills` | `enabled` / `auto_activate` / `semantic_routing` / `combo_enabled` / `combo_gap` / `hot_reload` / `ai_intent_generation_enabled` | 技能引擎：自动激活 / 语义路由 / 组合激活 / 热加载 |
| `safety` | `default_fallback` / `media_fallback_text` / `sensitive_words` | 兜底与敏感词 |
| `dead_letter` | `enabled` | 死信队列：LLM/发消息彻底失败时落库而非静默丢弃，可重放 |
| `storage` | `type` / `path` / `backup_enabled` / `backup_*` / `decisions_retention_days` / `messages_retention_days` / `doc_sync_interval_hours` | SQLite 路径与自动备份（legacy 兼容段，**运行期以 platforms.dingtalk.storage 为准**） |
| `logging` | `level` / `file` / `max_size_mb` / `max_backups` | 日志 |
| `web` | `port` / `host` / `auth_enabled` / `auth_username` / `auth_password` | 管理后台（安全默认仅本机回环 + 认证开启；密码为空且认证开启会启动报错） |
| `oa_approval` | `enabled` / `urge_reply_text` / `question_markers` / `action_markers` | 钉钉 OA 审批转发处理策略（别人转给你的审批 msgType=oa 默认回催办话术；含问号/动作标记则转 LLM 或审批工具） |
| `ocr_postprocess` | `enabled` / `min_chars` / `enabled_steps` | OCR 文本后处理管线（去不可见字符 / 压缩重复标点 / 去口语填充词 / 合并空行 / CJK 加空格） |
| `skillhub` | `auto_install` | SkillHub 市场（skill 榜单）安装脚本自动拉取开关（默认关，安全） |

## 多平台隔离（platforms）

灵桥支持同时接入多个 IM 平台（钉钉 / 企业微信 / 飞书），每个平台**独立适配器、独立数据库、独立轮询器**，数据天然隔离、互不干扰。

```yaml
platforms:
  - id: dingtalk                 # 平台唯一 id（也决定数据库文件名 ./data/<id>-ai.db）
    display_name: 钉钉
    enabled: true
    adapter_type: dingtalk       # dingtalk / feishu / wecom
    storage:
      type: sqlite
      path: ./data/linkora.db   # 平台专属库（默认库路径）
      backup_enabled: true
      backup_dir: ./data/backups
      backup_interval_hours: 24
      backup_max_count: 7
      backup_on_start: true
      decisions_retention_days: 14
      messages_retention_days: 90
      doc_sync_interval_hours: 1
    poller:                      # 与全局 poller 字段一致（见上表）
      interval_seconds: 10
      # ... 其余轮询参数
    adapter:                     # 钉钉 → DwsConfig 字段
      cli_path: dws
      dry_run: false
      profile: ''
      retries: 2
      timeout: 30
  # - id: feishu
  #   display_name: 飞书
  #   enabled: false
  #   adapter_type: feishu
  #   storage: { type: sqlite, path: ./data/feishu-ai.db }
  #   poller: { interval_seconds: 10 }
  #   adapter: { cli_path: lark-cli, dry_run: false }
```

- **运行期以 `platforms` 段为准**：`AppConfig.platforms` 为每个平台实例化独立的 `MessagePoller` / `LLMAgent` / `IMAdapter`。
- **向后兼容**：若 `config.yaml` 无 `platforms` 段，`load_config` 会用全局 `dws` / `storage` / `poller` 自动 seed 出一个 `dingtalk` 平台（数据库路径与行为完全不变）。
- **legacy 段保留**：顶层的 `dws` / `poller` / `storage` 由 `config_manage` 工具读取状态，请与 `platforms` 中对应平台的值保持一致。
- **新增平台**：在 `platforms` 列表内追加条目，`adapter_type` 设为 `feishu` / `wecom`，并将 `adapter.cli_path` 指向已安装的 CLI（`lark-cli` / `wecom-cli`）即可。

### 平台级覆盖（platforms[].rag / llm / tools）

> 字段定义见 `src/config_models.py` 的 `PlatformConfig`（`model_config = {"extra": "forbid"}`，故这些字段是**正式受支持**能力，而非残留配置）。

每个平台块（与 `storage` / `poller` / `adapter` 同级）可内嵌 **三个可选覆盖块** `rag` / `llm` / `tools`，用于**单独覆盖该平台的全局对应配置**：

- **不配置该块（或字段留空）** → 该平台**完全沿用全局** `rag` / `llm` / `tools`，无任何行为差异。
- **配置了该块** → 该平台**独立生效**自己的值；未填的字段仍继承全局。

**典型用途**

| 用途 | 覆盖块 | 示例 |
|---|---|---|
| 按平台分模型控成本 | `llm` | 飞书用便宜模型（`gpt-4o-mini`）、钉钉用主力模型 |
| 按平台隔离知识库 | `rag` | 不同平台挂不同 `embedding_model` 或 `chunk_size` 策略 |
| 按平台收窄工具集 | `tools` | 某平台关闭 `file_ops_enabled` 等 |

**可用字段（全部可选；省略 = 继承全局）**

- `rag`（`PlatformRagConfig`）：`chunk_size` / `chunk_overlap` / `chunk_hard_max`（安全天花板，字符数；`None`=派生为 `chunk_size*2`）/ `embedding_model`
- `llm`（`PlatformLLMConfig`）：`provider` / `base_url` / `api_key` / `model` / `temperature` / `max_tokens` / `timeout` / `fallback_model` / `fallback_api_key` / `fallback_base_url`
- `tools`（`PlatformToolsConfig`）：`search_enabled` / `file_ops_enabled` / `enabled`

**示例**（飞书单独用便宜模型 + 单独 KB 配置；其余平台不配即继承全局）：

```yaml
platforms:
  - id: feishu
    display_name: 飞书
    enabled: true
    adapter_type: feishu
    adapter: { cli_path: lark-cli }
    rag:
      embedding_model: BAAI/bge-base-zh-v1.5
      chunk_size: 800
      chunk_overlap: 120
      chunk_hard_max: 1600
    llm:
      provider: openai
      base_url: https://api.openai.com/v1
      api_key: sk-xxx
      model: gpt-4o-mini
      temperature: 0.3
      max_tokens: 2000
      timeout: 60
      fallback_model: gpt-4o
      fallback_api_key: sk-xxx
      fallback_base_url: https://api.openai.com/v1
    tools:
      search_enabled: true
      file_ops_enabled: false
      enabled: true
```

> 默认 `config.yaml.example` 中三个平台均未配置这些覆盖块；`config.yaml.example` 的 `platforms:` 段末尾另附一份**保持注释态**的参考示例，取消注释即可启用。

## 消息防抖

`poller.merge_window_seconds`（默认 60）+ 5 秒缓冲。同一人在窗口内的连续消息会合并为：

```
[消息1] 内容
[消息2] 内容
[消息3] 内容
```

然后作为单条上下文送入 LLM。

## 速率限制

`tools.rate_limit.<tool>.per_hour` 防止工具被滥用（如 `send_message` 防刷屏、`web_search` 防配额耗尽）。

## 规则引擎

匹配顺序：**黑/白名单 → 精确关键词 → 模糊关键词（jieba 分词 + 停用词）**。

```yaml
rules:
  enabled: true
  blacklist:                    # 黑名单中的用户/群组消息不处理；置空([])即不生效
    users: [张三]
    groups: []
  whitelist:
    enabled: false              # 是否启用白名单模式（仅处理白名单中的用户/群组）
    users: []
    groups: []
  keywords:
    - match: "在吗|在不在"
      reply: "在的，请问有什么事？"
  stop_words:
    - "失败, 成功, 错误, 问题, 文件"
  keyword_denylist:             # 从高频关键词词云中强制剔除的机器生成 token
    - dingtalkclient
    - mobilelink
  intent_filter:                # 意图过滤：识别并跳过无业务价值的消息（感谢/确认/结束/礼貌开场）
    enabled: true
    thank_you: [谢谢, 感谢]
    acknowledge: [收到, 好的, 明白]
    business_keywords: [问题, 错误, 配置, 排查]
```

## 多组织与跨组织处理

当你在钉钉中属于多个组织时，`dws` 拉取到的消息可能来自其他组织，而当前登录的 DWS profile 对该组织没有 CLI 数据访问权限，调用其会话接口会返回 `TOKEN_VERIFIED_FAILED`，并触发 `dws` 自动弹出 OAuth 浏览器窗口。

系统通过以下机制减少干扰：

1. **目标组织指定**：`poller.target_org_corp_id` 填写 `dws profile list` 中的某个 `corpId`，轮询器即只服务该组织的会话。留空则自动使用当前登录 profile 所属组织。该选项已在 Web 后台「设置 → 轮询高级 → 目标组织」提供下拉单选，实时生效。
2. **内存级跳过名单**：检测到跨组织权限错误的会话 ID 存入内存集合，后续轮询直接跳过。重启后自动清空，重新探测。
3. **本地优先认证检测**：所有认证状态检测优先读取本地 `~/.dws/profiles.json`，零网络调用，避免触发 dws 自动弹窗。

> 注意：钉钉 list API 返回的会话对象中不含组织字段，无法预先按组织过滤，只能"探测失败 → 内存跳过"。重启后跳过名单自动清空，便于重新探测。

## dws 认证与静默续期

**根因**：`dws` CLI 在收到 `TOKEN_VERIFIED_FAILED` 或 token 过期时会自动弹出 OAuth 浏览器窗口让用户重新授权。

**防御机制**：

1. **本地文件优先**：所有认证状态检测优先读取 `~/.dws/profiles.json`，不调用 dws 命令
2. **设备流静默登录**：登录时使用 `--device --no-browser`，不弹出浏览器窗口，而是在终端显示 userCode 和短链接供用户在其他设备上完成授权

启动时 `AuthMonitor` 会立即检查一次认证状态，若失效或即将过期则自动触发静默登录。

## 图片消息 OCR

`poller.image_ocr_enabled = true` 时，对方发来的图片消息（如截图）会自动下载到 `poller.image_temp_dir`（默认 `./data/tmp_images`，处理完即删），OCR 提取文字后随消息一并送入 LLM，使机器人能理解图片内容并据此作答。关闭后图片消息按普通附件处理（不识别内容）。

## 低置信度转人工与风格人格

- **低置信度转人工**：`llm.advanced.low_confidence_handoff_enabled`（默认 `true`）开启后，当 RAG 最佳相似度低于 `low_confidence_threshold`（默认 `0.35`）时，机器人不强行编造答案，而是转人工接管或生成草稿待确认。适合对"答错代价高"的场景。
- **风格人格**：`llm.persona_style_prompt` 留空时，系统启动时自动从主人的历史消息抽取语气 / 表达习惯画像（写入 `style_profiles` 表）并注入回复；如需固定人设，可在此显式填写系统级语气说明，覆盖自动画像。
- **动态 few-shot（按场景检索主人原话，提升口吻还原度）**：`llm.dynamic_few_shot`（默认 `false`）。开启后，每次生产回复基于当前消息做场景相似检索，从主人历史里取最像的 `llm.dynamic_few_shot_n`（默认 `4`）条 `(user→assistant)` 配对注入 system prompt，替代原本固定且被截断的静态样例（`build_system_prompt` 仅取 `[:1]` 且截断 40/60 字，近乎无效）。检索算法由 `llm.dynamic_few_shot_method` 控制：`trigram`（纯文本粗筛，零延迟）/ `embedding`（embedding 余弦精排，需向量）/ `hybrid`（默认，trigram 粗筛 + embedding 精排，embedding 复用 agent 已算好的 `query_vec`，无额外开销）。**默认关闭**，对现有回复行为零影响，可随时开关回退。
- **回测还原度评委口径**：`llm.backtest_judge_loose`（默认 `false`）。主人真实口吻多为口语 / 极简，与克隆回复字面重合度低，严格评委给分保守（约 10/100），难以反映真实还原度。开启后评委改为评「意图匹配 + 风格类别一致」，容忍措辞差异，回测分更贴近真实观感。**仅影响 `/api/persona/backtest` 打分，不影响真实回复。**
- **回复硬截断上限**：`llm.brevity_hard_cap`（默认 `0`）。`0` 表示沿用 `llm.advanced.hard_truncation_chars`（默认 300）；设为正数可覆盖普通回复的截断上限（用于按主人风格放宽 / 收紧，避免极简主人被无意义截断，或灌水主人被放过）。结构化 / 多行列表卡片仍放宽到 1300 字符。

## 后台限速、技能引擎与死信队列

- **后台 LLM 限速（`llm_throttle`）**：对话摘要与记忆提取属于后台 LLM 任务。免费 LLM 额度有严格频次限制，`llm_throttle` 对其实行节流——`background_min_interval_seconds`（活跃最小间隔）/ `idle_min_interval_seconds`（空闲降频间隔）/ `rate_limit_backoff_seconds`（触发 429 后暂停时长），并设有 `extract_memory_cooldown_seconds`（同会话记忆提取冷却）与 `max_summaries_per_cycle`（单周期摘要上限）。主模型触发 429/超时后后台任务自动暂停，保护免费额度。
- **技能引擎（`skills`）**：技能文件位于 `data/skills/{name}/SKILL.md`，声明 `intent_keywords` 与 `weight`，由智能引擎按意图自动调度激活。`auto_activate` 控制关键词自动激活；`semantic_routing` + `semantic_skill_threshold` 启用语义路由覆盖口语/同义改写；`combo_enabled` + `combo_gap` 支持复合意图组合激活多个 `composable` 技能；`hot_reload` 热加载新技能无需重启；`ai_intent_generation_enabled` 默认关闭（避免意外消耗 LLM 额度）。
- **死信队列（`dead_letter`）**：当主模型重试耗尽且备用模型也失败时，原始消息落库（而非静默丢弃），管理台可查看并重放（replay），避免消息石沉大海。关闭则退回旧行为（仅回 fallback 文本）。

## OA 审批 / OCR 后处理 / SkillHub / 向量索引（进阶配置组）

### OA 审批（`oa_approval`）

别人在钉钉里转给你的审批（`msgType=oa`）默认视为「催审批」，直接回固定话术、不调 LLM（省 token）。含问号 /「怎么 / 为什么」等标记的视为「提问」转交 LLM；含「转给 / 转交 / 离职」等动作的视为「动作指令」调用审批工具（`transfer_approval` 等）处理。

```yaml
oa_approval:
  enabled: true
  urge_reply_text: "请稍候，审批正在处理中，请耐心等待。"
  question_markers: ["?", "？", "怎么", "为什么", "为何", "什么情况", "什么意思", "合理吗", "对吗", "对不对", "帮我看", "帮我分析", "分析一下", "查一下", "看看", "哪", "如何"]
  action_markers: ["转给", "转交", "转由", "移交", "交接", "离职", "换人", "代批", "帮忙批", "改成", "转移给"]
```

### OCR 后处理（`ocr_postprocess`）

`poller.image_ocr_enabled = true` 下载截图 OCR 后，投喂 LLM 前执行文本清洗管线。各步骤可独立开关。

```yaml
ocr_postprocess:
  enabled: true
  min_chars: 5                       # 有效字符数阈值，低于此值跳过不投喂 LLM
  enabled_steps:
    remove_invisible: true           # 零宽/不可见控制字符剔除
    dedup_punctuation: true          # 连续重复标点压缩（>2 → 1）
    remove_fillers: true             # 口语填充词/语气词剔除
    normalize_layout: true           # 合并空行、去除首尾空白
    cjk_spacing: false               # 中文/英文/数字间加空格（默认关，避免改变原文排版）
```

### SkillHub 市场（`skillhub`）

```yaml
skillhub:
  auto_install: false                # 是否允许 Web 运行时自动拉取并执行 skillhub 安装脚本（默认关，安全）
```

### 记忆向量索引（`memory.vector_*`）

记忆向量索引类型与幽灵向量自动清理（规模增长时显式切 `hnsw` 提速）：

```yaml
memory:
  vector_index_type: flat            # flat=精确暴力检索(默认)；hnsw=近似检索，规模增长时提速显著
  vector_index_hnsw_ef: 64           # HNSW efConstruction/efSearch（仅 hnsw 生效）
  vector_phantom_rebuild_ratio: 0.3  # 幽灵向量占比超此阈值时自动重建回收空间（0=禁用自动重建）
  vector_cache_embeddings: true      # 索引内缓存归一化 embedding，支撑精确重建
```

## 环境变量

| 变量 | 用途 | 优先级 |
|---|---|---|
| `LLM_API_KEY` | 覆盖 `config.llm.api_key` | 1 |
| `HF_TOKEN` | HuggingFace 认证 | 2 |
| `HF_HUB_OFFLINE` | 设为 `1` 禁用 HF Hub 访问 | 强制（代码内自动设置） |
| `ENABLE_WEB` | Docker 环境下控制是否启动 Web 面板 | Docker 专用 |
