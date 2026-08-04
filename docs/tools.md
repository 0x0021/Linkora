# 工具清单

27 个内置工具统一继承 `BaseTool`，LLM 通过 OpenAI Function Calling 调用。

每个工具都配置了 **意图关键词**（`intent_keywords`），Agent 在挑选可用工具时按关键词打分，基础工具（send/recall_memory）始终包含。

## 工具列表

### 核心工具

| # | 工具名 | 中文名 | 用途 | 速率限制(/小时) |
|---|---|---|---|---|
| 1 | `send_message` | 发送消息 | 向指定会话发送消息，支持文本/图片/文件 | 128 |
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

### 管理与诊断工具

| # | 工具名 | 中文名 | 用途 | 速率限制 |
|---|---|---|---|---|
| 12 | `system_status` | 检查系统状态 | CPU/内存/DWS登录/数据库连接 | - |
| 13 | `message_stats` | 消息统计 | 查消息总量/工具调用/会话统计 | - |
| 14 | `keyword_rules` | 关键词规则管理 | 增删改关键词规则 | - |
| 15 | `config_manage` | 配置管理 | 读写运行时配置 | - |

### 业务工具

| # | 工具名 | 中文名 | 用途 | 速率限制 |
|---|---|---|---|---|
| 16 | `get_attendance` | 查询考勤 | 查个人考勤记录（月度/指定日期） | - |
| 17 | `send_ding` | 发送DING | 通过 DING 功能提醒他人 | - |
| 18 | `transfer_approval` | 审批转交 | 将指定审批任务转交给其他审批人（钉钉） | - |

### 会话与消息工具

| # | 工具名 | 中文名 | 用途 | 速率限制 |
|---|---|---|---|---|
| 19 | `get_unread` | 查询未读消息 | 汇总未读会话与消息摘要 | - |
| 20 | `get_conversation_info` | 查询会话信息 | 查会话详情、成员列表、类型 | - |
| 21 | `search_messages` | 搜索消息记录 | 按关键词检索历史消息 | - |

### 媒体与组织工具

| # | 工具名 | 中文名 | 用途 | 速率限制 |
|---|---|---|---|---|
| 22 | `upload_image` | 上传图片 | 上传本地图片/文件到钉钉，返回 media_id | - |
| 23 | `get_my_profile` | 查询个人信息 | 查姓名/工号/手机/邮箱/部门/组织 | - |
| 24 | `list_orgs` | 列出组织 | 列出已登录的所有组织信息 | - |
| 25 | `get_current_org` | 当前组织 | 查询当前活跃组织详细信息 | - |

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
    # ... 其他工具
```

## 速率限制

`tools.rate_limit.<tool>.per_hour` 控制每个工具每小时最大调用次数，防止滥用。

```yaml
tools:
  rate_limit:
    send_message:
      per_hour: 128
    web_search:
      per_hour: 512
```

## 工具路由模式

通过 `tools.tool_routing_mode` 控制每轮暴露给 LLM 的工具范围：

| 模式 | 说明 |
|---|---|
| `smart`（默认） | 关键词命中意图工具 → 精准暴露；无命中 → 全量兜底 |
| `all` | 每轮全量暴露所有已启用工具 |
| `keyword` | 纯关键词过滤，无命中回退基础工具（send/recall_memory/web_search/weather） |

详细路由逻辑参见 [design.md](design.md#6-路由模式详解)。
