# 工具清单

38 个内置工具统一继承 `BaseTool`，LLM 通过 OpenAI Function Calling 调用。完整清单以 `src/tools/registry.py` 的 `BUILTIN_TOOL_MANIFEST` 为单一真源，并与 `config.yaml.example` 的 `tools.available` 保持一致（CI 漂移测试守护，缺一项即报警）。

每个工具都配置了 **意图关键词**（`intent_keywords`），Agent 在挑选可用工具时按关键词打分；基础工具（`send_message` / `recall_memory` / `save_memory`）始终包含。

## 工具列表

### 核心工具（消息 / 文档 / 联系人 / 记忆）

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 1 | `send_message` | 发送消息 | 向指定会话发送消息，支持文本/图片/文件 | 30 |
| 2 | `search_contact` | 搜索联系人 | 通讯录按姓名/拼音搜索 | - |
| 3 | `get_calendar_events` | 查询日程事件 | 查今天/指定时间日程 | - |
| 4 | `create_todo` | 创建待办 | 创建钉钉待办任务 | 20 |
| 5 | `search_doc` | 搜索钉钉文档 | 搜索钉钉文档库 | - |
| 6 | `get_doc_content` | 读取文档内容 | 读取文档正文 | - |
| 7 | `kb_search` | 知识库检索 | RAG 检索私有知识库 | - |
| 8 | `recall_memory` | 召回长期记忆 | 拉取与话题相关的历史记忆 | - |
| 9 | `save_memory` | 写入长期记忆 | 持久化重要信息 | - |
| 10 | `web_search` | 联网搜索 | 调用外部搜索引擎 | 50 |
| 11 | `get_weather` | 查询天气 | 查指定城市天气 | 30 |

### 管理与诊断工具

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 12 | `system_status` | 检查系统状态 | CPU/内存/DWS登录/数据库连接 | - |
| 13 | `message_stats` | 消息统计 | 查消息总量/工具调用/会话统计 | - |
| 14 | `keyword_rules` | 关键词规则管理 | 增删改关键词规则 | - |
| 15 | `config_manage` | 配置管理 | 读写运行时配置 | - |

### 钉钉业务工具（考勤 / DING / 审批转交）

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 16 | `get_attendance` | 查询考勤 | 查个人考勤记录（月度/指定日期） | 20 |
| 17 | `send_ding` | 发送DING | 通过 DING 功能提醒他人 | 20 |
| 18 | `transfer_approval` | 审批转交 | 将指定审批任务转交给其他审批人（钉钉） | 10 |

### 会话与消息工具

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 19 | `get_unread` | 查询未读消息 | 汇总未读会话与消息摘要 | 60 |
| 20 | `get_conversation_info` | 查询会话信息 | 查会话详情、成员列表、类型 | 60 |
| 21 | `search_messages` | 搜索消息记录 | 按关键词检索历史消息 | 60 |

### 媒体与组织工具

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 22 | `upload_image` | 上传图片 | 上传本地图片/文件到钉钉，返回 media_id | 30 |
| 23 | `get_my_profile` | 查询个人信息 | 查姓名/工号/手机/邮箱/部门/组织 | 30 |
| 24 | `list_orgs` | 列出组织 | 列出已登录的所有组织信息 | 30 |
| 25 | `get_current_org` | 当前组织 | 查询当前活跃组织详细信息 | 30 |

### 钉钉 AI 听记（只读）

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 26 | `list_minutes` | 列出 AI 听记 | 列出 AI 听记/会议纪要（仅钉钉） | - |
| 27 | `get_minutes` | 获取听记详情 | 获取听记摘要/待办/转写/信息（仅钉钉） | - |

### 钉钉知识库（只读）

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 28 | `wiki_space_list` | 列出知识库空间 | 列出钉钉知识库空间（仅钉钉） | - |
| 29 | `wiki_space_search` | 搜索知识库空间 | 在知识库中搜索空间（仅钉钉） | - |
| 30 | `wiki_node_list` | 列出知识库节点 | 列出知识库空间下的节点（仅钉钉） | - |
| 31 | `wiki_node_search` | 搜索知识库节点 | 在知识库内搜索节点（仅钉钉） | - |

### 钉钉 OA 审批查询（只读）

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 32 | `approval_list_forms` | 列出审批表单 | 列出审批表单模板（仅钉钉） | - |
| 33 | `approval_search_forms` | 搜索审批表单 | 搜索审批表单模板（仅钉钉） | - |
| 34 | `approval_get_detail` | 查看审批详情 | 查看审批实例详情（仅钉钉） | - |
| 35 | `approval_list_pending` | 待我审批 | 查询待我审批（仅钉钉） | - |
| 36 | `approval_list_tasks` | 审批任务 | 查询审批实例下的审批任务（仅钉钉） | - |
| 37 | `approval_list_initiated` | 已发起审批 | 查询已发起审批记录（仅钉钉） | - |
| 38 | `approval_list_executed` | 我已处理审批 | 查询我已处理的审批（仅钉钉） | - |

## 工具白名单

LLM 只能调用 `config.yaml` 中 `tools.available` 列表里声明的工具，未声明的工具对 LLM 完全不可见。

```yaml
tools:
  available:
    - send_message
    - kb_search
    - recall_memory
    - save_memory
    - get_weather
    # ... 其余工具见 config.yaml.example（共 38 个）
```

## 速率限制

`tools.rate_limit.<tool>.per_hour` 控制每个工具每小时最大调用次数，防止滥用。未列出的工具不单独限流。

```yaml
tools:
  rate_limit:
    send_message:         { per_hour: 30 }
    create_todo:          { per_hour: 20 }
    get_attendance:       { per_hour: 20 }
    send_ding:            { per_hour: 20 }
    get_weather:          { per_hour: 30 }
    get_my_profile:       { per_hour: 30 }
    list_orgs:            { per_hour: 30 }
    get_current_org:      { per_hour: 30 }
    upload_image:         { per_hour: 30 }
    transfer_approval:    { per_hour: 10 }
    get_unread:           { per_hour: 60 }
    get_conversation_info: { per_hour: 60 }
    search_messages:      { per_hour: 60 }
    web_search:           { per_hour: 50 }
```

## 工具路由模式

通过 `tools.tool_routing_mode` 控制每轮暴露给 LLM 的工具范围：

| 模式 | 说明 |
|---|---|
| `smart`（默认） | 关键词命中意图工具 → 精准暴露；无命中 → 全量兜底 |
| `all` | 每轮全量暴露所有已启用工具 |
| `keyword` | 纯关键词过滤，无命中回退基础工具（send/recall_memory/web_search/weather） |

详细路由逻辑见 `design.md` 的「LLM 智能层」与「意图分类体系」章节。
