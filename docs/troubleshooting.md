# 故障排查（Troubleshooting）

本页按「登录 / 权限 / 收不到 / 发不出 / 平台能力缺失」五类整理常见症状→原因→修复。所有结论均来自代码与配置真值，未做猜测。

> 通用排障顺序：① 看启动日志与各平台状态（Web 管理台「设置 → 平台」，用 `?platform=` 切换器）；② 查 CLI 版本自检结果 `data/cli_versions.json`；③ 核对 `config.yaml` 中 `platforms[].enabled` 与 `adapter.cli_path`。

---

## 1. 登录 / 凭证类

| 平台 | CLI（安装命令） | 最低版本 | 登录方式 |
|---|---|---|---|
| 钉钉 | `dingtalk-workspace-cli`（`npm i -g dingtalk-workspace-cli`） | — | `dws login`，`dws profile list` 确认目标组织 |
| 飞书 | `lark-cli`（`npm i -g lark-cli`） | **v1.0.72+** | `lark-cli login` |
| 企业微信 | `@wecom/cli`（`npm i -g @wecom/cli`） | **v0.1.9+** | `wecom-cli` 扫码登录（通常交互式） |

**症状 → 修复**：

- **启动报 CLI 未找到 / 拉不到消息**：对应平台 CLI 未安装或未登录。Linkora 启动时有后台线程尝试**自动安装 + 自动升级**（`AUTO_INSTALL` / `AUTO_UPDATE` 默认开启），失败仅记日志、**不阻塞启动**，因此建议手动装好并登录。CLI 解析优先级：先 `PATH`，其次常见路径（`lark-cli` / `wecom-cli` 还会查 `/opt/homebrew/bin`、`/usr/local/bin`；`dws` 走 `PATH`）。
- **钉钉跨组织报 `TOKEN_VERIFIED_FAILED`**：该组织尚未开启 CLI 数据访问权限，框架会自动弹 OAuth 引导授权。若频繁被跨组织会话干扰，用 `poller.target_org_corp_id` 锁定单一组织。

---

## 2. 权限类

- **Web 后台登不进**：`web.auth_password` 不能为空（空值会导致配置校验失败，`AppConfig()` 无法实例化）。连续失败达 5 次（`_AUTH_MAX_FAILS`，窗口 `_AUTH_FAIL_WINDOW=300s`）将封禁 300s（`_AUTH_BLOCK_SECONDS`），可通过环境变量调整。详见 [`security.md`](./security.md)。
- **工具调用报权限错误**：适配器返回 `IMAdapterPermissionError`（飞书错误包形如 `{"ok": false, "error": {"code": ..., "message": "..."}}`，错误以 JSON 打到 stdout、退出码 1）。通常是登录账号缺少对应权限范围（审批 / 通讯录 / 文档需相应授权），到对应平台后台给 CLI 账号补权即可。

---

## 3. 收不到消息

- **`platforms[].enabled: false`**：该平台轮询器未启动。改为 `true` 后**热重载**生效。
- **`poller.interval_seconds` 过大**：调小拉取间隔。
- **数据库未初始化 / 适配器未连通**：看启动日志中的 DB init 与适配器连接输出。
- **钉钉跨组织干扰**：见 §1，用 `poller.target_org_corp_id` 锁定组织。

---

## 4. 发不出 / 调用报错

- **`adapter.dry_run: true`**：适配器只拼命令不真正发送，用于调试。需要真发时改为 `false`。
- **门控拦截（预期行为，非故障）**：钉钉 / 飞书在「真人已回复 / 已接管」时会触发**已读闸门**，AI 不再补刀、不再浪费 Token。Web 管理台可看到门控命中日志；这是设计内的人机协同，不是 bug。
- **平台能力缺失 → 运行时报错**（显式门控，非静默失败）：
  - 在**飞书**调用 `get_calendar_events` / `create_todo` → 飞书适配器未实现 `calendar_event_list` / `todo_task_create`（保持 `NotImplementedError` 桩）→ 当前不支持，改用钉钉 / 企微或换其他工具。
  - 在**企业微信**调用 `get_conversation_info` → 企微适配器未实现 `chat_conversation_info` → 当前不支持。
  - 在**飞书 / 企微**调用钉钉专属工具（`send_ding` / `send_message` / `approval_*` / `wiki_*` / `search_contact` / `get_attendance` / `get_minutes` 等）→ 被平台门控过滤，不可用。
- **飞书 CLI 噪声**：`lark-cli` 可能在 JSON 外吐安装提示 / 进度条等非 JSON 日志，基类 `BaseIMAdapter` 会自动剥离，不影响解析。
- **企微启动 WARNING「当前平台不支持已读回执」**：启动日志出现 `[wecom] 当前平台不支持已读回执，suppress_when_owner_read / mark_read_after_process 将不生效`。含义：企微 CLI 无已读回执能力，**这不是报错、不影响自动回复**；只是 `suppress_when_owner_read`（已读闸门：老板已读就闭嘴）与 `mark_read_after_process`（处理完消未读红点）在企微上**静默无效**（基类为空操作，不抛异常）。处置二选一：① 想消除告警，把该平台块下 `poller.suppress_when_owner_read` 与 `poller.mark_read_after_process` 均设为 `false`；② 想保留「人工介入就不插嘴」的保护，改用 `poller.owner_present_cooldown_seconds`（真人在场时间窗，不依赖已读回执，三平台都生效）。

---

## 5. 平台专属能力缺失速查表

| 平台 | 当前不支持的能力 | 表现 | 建议 |
|---|---|---|---|
| 飞书 | 日历（`get_calendar_events`）、待办（`create_todo`）、文档（`doc_*`） | 调用即运行时报错 | 用钉钉 / 企微；或回避该工具 |
| 企业微信 | 会话信息（`get_conversation_info`）、文档（`doc_*`）、**已读闸门 / 真人接管** | 前两者调用即报错；后两者**静默失效**（启动期有 WARNING，见 §4） | 需「接管」场景用钉钉 / 飞书 |
| 飞书 / 企微 | 主动外呼（`send_ding` / `send_message`）、OA 审批、Wiki、通讯录、考勤、AI 听记 | 门控不可用 | 这些为**钉钉专属**；自动回复不受影响 |

> 完整矩阵与根因见 [`platform-capabilities.md`](./platform-capabilities.md)。标注「当前不支持」是平台 / 适配器固有限制，**不是「即将支持」**。

---

## 6. 数据隔离与启停

- 各平台**物理隔离**：独立数据库 `./data/<id>-ai.db`（钉钉默认 `./data/linkora.db`）、独立轮询器。
- 临时停用某平台：把对应 `platforms[].enabled` 改为 `false` 保存（热重载生效），数据库与历史保留。
- **配置热重载**：多数 `config.yaml` 改动自动生效；仅 `llm` / `embedding` / `rag` / `poller` 相关变更需重启进程。

---

## 7. 还找不到答案？

- 能力矩阵与根因：[`platform-capabilities.md`](./platform-capabilities.md)
- 安全边界与权限：[`security.md`](./security.md)
- 分步接入三平台：[`getting-started-platforms.md`](./getting-started-platforms.md)
- 全部配置项：[`configuration.md`](./configuration.md)
