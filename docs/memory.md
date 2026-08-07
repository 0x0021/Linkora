# 长期记忆

## 数据模型

### 核心数据表

| 表名 | 说明 |
|---|---|
| `messages` | 收发消息记录 |
| `conversations` | 会话缓存 |
| `conversation_summaries` | 对话摘要压缩归档（CAS 代际写回） |
| `memories` | 长期记忆（按 `sender_id` 隔离） |
| `dedup_messages` | 消息去重缓存 |
| `tool_execution_logs` | 工具调用日志 |
| `kb_documents` / `kb_chunks` | 知识库文档与分块 |
| `keyword_rules` | 关键词规则 |
| `config` | 配置中心持久化 |
| `blocked_conversations` | 不遍历黑名单 |
| `style_profiles` | 风格人格画像（id=1 单例） |
| `feedback` | 反馈记录（message_id / rating / correction / note） |
| **`decisions`** | **决策追踪记录（意图/动作/路由模式/路由工具/回复预览）** |

### 记忆表结构

每条记忆绑定具体用户（`sender_id` 隔离），跨会话保留：

```sql
memories (
  id, sender_id, sender_name, chat_id,
  content, source, created_at, last_accessed_at,
  access_count, vector BLOB
)
```

### 决策追踪表结构

```sql
decisions (
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
)
```

## 写入策略

- LLM 主动调用 `save_memory(content=...)` 保存关键信息
- 写前先按内容去重（同 sender_id 相似度 > 0.9 视为重复）
- 系统提示词引导模型在对话结束时保存"用户偏好/事实/上下文"

## 召回策略

- `recall_memory(query=..., top_k=5)` 自动按当前 `sender_id` 过滤
- 最小相似度阈值（`memory.retrieval.min_similarity`，默认 0.6）过滤低质量结果
- 定期清理（`memory.cleanup.*`）删除过期低质记忆

## Agent 自动注入

Agent 会自动在 `recall_memory` 和 `save_memory` 工具调用中注入 `sender_id`、`sender_name`、`chat_id`，确保记忆绑定到正确用户，无需 LLM 显式传参。

## 相关配置

```yaml
memory:
  cleanup:
    enabled: true
    max_age_days: 30
    min_similarity_threshold: 0.9
  retrieval:
    min_similarity: 0.6
```
