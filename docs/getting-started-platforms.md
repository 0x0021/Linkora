# 多平台接入指南

Linkora 可同时接入**钉钉、飞书、企业微信**三大 IM 平台。每个平台拥有**独立的适配器、独立数据库、独立轮询器**，数据物理隔离、互不影响，可任意组合启停。

本指南是「分步接入」手册（配置参考详见 [`configuration.md`](./configuration.md)）。三种平台的差异只在 **CLI 安装、登录、最小 `platforms[]` 配置** 三处，其余 LLM / RAG / 工具 / 技能配置完全通用。

> ⚠️ 各平台能力范围不同，接入前请先阅读 [`platform-capabilities.md`](./platform-capabilities.md)：OA 审批 / AI 听记为钉钉专属（设计内）；飞书暂不支持日历 / 待办、企业微信暂不支持已读标记 / 会话信息 / 置顶会话（当前平台限制，非「即将支持」）。

## 0. 通用前置

```bash
# 1. 获取代码
git clone https://github.com/0x0021/Linkora.git
cd Linkora

# 2. 安装依赖（Python ≥ 3.14）
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock

# 3. 生成配置
cp config.yaml.example config.yaml

# 4. 至少填好 LLM（llm.api_key / llm.base_url / llm.model）与 Web 后台密码（web.auth_password 不能为空）
```

通用配置要点：
- `platforms` 是运行期**唯一真源**。钉钉默认已在 `platforms[0]` 配好；飞书 / 企微需在列表内追加条目并 `enabled: true`。
- 顶层 `dws` / `poller` / `storage` 为 **legacy 兼容段**，仅供 `config_manage` 工具读取状态，请与 `platforms` 中对应平台的值保持一致。
- 修改 `config.yaml` 后通常**自动热重载**；仅 `llm` / `embedding` / `rag` / `poller` 相关变更需重启。

## 1. 钉钉（默认已启用）

钉钉适配通过 **DWS CLI** 对接，是开箱即用的默认平台。

1. 安装并登录 DWS CLI（见 DWS 官方文档），确保 `dws profile list` 能列出目标组织。
2. `config.yaml` 中 `platforms[0]` 已是钉钉示例，确认：
   ```yaml
   platforms:
     - id: dingtalk
       display_name: 钉钉
       enabled: true
       adapter_type: dingtalk
       storage: { type: sqlite, path: ./data/linkora.db }
       adapter: { cli_path: dws, dry_run: false }
   ```
3. 启动 `python main.py`，观察日志中 DWS 认证与数据库初始化。

**常见坑**：跨组织会话会触发 `TOKEN_VERIFIED_FAILED` 并自动弹 OAuth；可用 `poller.target_org_corp_id` 锁定单一组织避免干扰。

## 2. 飞书

飞书适配通过 **lark-cli**（v1.0.72+）对接。

1. 安装 `lark-cli` 并登录（确保 `lark-cli` 在 `PATH` 中）。
2. 在 `config.yaml` 的 `platforms` 列表内追加飞书条目（取消 `config.yaml.example` 中 `feishu` 段的注释并改 `enabled: true`）：
   ```yaml
   platforms:
     - id: feishu
       display_name: 飞书
       enabled: true
       adapter_type: feishu
       storage: { type: sqlite, path: ./data/feishu-ai.db }
       poller: { interval_seconds: 10 }
       adapter: { cli_path: lark-cli, dry_run: false }
   ```
3. 启动后飞书将独立于钉钉建立自己的库与轮询器。

**平台限制（当前）**：飞书**不支持**日历查询（`get_calendar_events`）与待办创建（`create_todo`）——调用这些工具会运行时报错；知识检索与大部分工具可用。

## 3. 企业微信

企业微信适配通过 **wecom-cli**（v0.1.9+）对接。

1. 安装 `wecom-cli` 并登录。
2. 在 `platforms` 列表内追加企微条目：
   ```yaml
   platforms:
     - id: wecom
       display_name: 企业微信
       enabled: true
       adapter_type: wecom
       storage: { type: sqlite, path: ./data/wecom-ai.db }
       poller: { interval_seconds: 10 }
       adapter: { cli_path: wecom-cli, dry_run: false }
   ```
3. 启动后企微独立建库与轮询。

**平台限制（当前）**：企业微信**不支持**已读标记（`mark_read`）、会话信息（`chat_conversation_info`）、置顶会话（`chat_list_top_conversations`），因此「已读闸门 / 真人接管」类人机协同特性在企微上不生效；文档列表（`doc_list`）亦不可用。

## 4. 验证与切换

- 管理台「设置 → 平台」可查看各平台状态与 `?platform=` 切换器。
- 启动后各平台数据库分别位于 `./data/<id>-ai.db`（钉钉默认 `./data/linkora.db`）。
- 想临时停用某平台：把对应 `platforms[].enabled` 改为 `false` 并保存（热重载生效），数据库与历史保留。

## 5. 下一步

- 配置 LLM / RAG / 工具：见 [`configuration.md`](./configuration.md)
- 平台能力对照与已知限制：见 [`platform-capabilities.md`](./platform-capabilities.md)
- 权限与安全：见 [`security.md`](./security.md)
- 故障排查：见 [`troubleshooting.md`](./troubleshooting.md)
