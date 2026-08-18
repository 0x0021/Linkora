# 平台能力对照（Platform Capabilities）

Linkora 可同时接入**钉钉、飞书、企业微信**三大 IM 平台。三者共享同一套 LLM / RAG / 规则引擎 / 技能 / Web 管理台，但因为底层 CLI 适配器的能力不同，**部分工具与「人机协同」特性在各平台上的可用性存在差异**。

本页是**用户向**能力矩阵。所有结论均来自代码单一真源：

- 工具级可用性 = `src/tools/registry.get_builtin_tool_platforms()`（遍历 `BUILTIN_TOOL_MANIFEST`，读取每个工具的 `platforms` 属性）。
- 适配器级能力 = 各 CLI 适配器实际实现的方法（`src/im_adapter/feishu.py`、`src/im_adapter/wecom.py`、`src/dws_adapter/`）。

> ⚠️ **措辞约定（决策 3）**：下文标注「当前不支持」之处，需区分两类：
> - **工具级门控**（§2 矩阵中的 ✗）：工具在目标平台被显式门控，执行层会**显式报错**，不会静默失败。
> - **适配器能力缺失**（§3 的 `mark_read` / `chat_conversation_info` 等）：基类为空实现，**静默降级**（返回空结果、不抛异常）——因此这类缺失在 v0.3 起由启动期 WARNING 显式告知，见 §3。

---

## 1. 三平台一致的核心能力

以下能力在三平台上**完全一致**，不受适配器差异影响：

- **RAG 知识检索**（`kb_search`）、**消息搜索**（`search_messages`）、**长期记忆**（`save_memory` / `recall_memory`）
- **天气**（`get_weather`）、**联网搜索**（`web_search`）、**系统状态**（`system_status`）
- **配置管理**（`config_manage`）、**未读汇总**（`get_unread`）、**个人资料**（`get_my_profile`）、**消息统计**（`message_stats`）、**关键词规则**（`keyword_rules`）
- **自动回复管线**：轮询拉取 → 规则 / 意图分流 → LLM 生成 → 原路回发，在三平台均可用（飞书 / 企微的「自动回复发回」走各自适配器，与钉钉一致）

> 注：工具 `platforms` 属性为空列表 `[]` 表示「全平台可用」（基类 `BaseTool.platforms` 默认值），**不是漏配**。上述 12 个平台无关工具即属于此类。

---

## 2. 工具级能力矩阵

下表按能力分组列出各平台是否可用。✓ = 可用，✗ = 当前不支持（显式门控或适配器无对应方法）。

| 能力分组 | 工具（name） | 钉钉 | 飞书 | 企业微信 |
|---|---|:---:|:---:|:---:|
| **OA 审批**（7） | `approval_list_forms` / `approval_list_pending` / `approval_list_initiated` / `approval_list_executed` / `approval_list_tasks` / `approval_get_detail` / `approval_search_forms` / `transfer_approval` | ✓ | ✗ | ✗ |
| **组织与考勤**（3） | `get_attendance` / `get_current_org` / `list_orgs` | ✓ | ✗ | ✗ |
| **文档**（2） | `search_doc` / `get_doc_content` | ✓ | ✗ | ✗ |
| **AI 听记**（2） | `get_minutes` / `list_minutes` | ✓ | ✗ | ✗ |
| **通讯录**（1） | `search_contact` | ✓ | ✗ | ✗ |
| **主动发消息**（2） | `send_ding` / `send_message` | ✓ | ✗ | ✗ |
| **媒体 / 图片**（1） | `upload_image` | ✓ | ✗ | ✗ |
| **知识库 Wiki**（4） | `wiki_space_list` / `wiki_space_search` / `wiki_node_list` / `wiki_node_search` | ✓ | ✗ | ✗ |
| **日历 / 待办**（2） | `get_calendar_events` / `create_todo` | ✓ | ✗ | ✗ |
| **会话信息**（1） | `get_conversation_info` | ✓ | ✓ | ✗ |

**根因（适配器级，已逐一核对代码）**：

- 飞书适配器（`feishu.py`）**未实现** `calendar_event_list` / `todo_task_create` / `doc_list` → 故日历、待办、文档类工具在飞书不可用（工具层门控已隐藏）。
- 企业微信适配器（`wecom.py`）：`calendar_event_list` / `todo_task_create` / `doc_search` / `doc_read` 因企微 CLI 无对应能力而显式报错 / 空实现，故 `get_calendar_events` / `create_todo` 已对企微门控隐藏（`platforms=["dingtalk"]`）；`chat_conversation_info` / `chat_list_top_conversations` / `mark_read` 未实现 → 会话信息类工具在企微不可用。
- 钉钉（DWS）适配器能力最完整，上述全部支持。

> 工具门控与适配器能力完全对应：`get_calendar_events` / `create_todo` 因企微 CLI 不支持，门控为 `["dingtalk"]`（仅钉钉）；`get_conversation_info` 因企微适配器缺 `chat_conversation_info`，门控为 `["dingtalk","feishu"]`。Web 管理台会按当前平台过滤意图与工具映射，不会向不可用的平台暴露对应工具。

---

## 3. 人机协同 / 门控特性

「已读闸门 / 真人接管 / 发送前复核」依赖适配器的 `mark_read` + `chat_conversation_info` + `chat_list_top_conversations` 三个方法识别人工介入状态：

| 方法 | 钉钉 | 飞书 | 企业微信 |
|---|:---:|:---:|:---:|
| `mark_read` | ✓ | ✓ | ✗ |
| `chat_conversation_info` | ✓ | ✓ | ✗ |
| `chat_list_top_conversations` | ✓ | ✓ | ✗ |

**结论**：

- **钉钉、飞书**：人机协同门控完整生效——真人已回复 / 已接管时，AI 不再补刀、不再浪费 Token。
- **企业微信**：三种适配器方法均未实现，且基类实现是**空操作**（`src/im_adapter/base_adapter.py` 中 `mark_read` / `chat_conversation_info` / `chat_list_top_conversations` 直接返回空 dict，不抛异常）——因此「人工介入即停」这一层保护在企微上**静默失效**：自动回复仍照常工作（按规则 / 置信度生成并回发），只是少了「人工已回复就别插嘴」的识别，且链路不留错误痕迹。

  **v0.3 起显式可见**：若企微平台配置了 `poller.suppress_when_owner_read` 或 `poller.mark_read_after_process`（任一为 `true`），启动期会打印一条 WARNING：`[wecom] 当前平台不支持已读回执，suppress_when_owner_read / mark_read_after_process 将不生效`（实现见 `src/im_adapter/wecom.py` 的 `warn_read_signal_unsupported`，由 `src/platform/primary.py` 在适配器构造后调用）。这不是报错、不影响自动回复。处置见 [`troubleshooting.md`](./troubleshooting.md)。

---

## 4. 主动发消息工具 vs 自动回复

`send_ding` / `send_message` 是**钉钉专属的「主动外呼」工具**（LLM 可主动调用向会话发消息）。这**不意味着**飞书 / 企微「不能发消息」：

- 飞书 / 企微的**自动回复**（响应入站消息）走各自适配器的回发管线，与钉钉一致，不受该门控限制。
- 仅「LLM 主动发起新会话 / 主动推送」这类场景在飞书 / 企微上暂不可用。

---

## 5. 设计说明：为什么采用「显式门控」

平台能力差异通过**显式门控**处理（决策 3）：

- 工具声明 `platforms` 属性，Web 按平台过滤意图与路由映射；
- 不可用平台调用被门控工具时**运行时明确报错**，而不是静默成功或乐观假设全支持。

优点：用户能立即知道某能力在当前平台不可用，避免「调了没反应」的黑洞体验；门控清单集中在工具类，单一真源易审计。代价：能力矩阵需随适配器补全而更新（见 §6 排障与 §2 根因）。

---

## 6. 相关文档

- 分步接入三种平台：见 [`getting-started-platforms.md`](./getting-started-platforms.md)
- 分平台故障排查：见 [`troubleshooting.md`](./troubleshooting.md)
- 权限与安全边界：见 [`security.md`](./security.md)
- 全部配置项：见 [`configuration.md`](./configuration.md)
- 工具完整清单与参数：见 [`tools.md`](./tools.md)
