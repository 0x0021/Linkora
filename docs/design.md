# 灵桥 (Linkora) — 企业 AI 智能连接平台

> 灵桥 (Linkora) 是基于 `dws` 等平台 CLI 适配器的企业 AI 智能连接平台，统一接入钉钉、企业微信、飞书，规则优先、LLM 兜底、支持 Tool Calling 与长期记忆。连接企业智能，桥接无限可能。

---

## 1. 项目定位与设计哲学

灵桥 (Linkora) 是基于 dingtalk-workspace-cli（`dws`）等平台适配器的企业 AI 智能连接平台，统一接入钉钉、企业微信、飞书。

**核心设计原则：**

1. **规则优先，LLM 兜底**：确定性逻辑走规则引擎（黑白名单/关键词/意图分类），不确定才调用 LLM
2. **dws 为唯一钉钉入口**：所有钉钉操作通过 `dws` CLI，不直接调 OpenAPI
3. **可插拔 Tool**：每个 Tool 独立实现、统一接口（`BaseTool`）、白名单控制
4. **幂等与去重**：基于 `msg_id` 全局去重，发送走 `--uuid` 幂等
5. **配置驱动**：所有可变项走 YAML 配置，pydantic 校验，环境变量覆盖

---

## 2. 系统架构总览

### 四层架构

```
┌─────────────────────────────────────────────────────────┐
│                 消息采集层（Polling Layer）                │
│  poller.py → DWS拉取 → 去重 → 合并窗口 → 回复冷却        │
├─────────────────────────────────────────────────────────┤
│                 规则引擎层（Rule Layer）                   │
│  rule_engine.py → 黑白名单 → 意图分类 → 关键词规则         │
├─────────────────────────────────────────────────────────┤
│                 LLM 智能层（Agent Layer）                 │
│  llm/agent.py → smart/all/keyword 路由 → Tool Selection  │
│                  intent.py → 处置层 + 行动层意图           │
├─────────────────────────────────────────────────────────┤
│                 工具执行层（Tool Layer）                   │
│  tools/*（27 个工具）→ dws_adapter.py → DWS CLI           │
└─────────────────────────────────────────────────────────┘
                            │
               ┌────────────┼────────────┐
               ▼            ▼            ▼
          memory/       decision_     web/
       sqlite_store.py  tracker.py   api.py (FastAPI)
```

---

## 3. 核心模块清单及职责

### 3.1 消息采集层

| 模块 | 职责 |
|---|---|
| `src/poller.py`（约 266 行） | 消息轮询/去重/合并窗口/回复冷却/跨组织跳过/持久化黑名单/单聊"已读不回复"闸门 |
| `src/poller_core_parse.py` | 入站消息解析（含 OA 审批卡片、DING 消息、系统通知） |
| `src/dws_adapter.py` | 钉钉 DWS CLI 封装，统一超时/重试/JSON解析，API 熔断器 |
| `src/auth_monitor.py` | DWS 登录态检测与自动续期 |

**poller.py 核心机制：**
- **消息去重**：`msg_id` 经进程内 LRU 缓存 + SQLite `dedup_messages` 表双重去重
- **合并窗口**：同人短时（可配）多条消息自动合并为一条投递，减少 LLM 调用
- **回复冷却**：同一会话在冷却期内不再触发新一轮 LLM 处理
- **跨组织跳过**：探测到跨组织权限错误→内存+持久化黑名单，后续直接跳过，避免反复触发 dws 弹窗
- **list-all 空轮探针**：连续 N 轮无新消息则告警
- **单聊"已读不回复"闸门**：对方发来的消息已被我读完时，跳过自动回复（外部好友除外）

### 3.2 规则引擎层

| 模块 | 职责 |
|---|---|
| `src/rule_engine.py`（约 407 行） | 黑白名单（人/群、支持正则）/关键词规则匹配/意图分类委托 |
| `src/intent.py`（约 890 行） | 意图分类体系：处置层判定（business/social 子型）+ 行动层意图匹配 |

**规则优先级（从高到低）：**

| 优先级 | 规则类型 | 说明 |
|---|---|---|
| 1 | 黑名单（人/群） | 支持正则，命中直接丢弃 |
| 2 | 免打扰时段 | 支持每天时段/按星期/周末，命中不回复仅记录 |
| 3 | 白名单模式 | 开启时仅白名单内会话继续处理 |
| 4 | 意图过滤（处置层） | 社交意图（致谢/确认收到/道别/招呼）→ 跳过 |
| 5 | 关键词规则（DB/SQLite 管理） | 支持精确/模糊匹配，命中返回预设回复 |
| 6 | LLM Agent | 以上均未命中 → 进 LLM 智能处理 |

### 3.3 LLM 智能层

| 模块 | 职责 |
|---|---|
| `src/llm/agent.py`（约 1162 行） | LLM Agent：路由模式解析/工具选择/执行循环/回复生成 |
| `src/llm/client.py` | OpenAI 兼容 LLM 客户端（主备双模型自动切换） |
| `src/llm/summary_scheduler.py`（约 186 行） | 后台异步摘要调度器（H2-A：单 daemon 线程 + 队列 + CAS 写回） |
| `src/intent.py`（约 890 行） | 意图分类体系（处置层+行动层），供路由与决策追踪使用 |
| `src/decision_tracker.py`（约 189 行） | 决策追踪器：进程内 deque + SQLite 双写 |

**Agent 执行循环：**
```
while 未生成最终文本 且 未达最大轮次:
    调用 LLM
    若返回文本消息 → 作为最终回复，break
    若返回 tool_calls → 逐个执行工具 → 结果追加到 messages → continue
    若达到收敛阈值 → 移除检索类工具，强制综合
```

### 3.4 工具执行层

| 模块 | 职责 |
|---|---|
| `src/tools/base.py` (192行) | `BaseTool` 抽象基类 / `ToolRouter` 路由注册 / `RateLimiter` 限流 |
| `src/tools/` (27 个工具) | 具体工具实现，见下文工具清单 |

### 3.5 基础设施层

| 模块 | 职责 |
|---|---|
| `src/memory/sqlite_store.py`（约 1162 行；业务 CRUD 拆分至 `src/memory/*_repo.py`） | SQLite 持久化：15+ 张数据表，含 decisions 决策追踪表 |
| `src/memory/embedding.py` | BGE 中文向量模型 |
| `src/memory/vector_index.py` | FAISS 向量索引 |
| `src/memory/reranker.py` | 向量+关键词混合重排序 |
| `src/config.py`（约 1030 行） | Pydantic 配置模型 |
| `src/models.py` | Message 等数据类 |
| `src/db_backup.py`（约 282 行） | SQLite 自动备份 |
| `src/doc_sync_scheduler.py`（约 260 行） | 钉钉文档定时同步（检测内容变化，自动重新导入知识库） |
| `src/shared_state.py` | 主进程与 Web 共享状态 |
| `web/api.py`（已按路由拆分至 `web/routers/`） | FastAPI 管理后台，约 30 个路由模块、150+ 端点 |

---

## 4. 数据流全景

```
1. 新消息进入
   └─ poller 拉取（list-all 主通道 / list-unread 辅助通道）
        └─ msg_id 双重去重（内存 + SQLite）

2. 去重后进入处理流水线
   ├─ 合并窗口：同人短时多条 → 合并
   ├─ 回复冷却：冷却期内 → 跳过
   ├─ 单聊"已读不回复"闸门 → 跳过
   └─ 进入规则引擎

3. 规则引擎
   ├─ 黑名单命中 → 丢弃（tracker.record action=skip）
   ├─ 免打扰时段 → 跳过（tracker.record action=skip）
   ├─ 白名单过滤 → 非白名单跳过
   ├─ 意图过滤：社交意图 → 跳过（tracker.record action=skip, intent=social.xxx）
   ├─ 关键词规则命中 → 直接回复（tracker.record action=reply-rule）
   └─ 未命中 → 进入 LLM Agent（tracker.record action=llm）

4. LLM Agent
   ├─ 路由模式解析：smart / all / keyword
   ├─ smart：关键词命中意图工具 → 精准暴露；无命中 → 全量兜底
   ├─ all：每轮全量暴露所有工具
   ├─ keyword：纯关键词过滤（无命中回退 FALLBACK 基础工具）
   ├─ 工具执行循环（最多 max_tool_rounds 轮）
   │   └─ 连续检索收敛护栏：移除 web_search/kb_search/search_doc 强制综合
   ├─ RAG 自动注入（三层门控：长度/有意义文本/文档类意图）
   │   └─ per-turn 门控：仅实际注入 RAG 的轮次才下发知识使用规则护栏
   ├─ 长期记忆自动召回 + 对话上下文
   ├─ 对话历史分层：max_recent 条近期消息完整保留，older 段走缓存或异步摘要（H2-A）
   └─ 决策追踪：每步路由记录落库供回溯

5. 回复生成
   ├─ 清洗（去控制字符/发送者前缀）
   ├─ 敏感词过滤
   ├─ 速率限制检查
   ├─ 生成 UUID（幂等）
   └─ 通过 dws chat message send 发送

6. 收尾
   ├─ msg_id 写入 dedup_messages
   ├─ 消息写入 messages 表
   ├─ 更新 conversation 状态
   ├─ decision_tracker.record() → 内存 deque + SQLite decisions 表
   └─ 可选：异步提炼长期记忆
```

---

## 5. 数据库 Schema

主数据库：`data/linkora.db`（SQLite，各平台独立数据库文件）

| 表名 | 用途 |
|---|---|
| `messages` | 收发消息记录（msg_id / sender_id / content / timestamp / role / chat_id / peer_*） |
| `conversations` | 会话缓存（chat_id / chat_name / chat_type / last_message_at / message_count） |
| `conversation_summaries` | 对话摘要压缩归档（chat_id / summary / covered_count / generation / created_at） |
| `memories` | 长期记忆（sender_id / content / source / vector BLOB / access_count） |
| `dedup_messages` | 消息去重缓存（msg_id 唯一索引） |
| `processed_msg_ids` | 已处理消息 ID（与 dedup 配合） |
| `tool_execution_logs` | 工具调用日志（tool_name / input / output / success / duration_ms） |
| `kb_documents` | 知识库文档（title / source / format / content / created_at） |
| `kb_chunks` | 知识库分块（document_id / content / vector BLOB / chunk_index） |
| `keyword_rules` | 关键词规则（match_pattern / reply_text / match_type / priority / enabled） |
| `config` | 配置中心持久化（key-value） |
| `blocked_conversations` | 不遍历黑名单（持久化跨组织/无权限会话） |
| `external_friends` | 外部好友管理 |
| `style_profiles` | 风格人格画像（profile_json / updated_at，id=1 单例） |
| `feedback` | 反馈记录（message_id / rating / correction / note） |
| **`decisions`** | **决策追踪记录（sender_id / sender_name / intent / action / routing_mode / routed_tools / reply_preview / created_at）** |

### decisions 表结构

```sql
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id TEXT NOT NULL,
    sender_name TEXT DEFAULT '',
    conversation_id TEXT DEFAULT '',
    conversation_name TEXT DEFAULT '',
    content_preview TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    action TEXT NOT NULL,
    routing_mode TEXT DEFAULT '',
    routed_tools TEXT DEFAULT '',
    reply_preview TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_sender ON decisions(sender_id);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
```

---

## 6. 路由模式详解

`tool_routing_mode` 配置项（默认 `smart`），控制每轮暴露给 LLM 的工具范围。

| 模式 | 决策逻辑 | 适用场景 |
|---|---|---|
| **smart**（默认） | 关键词命中意图工具 → 精准暴露相关工具；无明确意图关键词 → 回退全量（交给主模型自选，保证不漏） | 大多数场景：既省 token 又不漏工具 |
| **all** | 每轮全量暴露所有已启用工具 | 测试/调试，或模型能力很强时可全量 |
| **keyword** | 纯 intent_keywords 过滤：命中则暴露，无命中回退 FALLBACK 基础工具（send_message/save_memory/recall_memory/web_search/get_weather） | 需要严格控制工具暴露范围、减少 LLM 乱调工具 |

**smart 模式具体流程：**
1. 提取消息文本
2. 始终包含基础工具：send_message, save_memory, recall_memory
3. 遍历所有工具：
   - 工具声明了 `intent_keywords`（具体场景词）→ 仅据此精准判定
   - 工具未声明具体场景词 → 用其抽象行动意图（`TOOL_ACTION_MAP`）证据词兜底
4. 命中任一非基础工具 → 精准暴露命中集
5. 无命中 → 回退全量暴露所有工具（交给主模型自行判断）

---

## 7. 意图分类体系

> 实现：`src/intent.py`（`IntentCategory` / `IntentRegistry` / `TOOL_ACTION_MAP`）

### 设计原则

1. **两层结构**：处置层判断消息是否值得处理，行动层识别具体抽象动作
2. **抽象而非穷举**：工具不各自列举场景词，而是声明自己服务哪些抽象行动意图
3. **证据词集中维护**：具体关键词在注册表里集中声明，新增场景只扩充证据词，无需改分支

### 第一层：处置意图（DISPOSITION）

| 类别 | 语义 | 典型触发 |
|---|---|---|
| `business` | 包含可被助手处理的行动意图（提问/请求/指令） | 含疑问词/请求动词/领域关键词/较长非套话消息 |
| `social.gratitude` | 致谢，无后续行动诉求 | 含感谢类词 + 短消息 |
| `social.acknowledge` | 确认已读/知晓，无新请求 | 含确认类词（收到/好的/OK）+ 短消息；超长降级 business |
| `social.closing` | 结束语，表达离开意向 | 含道别类词 |
| `social.polite` | 问候/客套/致歉，无实质请求 | 含招呼/客套词 + 短消息 |

**优先级裁决（保证互斥）：** `business` > `social.gratitude` > `social.polite` > `social.acknowledge` > `social.closing` > 默认 `business`

### 第二层：行动意图（ACTION）

行动意图彼此正交、可共存（一条消息可同时是 `query` + `communicate`）。

| 类别 | 语义边界 | 典型触发 |
|---|---|---|
| `action.query` | 获取已有信息/状态/数据，无副作用 | 天气/搜索/知识库/文档/通讯录/日历/审批/考勤等读取类 |
| `action.execute` | 创建/修改/发送/触发，产生状态变更或副作用 | 发送/创建/安排/设置/提交/提醒/预约等 |
| `action.analyze` | 对已有信息总结/对比/分析，或生成新内容 | 总结/概括/分析/生成/起草/复盘等 |
| `action.communicate` | 与人的消息往来、会话管理 | 发消息/看未读/查会话/搜记录/@某人 |
| `action.media` | 上传或处理图片/文件/语音/视频等媒体素材 | 上传图片/文件/截图/媒体操作 |

### 工具 → 抽象行动意图映射

| 工具 | 服务意图 |
|---|---|
| send_message | action.execute, action.communicate |
| save_memory | action.analyze |
| recall_memory | action.query, action.analyze |
| web_search / get_weather / kb_search | action.query |
| search_doc / get_doc_content / search_contact | action.query |
| get_calendar_events / get_attendance / approval_get_detail / approval_list_pending | action.query |
| get_my_profile / list_orgs / get_current_org | action.query |
| system_status / message_stats / keyword_rules | action.query |
| config_manage | action.query, action.execute |
| get_unread / get_conversation_info / search_messages | action.query, action.communicate |
| create_todo / send_ding | action.execute (+ send_ding: action.communicate) |
| upload_image | action.execute, action.media |

---

## 8. 决策追踪机制

### 完整链路

```
消息处理 → rule_engine / llm_agent 判定
       → decision_tracker.tracker.record(
             sender_id, sender, chat, content, intent,
             action,     # skip | reply-rule | llm
             routing_mode,  # smart | all | keyword | None
             routed_tools,  # 本轮暴露给 LLM 的工具名列表
             reply_preview
         )
       → 写入内存 deque（默认 300 条，首页卡片近实时展示）
       → 同时写入 SQLite decisions 表（持久化，进程重启不丢失）
       → Web UI 双渠道展示：
          ① 首页「最近决策追踪」卡片（固定高度滚动，实时轮询）
          ② 「意图 & 路由 → 决策追踪」子页面（分页+筛选，持久化历史）
```

### DecisionTracker

- 进程级单例 `tracker`，在 `main.py` 中通过 `tracker.set_sqlite_store(store)` 注入存储后端
- `record()` 双写：内存 deque + SQLite（持久化失败不阻塞主流程）
- `recent(n)` 从内存读取，供首页实时展示
- 三类记录点（`main.py`）：
  - `action="skip"`：意图过滤跳过
  - `action="reply-rule"`：关键词规则直接回复
  - `action="llm"`：交给 LLM 处理（含 routing_mode + routed_tools）

### Web API

| 端点 | 说明 |
|---|---|
| `GET /api/decisions` | 最近 n 条（内存 deque） |
| `GET /api/decisions/history` | 分页查询持久化历史（支持 sender_name / conversation_id / intent / action 过滤） |
| `GET /api/decisions/stats` | 决策统计（各意图/动作/发送者计数 + 筛选下拉选项） |

---

## 9. 工具清单（完整 27 个）

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 1 | `send_message` | 发送消息 | 向指定会话发送消息 | 128 |
| 2 | `search_contact` | 搜索联系人 | 通讯录按姓名/拼音搜索 | - |
| 3 | `get_calendar_events` | 查询日程事件 | 查今天/指定时间日程 | - |
| 4 | `create_todo` | 创建待办 | 创建钉钉待办任务 | 512 |
| 5 | `search_doc` | 搜索钉钉文档 | 搜索钉钉文档库 | - |
| 6 | `get_doc_content` | 读取文档内容 | 读取文档正文 | - |
| 7 | `kb_search` | 知识库检索 | RAG 检索私有知识库 | - |
| 8 | `recall_memory` | 召回长期记忆 | 拉取与话题相关的历史记忆 | - |
| 9 | `save_memory` | 写入长期记忆 | 持久化重要信息 | - |
| 10 | `web_search` | 联网搜索 | 调用外部搜索引擎 | 512 |
| 11 | `get_weather` | 查询天气 | 查指定城市天气 | 512 |
| 12 | `system_status` | 检查系统状态 | CPU/内存/服务健康 | - |
| 13 | `message_stats` | 消息统计 | 查消息/工具调用统计 | - |
| 14 | `keyword_rules` | 关键词规则管理 | 增删改关键词规则 | - |
| 15 | `config_manage` | 配置管理 | 读写运行时配置 | - |
| 16 | `transfer_approval` | 审批转交 | 将指定审批任务转交给其他审批人（钉钉） | - |
| 17 | `get_attendance` | 查询考勤 | 查个人考勤记录 | - |
| 18 | `send_ding` | 发送DING | 通过 DING 功能提醒他人 | - |
| 19 | `get_unread` | 查询未读消息 | 汇总未读会话与消息摘要 | - |
| 20 | `get_conversation_info` | 查询会话信息 | 查会话详情与成员 | - |
| 21 | `search_messages` | 搜索消息记录 | 按关键词搜索历史消息 | - |
| 22 | `upload_image` | 上传图片 | 上传本地图片/媒体到钉钉 | - |
| 25 | `get_my_profile` | 查询个人信息 | 查姓名/工号/部门/组织 | - |
| 26 | `list_orgs` | 列出组织 | 列出已登录的组织列表 | - |
| 27 | `get_current_org` | 当前组织 | 查询当前活跃组织信息 | - |

---

## 10. DWS 适配器

`src/dws_adapter.py` — 封装所有 `dws` 命令调用，统一超时/重试/日志/JSON 解析。

**核心接口：**
- `chat_message_send()` / `chat_message_list_unread_conversations()` / `chat_message_list_direct()` / `chat_message_list()`
- `chat_conversation_info()` / `doc_search()` / `doc_get()` / `contact_user_search()` / `contact_user_get_self()`
- `calendar_event_list()` / `todo_task_create()`

**API 熔断器**：对 6 个易触发 dws 验证弹窗的接口实施熔断。首次权限错误即标记，冷却期（默认 30 分钟）内不再调用。冷却结束自动恢复，也可通过 `/api/clear-cross-org-skips` 手动重置。

---

## 11. 配置（config.yaml 核心项）

| 分组 | 关键配置项 |
|---|---|
| **`platforms`** ★ | id / display_name / enabled / adapter_type / storage / poller / adapter（多平台隔离主配置段，运行期以本段为准） |
| `dws` | cli_path / timeout / retries / dry_run / profile（legacy 兼容段，`config_manage` 读取） |
| `poller` | interval_seconds / unread_conversation_count / messages_per_conversation / history_window / history_days / merge_window_seconds / max_dispatch_per_cycle / max_concurrent_replies / target_org_corp_id / blacklist_* / image_ocr_enabled / list_all_empty_alert_rounds / ai_tag_enabled |
| `llm` | provider / base_url / api_key / model / temperature / max_tokens / max_tool_rounds / system_prompt / persona_style_prompt / model_pool / fallback_model_pool |
| `llm.advanced` | max_chars_daily_chat / max_chars_tech_issue / hard_truncation_chars / rag_auto_inject / rag_max_results / low_confidence_handoff_enabled / low_confidence_threshold / history_tiering_recent / summary_async_enabled / summary_max_age_seconds / summary_min_coverage_ratio / summary_min_older | 长度控制；RAG 自动注入门控；低置信度转人工；历史分层阈值；异步摘要配置 |
| `llm_throttle` | enabled / background_min_interval_seconds / idle_min_interval_seconds / rate_limit_backoff_seconds / extract_memory_* / max_summaries_per_cycle |
| `embedding` | enabled / provider / model / top_k / base_url / api_key / hf_token / offline |
| `memory` | cleanup / retrieval / conversation_summary |
| `rules` | enabled / blacklist / whitelist / keywords / stop_words / keyword_denylist / intent_filter / regex_timeout_seconds |
| `tools` | enabled / available / rate_limit / allow_skill_tools / expose_all_tools / tool_routing_mode / semantic_routing / semantic_tool_threshold |
| `rag` | chunk_size / chunk_overlap |
| `skills` | enabled / auto_activate / semantic_routing / combo_enabled / combo_gap / hot_reload / ai_intent_generation_enabled |
| `safety` | sensitive_words / default_fallback / media_fallback_text |
| `dead_letter` | enabled |
| `storage` | type / path / backup_enabled / backup_* / decisions_retention_days / messages_retention_days / doc_sync_interval_hours（legacy 兼容段） |
| `web` | port / host / auth_enabled / auth_username / auth_password |

---

## 12. 技术栈

| 类别 | 选型 |
|---|---|
| 语言 | Python 3.13+（推荐 3.13/3.14） |
| 钉钉接口 | dws CLI (v1.0.46+) |
| LLM | OpenAI 兼容 API（支持第三方代理/Ollama），主备双模型 |
| 存储 | SQLite（本地单文件，15+ 张表，各平台独立数据库） |
| 向量检索 | FAISS（BGE 中文向量，Apple Silicon MPS 加速） |
| 配置 | YAML + pydantic 校验 + 环境变量覆盖 |
| 日志 | logging + RotatingFileHandler |
| Web 后端 | FastAPI |
| Web 前端 | Bootstrap 5 + 原生 JS（SPA） |
| 后台运行 | launchd (macOS) / nssm (Windows) / Docker |
