# 抽象意图分类体系（Intent Taxonomy）

> 代码实现：`src/intent.py`（`IntentCategory` / `IntentRegistry` / `TOOL_ACTION_MAP`）
> 运行时观测：`GET /api/intents`（返回完整分类体系 + 工具映射）

## 设计原则

1. **抽象而非穷举**：工具不再各自列举"天气/气温/下雨"之类的场景词，而是声明自己服务于哪些**抽象行动意图**（如 `action.query`）。具体词作为某意图的*证据词库*集中在注册表里维护，新增场景只需扩充证据词，无需改匹配分支。
2. **两层 + 层次结构**：
   - **处置层（DISPOSITION）**：消息是 `business`（需处理）还是 `social`（纯社交、跳过）。`social` 下再分子型（致谢/确认/道别/礼貌），仅用于语义细分与日志观测，不改变"跳过"判定。
   - **行动层（ACTION）**：业务消息想要的*抽象动作*类别。行动意图彼此**正交、可共存**，因此不做互斥约束，只保证各自语义边界清晰。
3. **可扩展**：新增意图类型 = 在 `DEFAULT_INTENTS` 注册一个 `IntentCategory`；新增工具 = 在 `TOOL_ACTION_MAP` 加一行。都不需要改匹配分支。
4. **互斥性**：处置层的 `business` 与 `social` 互斥（命中 business 即不归 social），由 `classify_disposition` 的优先级裁决。
5. **每个抽象意图都有定义**：下方给出语义边界 + 典型触发条件。

## 分类体系

### 第一层：处置意图（DISPOSITION）—— 消息是否值得处理

| 类别 | 父 | 语义边界（核心语义） | 典型触发条件 |
|------|----|----------------------|--------------|
| `business` 业务意图 | — | 消息包含可被助手处理的行动意图（提问/请求/指令），或涉及工作/生活领域的具体内容，需要响应或执行操作。与 social 互斥。 | 含疑问词（什么/怎么/为什么/多少/哪）；含请求动词（帮我/排查/创建）；命中领域关键词（审批/天气/文档/股价…）；或较长非套话消息 |
| `social` 社交意图 | — | 消息仅为礼节性社交交换（致谢/确认/道别/问候/称赞/寒暄/情绪感叹），不含任何需执行或回答的请求/问题/指令。助手应跳过（或极简回应）。 | 短消息且命中社交词表、且不含 business 证据 |
| ↳ `social.gratitude` 致谢 | social | 表达谢意、认可对方付出，无后续行动诉求 | 含感谢类词（谢谢/感谢/辛苦了/蟹蟹/thx）+ 长度 ≤ 阈值 |
| ↳ `social.acknowledge` 确认收到 | social | 确认已读/知晓，无新请求或问题 | 含确认类词（收到/好的/OK/明白/嗯，短英文词边界匹配防误命中品牌名）+ 长度 ≤ 阈值；超长降级为 business |
| ↳ `social.closing` 结束语 | social | 表达结束对话、暂时离开 | 含道别类词（再见/拜拜/晚安/先这样/收工/回头聊） |
| ↳ `social.polite` 招呼/礼貌 | social | 问候、客套、引起注意或致歉，无实质请求 | 含招呼/客套词（你好/在吗/请问/抱歉/哈喽/早）+ 短消息 |
| ↳ `social.compliment` 称赞/夸奖 | social | 对助手或他人的成果、能力表达赞许、惊叹，无后续执行诉求 | 含赞许类词（厉害/太棒了/666/yyds/牛啊）+ 长度 ≤ 阈值 |
| ↳ `social.smalltalk` 闲聊/寒暄 | social | 无明确诉求的日常寒暄、近况询问、拉家常，不指向任何需执行的任务 | 含寒暄类词（在忙吗/最近好吗/吃饭了吗）+ 长度 ≤ 阈值 |
| ↳ `social.emotion` 情绪/感叹 | social | 情绪化感叹、笑叹、吐槽式表达（大笑/无语/服了），无执行诉求 | 含情绪感叹类词（哈哈/无语/服了/emo/破防）+ 长度 ≤ 阈值 |

> 优先级裁决（保证互斥）：`business` > `social.gratitude` > `social.polite` > `social.compliment` > `social.acknowledge` > `social.smalltalk` > `social.emotion` > `social.closing` > 默认 `business`。
> 注：`polite` 早于 `acknowledge` 是为了让"你好"这类问候优先归为礼貌而非被单字"好"误判为确认收到（修复原实现的一个跳过 bug）；`compliment` 早于 `acknowledge` 是为了让"可以啊"这类赞许优先归为称赞而非被"可以"误判为确认收到。所有社交子型的长度阈值默认由 `pure_thank_max_length`（20）驱动（closing 用 `pure_closing_max_length`，acknowledge 用 `pure_ack_max_length`）。

### 第二层：行动意图（ACTION）—— 业务消息想要的抽象动作

> 行动意图彼此正交、可共存（一条消息可同时是 `query` + `communicate`），不做互斥约束。

| 类别 | 语义边界 | 典型触发条件 |
|------|----------|--------------|
| `action.query` 信息查询 | 获取已有信息/状态/数据，无副作用（天气/搜索/知识库/文档/通讯录/日历/审批/考勤/未读/消息记录/组织/系统状态等读取类） | 含查询/搜索/了解/疑问词，或指向某类可读信息（天气/审批/考勤/文档/股价…） |
| `action.execute` 执行操作 | 创建/修改/发送/触发，产生状态变更或副作用 | 含发送/创建/新建/安排/设置/提交/上传/提醒/预约/办理/开通等动作动词 |
| `action.analyze` 分析生成 | 对已有信息总结/对比/分析，或生成新内容（草稿/归纳/复盘） | 含总结/概括/分析/对比/生成/起草/整理/归纳/提炼/复盘等 |
| `action.communicate` 通讯会话 | 与人的消息往来、会话管理（发消息/看未读/查会话/搜记录/@某人） | 涉及给人发消息、查看/检索会话与消息记录、@ 提醒等 |
| `action.media` 媒体处理 | 上传或处理图片/文件/语音/视频等媒体素材 | 涉及上传图片/文件、发送截图/语音/视频、媒体操作 |

## 工具 → 抽象行动意图映射（中心化）

> `TOOL_ACTION_MAP`（`src/intent.py`）是新增/调整工具时的唯一改动点。

| 工具 | 服务意图 |
|------|----------|
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

## 匹配流程

### 处置层（`rule_engine` → `IntentRegistry.classify_disposition`）
1. 空消息 / 未启用 → `business`
2. 命中 `business` 证据 → `business`（覆盖社交）
3. 按优先级判定 `social` 子型，受长度阈值约束；`acknowledge` 超长降级为 `business`
4. 其余 → `business`

### 行动层 / 工具路由（`agent._keyword_match_tool_names`）
- 基础工具（send_message / save_memory / recall_memory）恒含
- 工具**有**具体场景词（`intent_keywords`）→ 仅据此精准判定（主要信号，保证 smart 路由精确性）
- 工具**无**具体场景词 → 用其声明的抽象行动意图证据词兜底（避免无关键词工具漏匹配）
- `smart` 模式：命中相关工具则精准暴露；无命中回退全量（交给主模型自选，保证不漏）
- `keyword` 模式：命中则暴露，无命中回退 FALLBACK；`all` 模式：恒全量

## 向后兼容与运维自定义

- `config.intent_filter` 中的关键词/阈值（`business_keywords` / `thank_you` / `acknowledge` / `closing` / `polite` / `pure_thank_max_length` / `pure_ack_max_length`）仍可通过 `IntentRegistry.apply_intent_filter` 覆盖默认证据词，运维无需改代码即可调整意图识别。
- 各工具 `intent_keywords`（具体场景词）保留为精准匹配信号，抽象意图层作为补充与文档化载体。

## 决策追踪（Decision Tracking）

> 代码实现：`src/decision_tracker.py`（`DecisionRecord` / `DecisionTracker`）
> 持久化：`src/memory/sqlite_store.py` — `decisions` 表

每条消息的处理决策都会被记录，形成可追溯、可查询的决策时间线。

### DecisionRecord 数据结构

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | datetime | 决策时间戳 |
| `sender` | str | 发送者名称 |
| `sender_id` | str | 发送者 ID |
| `chat` | str | 会话名称 |
| `conversation_id` | str | 会话 ID |
| `content` | str | 消息内容预览（截断至 100 字符） |
| `intent` | str | 意图分类结果（如 `business`、`social.gratitude`） |
| `action` | str | 处理动作：`skip`（跳过） / `reply-rule`（关键词规则） / `llm`（LLM处理） |
| `routing_mode` | str | 工具路由模式：`smart` / `all` / `keyword` |
| `routed_tools` | list[str] | 本轮暴露给 LLM 的工具名称列表 |
| `reply_preview` | str | 回复预览（截断至 100 字符） |

### DecisionTracker 双写机制

```
tracker.record(...)
    ├─ 写入进程内 deque（maxlen=300，近实时展示，重启清空）
    └─ 写入 SQLite decisions 表（持久化，重启不丢失）
         └─ 支持按 sender/intent/action 多维度筛选
```

- **进程级单例** `tracker`，在 `main.py` 中通过 `tracker.set_sqlite_store(store)` 注入存储后端
- `record()` 方法同时写入内存队列和 SQLite，持久化写入失败不阻塞主流程
- `recent(n)` 方法从内存读取，供首页卡片近实时展示

### 三类决策记录点（main.py）

| 场景 | action | 携带信息 |
|---|---|---|
| 意图过滤跳过（社交意图、黑名单、免打扰等） | `skip` | intent, sender, conversation, content |
| 关键词规则直接回复 | `reply-rule` | intent, sender, conversation, content, reply_preview |
| 交给 LLM 处理 | `llm` | intent, sender, conversation, content, routing_mode, routed_tools, reply_preview |

### Web UI 中的应用

- **首页「最近决策追踪」卡片**：固定高度滚动展示最近 N 条决策（通过 `GET /api/decisions` 从内存队列读取，每 5 秒轮询刷新）
- **「意图 & 路由 → 决策追踪」子页面**：分页查询持久化决策历史（通过 `GET /api/decisions/history`），支持按发送者/意图/动作筛选，含概览统计卡片和决策列表
- **决策统计**：通过 `GET /api/decisions/stats` 获取各维度计数分布
