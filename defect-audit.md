# Linkora 缺陷系统梳理（2026-08-28）

> 范围：飞书 CLI 安装/更新 bug 已在上一轮修复（commit `dcaa4bd`），本报告覆盖**其余缺陷**。
> 方法：全量测试基线 + 三个只读子系统审计（配置读写 / Web 安全 / 轮询与适配器）+ 静态扫描 + 真实 CLI 行为验证。
> 测试基线：修复前 3824 passed → 修复后 **3829 passed**（+5 新增锁死测试），2 skipped / 2 xfailed，无失败。
> 门禁：ruff / pyright（改后源文件 0 错误）/ check_deps / gitleaks 全绿。

## 一、已修复（本轮，F1–F4）

### F4 · [P0] Web 写回静默丢弃未知配置 key
- **位置**：`web/routers/config.py` → `update_config`(:638)、`update_system_prompt`(:743)、`restore_default_config`(:521)
- **触发**：`config.yaml` 含 pydantic 未声明 key（用户自加键 / 旧版遗留 / 拼写未清），任意一次 Web 保存、改提示词、恢复默认
- **预期**：写配置永不丢既有参数（P0 红线）
- **实际**：`AppConfig` 默认 `extra="ignore"`，`load_config` 构造即丢弃未知 key；`model_dump()` 写回只含已知字段 → 永久丢参
- **修复**：三处写回前以 `_load_disk_config_raw()` 原始磁盘配置为基底 `_deep_merge(raw, model_dump())`，未知 key 保留；密钥仍走 `_revert_env_masked_secrets_to_disk` 还原为磁盘原值
- **测试**：`test_update_config_preserves_unknown_keys`

### F1 · [P1] 示例占位密码未被 fail-closed 拒绝
- **位置**：`src/config_models.py:851` `_KNOWN_DEFAULT_PASSWORDS`；`web/routers/config.py:526` 内联清单
- **触发**：`cp config.yaml.example config.yaml` 不改 `web.auth_password`（=`REPLACE_WITH_YOUR_STRONG_PASSWORD`）
- **预期**：`auth_enabled=true` 且密码为已知默认值 → 拒绝启动
- **实际**：占位哨兵不在拒绝清单 → 校验通过，管理后台以公开口令运行
- **修复**：占位哨兵加入两处拒绝清单（config.py 内联清单同步）；3 个加载 example 的测试隔离 auth（`test_tool_whitelist_drift` / `test_config_manage_guard` / `test_timeout_guard`）
- **测试**：`test_placeholder_password_rejected_fail_closed` + `test_example_still_ships_placeholder`

### F2 · [P1] GET /api/config 明文回传平台适配器密钥
- **位置**：`web/routers/config.py` `get_config`(:545) platforms 循环
- **触发**：任意已登录 `GET /api/config`
- **预期**：响应脱敏所有 secret
- **实际**：仅 mask 了 llm/embedding/web；`platforms[].adapter` 的 `corp_secret`/`token`/`encoding_aes_key` 明文回传（feishu/wecom 块仅为空值 decoy）
- **修复**：循环内对 `platforms[].adapter` 调 `_redact_secrets()`
- **测试**：`test_get_config_redacts_platform_adapter_secrets`

### F3 · [P2] 轮询线程遇非预期异常永久停答
- **位置**：`src/poller.py` `run_loop`(:275) `except (RuntimeError, IMAdapterError)`
- **触发**：`poll_once`/派发链抛 `TypeError`/`KeyError`/`ValueError`/`sqlite3.Error`/`OSError` 等
- **预期**：单轮出错记日志，下一轮继续
- **实际**：异常冲出 `run_loop` 杀死轮询线程 → 该平台永久静默停答
- **修复**：`except` 放宽为 `Exception`（`KeyboardInterrupt`/`SystemExit` 属 `BaseException` 不受影响，仍可中断）
- **测试**：`test_poller_loop_survives_generic_exception`

## 二、计划中（本轮未实现，避免引入跨平台回归）

### P1 · 飞书/企微自身回复回声死循环
- **位置**：`src/platform/runtime_dispatch.py:209` `_record_reply_success`；`feishu.py` / `wecom.py` `chat_message_reply`
- **根因（已核实）**：去重标记写死 DingTalk 的 `result["result"]["openTaskId"]`；飞书返回 `data.message_id`/`identity`、企微返回 `errcode`/`noop_uuid`（`feishu.py:494` / `wecom.py:747`），`real_msg_id=None` → 只标了本地 `reply_uuid`；轮询重拉回来的平台消息（`om_xxx`）与 `reply_uuid` 不匹配。在 **bot-fallback**（飞书默认 `--as bot`，`feishu.py:612`）或 **self 检测失效**（`primary.py:350` 当前用户 ID 为空）时，自身回复被当作用户消息 → 无限回声。
- **修复方案**：各适配器 send 返回归一化 `platform_msg_id`；`_record_reply_success` 优先用它标记去重，并将 assistant 行以平台 msg_id 为键持久化，使 `_check_if_bot_message` 按 id 命中。
- **实现步骤**：① 飞书/企微 `chat_message_reply` 返回含 `platform_msg_id` 的 dict；② `runtime_dispatch` 提取并 `_mark_msg_processed`；③ `message_repo` 以平台 msg_id 存 assistant 行；④ 加单测覆盖「重拉自身回复不再应答」。
- **风险**：跨三平台派发重构，需充分单测；故先规划，单独 PR。

### P2 · 飞书/企微回复重拉后被误存为 user（历史污染）
- 同 P1 根因；修复随 P1 一并落地。

### P3 · `data/skills/.../stock_query.py` 硬编码东方财富 token
- **位置**：`data/skills/stock-price-query/scripts/stock_query.py:50` `EASTMONEY_TOKEN` 明文
- **说明**：`data/` 已被 `.gitignore` 忽略且该文件 `git ls-files` 为空（未 tracked）→ 不进仓库，非公开泄露；属本地脚本 hygiene，建议移入 config/env 并轮换 token（用户本地处理）。

### P3 · `web/api.py:832` 并发写共用临时文件名
- **位置**：`_write_config` 用 `CONFIG_PATH + ".tmp"`
- **触发**：两个 `update_config` 并发
- **修复**：`tempfile.mkstemp` 生成唯一临时名（低风险，建议顺手修）。

### P3 · Web 安全增强（非漏洞）
- `logout` 令牌不吊销（`auth_middleware:248`）；`img_token` Cookie 缺 `Secure`（`routers/image.py:161`）；多 worker 未配 `web.jwt_secret` 致令牌跨进程失效（`auth_middleware:33`）；`RBAC require_role` 无路由使用（死代码）。

## 三、审计确认干净（无需改动）
- 配置原子写（`os.replace`+`fsync`）、import 深合并保留独有键、启动未知 key 告警（`validate_config_keys`）
- 历史 ASC/DESC 顺序（`history.py` / `prompt_builder.py` 一致，无反转）
- 媒体检测 `_detect_media_kind` 覆盖飞书/企微
- SSRF（`utils/net.py` + `image.py` 钉死 IP、禁重定向、禁私网/回环/链路本地）
- JWT 用 `pyjwt` 非 `eval`；密码 `PBKDF2` + `hmac.compare_digest`
- 全量测试 3829 passed / 2 skipped / 2 xfailed
