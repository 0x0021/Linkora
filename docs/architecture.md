# 灵桥 (Linkora) — 架构文档

## 项目概述

灵桥 (Linkora) 是一个面向企业的 **AI 智能连接平台**，统一接入**钉钉**、**飞书**、**企业微信**三大办公 IM 平台。它融合 RAG 知识库检索、规则引擎、技能系统、LLM 对话与 Web 管理台，让 AI 成为企业协作与自动化的智能中枢。

### 三平台隔离架构

从设计之初，灵桥就采用**物理隔离**的多平台架构：

- 每个平台拥有**独立的 SQLite 数据库**（钉钉默认 `./data/linkora.db`；飞书/企微可显式配置 `./data/feishu-ai.db`、`/data/wecom-ai.db` 等独立库）
- 每个平台拥有**独立的轮询器实例**（`MessagePoller`）
- 每个平台拥有**独立的 CLI 适配器实例**（`DwsAdapter` / `FeishuCliAdapter` / `WecomCliAdapter`）
- 主进程通过 `PlatformContext` 数据类维护各平台运行期组件，借助按平台的 contextvar（`set_current_platform` / `get_current_platform`，位于 `src/memory/platform_context.py`）在线程内路由到正确平台

三个平台的配置在 `config.yaml` 的 `platforms` 列表中以独立条目形式存在，可任意组合启停。

---

## 技术栈一览

| 层次 | 技术选型 |
|------|---------|
| 语言 | Python 3.14+（仅 3.14 系列） |
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite（WAL 模式，NORMAL 同步级别） |
| 向量检索 | FAISS (CPU) |
| Embedding | BGE 中文向量模型（sentence-transformers） |
| 文本分类 | jieba 分词 + 正则匹配 |
| OCR | RapidOCR / pytesseract / pymupdf |
| 文档解析 | pdfplumber / python-docx / python-pptx / openpyxl |
| LLM 客户端 | OpenAI 兼容接口（多服务商主备切换） |
| 配置管理 | PyYAML + Pydantic 模型校验 + 热重载 |
| 部署 | Docker / docker-compose |
| 测试 | pytest + pytest-cov + pytest-timeout |
| 模板引擎 | Jinja2 |
| CLI 工具 | DWS（钉钉）/ lark-cli（飞书）/ wecom-cli（企微） |

---

## 组件图

> 简化版 Mermaid 架构图见 [README.md § 架构总览](../README.md#架构总览)。
> 本节为详细版（含内部模块：SkillLoader / ToolRouter / RAG / LLM 等）。

```
                          ┌──────────────────────────────┐
                          │      Web 管理台 (:8080)       │
                          │    FastAPI + Jinja2 模板       │
                          │    29 个路由模块 · 153 端点      │
                          └──────────────┬───────────────┘
                                         │
  ┌──────────────────────────────────────┼──────────────────────────────────┐
  │                              core (main.py)                             │
  │    LinkoraEngine · PlatformContext · BackgroundLLMThrottle · ConfigReload  │
  ├─────────────────────────────────────────────────────────────────────────┤
  │                                                                         │
  │  ┌──────────┐    ┌──────────────┐    ┌──────────────┐                   │
  │  │  Poller  │───▶│  RuleEngine  │───▶│   Intent     │                   │
  │  │(消息轮询) │    │  (规则引擎)   │    │  Registry    │                   │
  │  │          │    │              │    │ (意图注册表)   │                   │
  │  │ 8 Mixins │    │ · 黑名单过滤  │    └──────┬───────┘                   │
  │  │          │    │ · 冷却机制   │           │                           │
  │  └──────────┘    │ · 关键词匹配  │    ┌──────▼───────┐                   │
  │                  │ · 正则匹配   │    │  SkillLoader  │                   │
  │                  │ · 意图判定   │    │   (技能系统)   │                   │
  │                  └──────────────┘    │ · SKILL.md    │                   │
  │                                      │   解析与发现   │                   │
  │                                      │ · SkillTool   │                   │
  │                                      │   自动包装     │                   │
  │                                      └──────┬───────┘                   │
  │                                             │                           │
  │                                      ┌──────▼───────┐                   │
  │                                      │  ToolRouter  │                   │
  │                                      │  (工具路由)   │                   │
  │                                      │ · 38 内置工具  │                   │
  │                                      │ · 自动发现    │                   │
  │                                      │ · 语义匹配    │                   │
  │                                      │ · 组合激活    │                   │
  │                                      └──────┬───────┘                   │
  │                                             │                           │
  │                               ┌─────────────┼─────────────┐             │
  │                        ┌──────▼──────┐ ┌────▼────┐ ┌─────▼──────┐       │
  │                        │    LLM      │ │   RAG   │ │  Decision  │       │
  │                        │   Agent     │ │  检索   │ │  Tracker   │       │
  │                        │ (对话/回复)  │ │ BGE+BM25│ │ (决策追踪)  │       │
  │                        └─────────────┘ └─────────┘ └────────────┘       │
  │                                                                         │
  │  ┌──────────────────────────────────────────────────────────────────┐   │
  │  │                     SQLiteStore (数据库层)                        │   │
  │  │  · WAL 模式 · 连接池(线程级) · 懒加载 init · PII 脱敏            │   │
  │  │  · FAISS 向量索引 · BM25 倒排 · 重排序 · 跨进程锁               │   │
  │  └──────────────────────────────────────────────────────────────────┘   │
  │                                    │                                    │
  │  ┌─────────────────────────────────┼────────────────────────────────┐   │
  │  │                       Repository 层                              │   │
  │  │  baseline_repo · blacklist_repo · conversation_repo              │   │
  │  │  decisions_repo · docs_repo · draft_repo · external_friend_repo  │   │
  │  │  feedback_repo · few_shot_repo · kb_repo · memory_ops_repo      │   │
  │  │  memory_repo · message_repo · routing_quality_repo               │   │
  │  │  (14 个独立 repo，通过 store.conn 访问数据库)                     │   │
  │  └──────────────────────────────────────────────────────────────────┘   │
  │                                                                         │
  │  ┌──────────────────────────────────────────────────────────────────┐   │
  │  │  MetricsCollector (可观测性)  ·  ReportLogger (定时报告)          │   │
  │  │  tool_stats · routing_accuracy · blacklist_trends · token_stats  │   │
  │  └──────────────────────────────────────────────────────────────────┘   │
  │                                                                         │
  └─────────────────────────────────────────────────────────────────────────┘
```

---

## 数据流

一条用户消息在灵桥中的完整处理路径：

```
  IM 平台（钉钉/飞书/企微）
        │
        ▼
  MessagePoller（轮询器）
    · 拉取未读消息（list-all / 按会话拉取）
    · 去重（msg_id + LRU 缓存 + SQLite 持久化）
    · 合并（同会话短时间多条合并为一批）
    · 访问控制（单聊已读不回复闸门）
    · 派发（线程池 + 背压控制）
        │
        ▼
  RuleEngine（规则引擎）
    · 黑名单检查 → 命中则 skip
    · 冷却检查 → 冷却期内 skip
    · 关键词匹配（exact / fuzzy / regex）
      → 命中则 reply-rule（直接回复）
    · 意图识别 → 映射到 domain.* 意图类别
      → 判定 action = skip | reply-rule | pass
        │
        ▼ (action=pass)
  SkillManager + SemanticRouter
    · 意图类别 → 候选技能/工具集
    · 语义路由（Phase 3 combo / Phase 4 convergence）
    · 工具白名单生成
        │
        ▼
  LLMAgent（LLM 对话代理）
    · 系统提示注入（技能/工具/风格/岗位/知识库）
    · 历史上下文组装（含 H2-A 异步摘要压缩）
    · LLM 调用（主备服务商，含 rate limit 退避）
    · Tool Calling 循环（最多 N 轮）
        │
        ▼
  Reply（回复发送）
    · Markdown 卡片组装（提取标题）
    · DWS/CLI 发送
    · 标记已读
        │
        ▼
  DecisionTracker（决策记录）
    · 意图判定 + 路由决策 + 回复摘要 → SQLite
    · 内存有界队列（300 条）供管理台首页卡片
```

### 关键数据流节点说明

| 节点 | 输出 | 落库 |
|------|------|------|
| Poller | Message 对象 | `conversations` / `messages` 表 |
| RuleEngine | RuleResult (action/reason/intent) | — |
| SemanticRouter | 工具白名单 + 技能名 | — |
| LLMAgent | 回复文本 + tool_calls | `tool_execution_logs` 表 |
| DecisionTracker | DecisionRecord | `decisions` 表 |
| MetricsCollector | 只读统计查询 | 读取 `tool_execution_logs` / `routing_quality` / `blocked_conversations` |

---

## Repository 模式说明

### 为什么拆

`SQLiteStore` 原本是一个超过 1300 行的"上帝类"，包含：
- 数据库连接管理与 schema 初始化
- 向量索引与 BM25 检索
- 14 个业务域的 CRUD 操作
- PII 脱敏与全文搜索

随着项目演进，这个单体类成为开发瓶颈：
1. **修改耦合**：改黑名单逻辑可能影响向量索引的代码审查范围
2. **测试困难**：无法对单个业务域做隔离测试
3. **协作冲突**：多人同时修改同一文件导致 Git 冲突
4. **心智负担**：新人需要通读 1300+ 行才能理解一个简单操作

### 怎么拆

采用 **Repository 模式**，每个业务域抽出为独立 repo 类：

```
SQLiteStore (连接管理 + schema + 向量/BM25 + 全文搜索)
    │
    ├── DecisionsRepo     (decisions_repo.py)      —— 决策记录 CRUD
    ├── BlacklistRepo     (blacklist_repo.py)      —— 黑名单/冷却管理
    ├── ConversationRepo  (conversation_repo.py)   —— 会话管理
    ├── MessageRepo       (message_repo.py)        —— 消息记录
    ├── KbRepo            (kb_repo.py)             —— 知识库文档/分块
    ├── MemoryRepo        (memory_repo.py)         —— 长期记忆
    ├── MemoryOpsRepo     (memory_ops_repo.py)     —— 记忆压缩/归档
    ├── BaselineRepo      (baseline_repo.py)       —— 基线/评估
    ├── DocsRepo          (docs_repo.py)           —— 钉钉文档同步
    ├── DraftRepo         (draft_repo.py)          —— 草稿管理
    ├── ExternalFriendRepo(external_friend_repo.py)—— 外部好友
    ├── FeedbackRepo      (feedback_repo.py)       —— 回复反馈
    ├── FewShotRepo       (few_shot_repo.py)       —— Few-shot 示例
    └── RoutingQualityRepo(routing_quality_repo.py)—— 路由质量追踪
```

### 懒加载模式

每个 repo 接收 `SQLiteStore` 实例作为构造参数，通过 `self.store.conn` 属性获取线程级 SQLite 连接。`SQLiteStore.conn` 本身是懒加载的 —— 首次访问时才调用 `init_schema()` 初始化数据库表。

`store_factory.py` 提供线程安全的单例工厂 `get_store()`，确保同一数据库路径只有一个 `SQLiteStore` 实例。

### Schema 独立管理

DDL 语句从 `SQLiteStore` 中独立抽离到 `src/memory/schema.py` 的 `init_schema()` 函数中，支持：
- 幂等建表（`CREATE TABLE IF NOT EXISTS`）
- 列迁移（`_ensure_column` 为旧数据库补列）
- 索引补充（`_try_create_index`）
- 完整性检查（`PRAGMA integrity_check`）

---

## 数据库表一览

| 表名 | 用途 | 所属 Repo |
|------|------|----------|
| `conversations` | 会话管理 | ConversationRepo |
| `messages` | 消息记录（支持全文搜索） | MessageRepo |
| `memories` | 长期记忆（用户级隔离） | MemoryRepo |
| `kb_documents` | 知识库文档元数据 | KbRepo |
| `kb_chunks` | 知识库文本分块 + embedding | KbRepo |
| `keyword_rules` | 关键词匹配规则 | RuleEngine（直读） |
| `dedup_messages` | 消息去重持久化 | MessagePoller |
| `dingtalk_docs` | 钉钉文档同步缓存 | DocsRepo |
| `tool_execution_logs` | 工具调用日志 | ToolRouter |
| `blocked_conversations` | 黑名单/冷却 | BlacklistRepo |
| `dead_letter_messages` | 死信队列 | 管理台重放 |
| `message_drafts` | 审批草稿 | DraftRepo |
| `decisions` | 决策追踪 | DecisionsRepo |
| `routing_quality` | 路由质量追踪 | RoutingQualityRepo |
| `external_friends` | 外部好友映射 | ExternalFriendRepo |
| `style_profiles` | 风格画像 | SQLiteStore |
| `style_profile_versions` | 画像版本历史 | SQLiteStore |
| `feedback` | 回复反馈评分 | FeedbackRepo |
| `conversation_summaries` | H2-A 会话摘要缓存 | SQLiteStore |
| `kv` | 通用键值存储 | SQLiteStore |
