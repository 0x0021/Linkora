# 灵桥 (Linkora) — 设计总览

> 本篇聚焦**「为什么这么设计」**：项目定位、核心设计哲学、工具路由模式与决策追踪机制。
> 需要理解系统结构、分层、数据流、数据库 Schema 或模块清单，请读 [架构设计](architecture.md)。
> 工具 / 配置 / 意图 / 接口的细节分别见 [tools.md](tools.md) · [configuration.md](configuration.md) · [intent-model.md](intent-model.md) · [web-api.md](web-api.md)。

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

## 2. 路由模式详解

`tool_routing_mode` 配置项（默认 `smart`）控制每轮暴露给 LLM 的工具范围。

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

## 3. 决策追踪机制

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

## 4. DWS 适配器设计

`src/dws_adapter/`（包）— 封装所有 `dws` 命令调用，统一超时/重试/日志/JSON 解析。直接体现「dws 为唯一钉钉入口」原则。

**核心接口：**
- `chat_message_send()` / `chat_message_list_unread_conversations()` / `chat_message_list_direct()` / `chat_message_list()`
- `chat_conversation_info()` / `doc_search()` / `doc_get()` / `contact_user_search()` / `contact_user_get_self()`
- `calendar_event_list()` / `todo_task_create()`

**API 熔断器**：对 6 个易触发 dws 验证弹窗的接口实施熔断。首次权限错误即标记，冷却期（默认 30 分钟）内不再调用。冷却结束自动恢复，也可通过 `/api/clear-cross-org-skips` 手动重置。

---

## 参考文档导航

| 文档 | 说明 |
|---|---|
| [architecture.md](architecture.md) | 整体架构、分层、组件图、数据流、数据库表 |
| [tools.md](tools.md) | 内置工具清单与速率限制（单一真源 `BUILTIN_TOOL_MANIFEST`） |
| [configuration.md](configuration.md) | `config.yaml` 全部配置项 |
| [intent-model.md](intent-model.md) | 意图分类体系与工具映射 |
| [web-api.md](web-api.md) | 后端接口概览 |
| [rag.md](rag.md) | RAG 知识库：格式、分块、检索与重排序 |
