# Web API 概览

`web/api.py` 提供 153 个端点（含 29 个路由模块），均为 HTTP JSON 接口。默认端口 8080，可通过 `config.web.port` 调整。

## 认证

默认启用 HTTP Basic Auth（`config.web.auth_enabled`），白名单路径：`/`、`/health`、`/api/auth/login`、`/static/*`。

```
Authorization: Basic base64(username:password)
```

## 端点分组

| 路径前缀 | 功能 |
|---|---|
| `/api/status` · `/api/health` · `/api/config-drift` | 引擎状态 / 健康检查 / 配置漂移检测 |
| `/api/platforms` · `/api/system/paths` · `/api/platforms/health` | 多平台状态与运行时路径 |
| `/api/kb` | 知识库文档 CRUD、上传、URL 导入、检索、重排、钉钉/飞书文档同步 |
| `/api/skills`（含技能市场） | 技能管理 / 安装 / 市场排行 |
| `/api/departments` | 部门架构（懒加载） |
| `/api/conversations` · `/api/messages` | 会话与消息记录查询、导出 |
| `/api/image` | 图片 / OCR 代理与令牌 |
| `/api/routing-quality` | 路由质量统计与聚合 |
| `/api/memories` | 长期记忆 CRUD 与分类 |
| `/api/dead-letters` | 死信队列重放 / 丢弃 / 导出 |
| `/api/stats` | 消息与工具调用统计 |
| `/api/drafts` | 草稿审批（预览 / 批准 / 编辑 / 拒绝） |
| `/api/logs` | 运行日志读取 |
| `/api/tools` · `/api/tools-chain` | 工具清单 / 调用链路可视化 |
| `/api/feedback` | 反馈记录（评估闭环） |
| `/api/decisions` · `/api/decisions/history` · `/api/decisions/stats` · `/api/decisions/export` | 决策追踪（内存 / 持久化 / 统计） |
| `/api/orgs` · `/api/clear-cross-org-skips` | 组织与跨组织熔断 |
| `/api/simulate` | 对话模拟（调试） |
| `/api/keywords` | 关键词规则 CRUD |
| `/api/cost-quality` | 成本 / 质量看板（summary / trend / confidence-hist / citations） |
| `/api/persona` | 风格人格画像与版本 |
| `/api/external-friends` | 外部好友管理 |
| `/api/rules` | 规则引擎配置 |
| `/api/intents` | 意图分类体系（处置层+行动层） |
| `/api/dingtalk-docs` | 钉钉文档搜索 / 同步 / 导入 |
| `/api/config` · `/api/llm/prompt` | 配置读写、导入导出、系统提示词 |
| `/api/sync-history` · `/api/history/import` · `/api/messages/sync-history` | 历史消息导入 |
| `/api/metrics` · `/api/debounce-metrics` · `/api/backpressure-metrics` · `/api/embedding-status` · `/api/poller-status` · `/api/llm-metrics` | 可观测性指标（含按 request_id 追溯） |

## 核心数据表（SQLite）

| 表名 | 说明 |
|---|---|
| `messages` | 收发消息记录（含 `msg_id`、`sender_id`、`peer_*`） |
| `conversations` | 会话缓存 |
| `memories` | 长期记忆（含 `sender_id` 隔离） |
| `tool_execution_logs` | 工具调用日志 |
| `kb_documents` / `kb_chunks` | 知识库文档与分块 |
| `keyword_rules` | 关键词规则 |
| `config` | 配置中心持久化 |
| `decisions` | 决策追踪记录（意图/动作/路由模式/路由工具/回复预览） |
| `feedback` | 反馈记录（message_id / rating / correction / note） |
| `style_profiles` | 风格人格画像（profile_json / updated_at，id=1 单例） |

## 组织与熔断相关接口

### `GET /api/orgs`

返回已登录 DWS 的组织列表、当前/目标组织、跨组织跳过会话数及当前熔断状态。供设置页「目标组织」下拉与熔断状态展示使用。

**响应示例**：

```json
{
  "orgs": [
    { "corp_id": "ding9888ef577f7811cb", "corp_name": "珞石（山东）机器人集团股份有限公司" }
  ],
  "current": { "corp_id": "ding9888ef577f7811cb", "corp_name": "珞石（山东）机器人集团股份有限公司" },
  "target": "",
  "skipped_count": 12,
  "tripped": { "chat_message_list_all": 1756 }
}
```

`tripped` 为当前处于熔断冷却期的接口及其剩余冷却秒数；为空对象 `{}` 表示无熔断。

### `POST /api/clear-cross-org-skips`

清除跨组织/无效会话跳过名单，并重置全部 API 熔断器，下一轮轮询将重新探测所有会话与接口。切换目标组织或登录了更多组织后调用。

**响应示例**：

```json
{
  "cleared_conversations": 12,
  "cleared_circuit": 1,
  "skipped_count": 0,
  "tripped": {}
}
```

## 决策追踪相关接口

### `GET /api/intents`

返回当前生效的抽象意图分类体系，包含处置层与行动层的完整定义、证据词、工具映射，以及当前路由模式。

**响应示例**：

```json
{
  "disposition": [
    {"category": "business", "label": "业务消息", "description": "...", "cue_words": ["为什么", "怎么"]},
    {"category": "social.gratitude", "label": "致谢", "description": "...", "cue_words": ["谢谢"]}
  ],
  "action": [
    {"category": "action.query", "label": "信息查询", "description": "...", "cue_words": []}
  ],
  "tool_action_map": {"web_search": ["action.query"]},
  "meta": {
    "routing_mode": "smart",
    "tools_count": 38,
    "routing_mode_desc": "按意图关键词精准暴露相关工具；无明确意图时回退全量（不漏工具）"
  }
}
```

### `GET /api/decisions`

返回最近 n 条消息处理决策（进程内内存队列，重启即清空）。供首页「最近决策追踪」卡片实时展示。

**参数**：`n` (int, 默认 50) — 返回条数

### `GET /api/decisions/history`

分页查询持久化决策历史，数据来自 SQLite `decisions` 表，进程重启不丢失。

**参数**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `page` | int | 1 | 页码 |
| `page_size` | int | 20 | 每页条数（上限 100） |
| `sender_name` | str | — | 按发送者名称过滤 |
| `conversation_id` | str | — | 按会话 ID 过滤 |
| `intent` | str | — | 按意图类别过滤 |
| `action` | str | — | 按处理动作过滤（skip / reply-rule / llm） |

**响应示例**：

```json
{
  "items": [
    {
      "id": 1,
      "sender_name": "张三",
      "conversation_name": "XX项目群",
      "content_preview": "今天的进度怎么样了",
      "intent": "business",
      "action": "llm",
      "routing_mode": "smart",
      "routed_tools": ["kb_search", "send_message", "recall_memory"],
      "reply_preview": "根据知识库记录...",
      "created_at": "2026-07-11 22:30:15"
    }
  ],
  "total": 1523,
  "page": 1,
  "page_size": 20
}
```

### `GET /api/decisions/stats`

决策统计概览，返回各意图/动作/发送者的计数分布，及筛选下拉选项列表。

**响应示例**：

```json
{
  "total": 1523,
  "by_intent": {"business": 1100, "social.gratitude": 320, "social.acknowledge": 80},
  "by_action": {"llm": 850, "reply-rule": 350, "skip": 323},
  "by_sender": {"张三": 420, "李四": 350},
  "senders": ["张三", "李四", "王五"],
  "intents": ["business", "social.gratitude", "social.acknowledge"],
  "actions": ["skip", "reply-rule", "llm"]
}
```

## 反馈相关接口（评估闭环）

### `POST /api/feedback`

提交一条对 AI 回复的反馈，用于评估闭环（Feature D）。

**请求体**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `message_id` | str | 被评价的消息 ID |
| `conversation_id` | str | 会话 ID |
| `sender_id` | str | 发送者 ID |
| `rating` | int | 1=有用 / -1=无用 |
| `correction` | str | 主人纠正的正确回复（可选） |
| `note` | str | 备注 |

**响应示例**：

```json
{ "success": true, "feedback_id": 12, "message": "反馈已记录" }
```

### `GET /api/feedback`

回溯已记录的反馈列表，默认返回最近 200 条。

**参数**：`limit` (int, 默认 200)

**响应示例**：

```json
{ "success": true, "feedback": [ { "id": 12, "rating": -1, "correction": "正确答案应为……", "created_at": "2026-07-17 21:00:00" } ] }
```

> 离线评测另见 `scripts/eval_rag.py` + `scripts/golden_qa.json`：以黄金问答集对 RAG 检索与重排序做批量回归，持续校准阈值与权重。
