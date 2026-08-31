# Linkora 缺陷系统梳理（持续维护，截至 2026-08-31）

> 范围：覆盖配置读写 / Web 安全 / 轮询与适配器 / 提示词行为四类缺陷；飞书 CLI 安装包名与更新解析 bug 已在更早一轮修复（commit `dcaa4bd`）。
> 方法：全量测试基线 + 三个只读子系统审计（配置读写 / Web 安全 / 轮询与适配器）+ 静态扫描 + 真实 CLI 行为验证 + 线上日志取证。
> 测试基线：8-28 轮修复前 3824 passed → 3836 passed；8-31 三轮修复后 **3911 passed / 2 skipped / 2 xfailed**（collect 3915）。
> 门禁：ruff / pyright（改后源文件 0 错误）/ check_deps / gitleaks 全绿。

---

## 一、已修复

### 8-28 轮次（commit `74ce921` 配置/安全/轮询，`a5cfe96` 回声环）

#### F4 · [P0] Web 写回静默丢弃未知配置 key
- **位置**：`web/routers/config.py` → `update_config`(:638)、`update_system_prompt`(:743)、`restore_default_config`(:521)
- **触发**：`config.yaml` 含 pydantic 未声明 key（用户自加键 / 旧版遗留 / 拼写未清），任意一次 Web 保存、改提示词、恢复默认
- **预期**：写配置永不丢既有参数（P0 红线）
- **实际**：`AppConfig` 默认 `extra="ignore"`，`load_config` 构造即丢弃未知 key；`model_dump()` 写回只含已知字段 → 永久丢参
- **修复**：三处写回前以 `_load_disk_config_raw()` 原始磁盘配置为基底 `_deep_merge(raw, model_dump())`，未知 key 保留；密钥仍走 `_revert_env_masked_secrets_to_disk` 还原为磁盘原值
- **测试**：`test_update_config_preserves_unknown_keys`

#### F1 · [P1] 示例占位密码未被 fail-closed 拒绝
- **位置**：`src/config_models.py:851` `_KNOWN_DEFAULT_PASSWORDS`；`web/routers/config.py:526` 内联清单
- **触发**：`cp config.yaml.example config.yaml` 不改 `web.auth_password`（=`REPLACE_WITH_YOUR_STRONG_PASSWORD`）
- **预期**：`auth_enabled=true` 且密码为已知默认值 → 拒绝启动
- **实际**：占位哨兵不在拒绝清单 → 校验通过，管理后台以公开口令运行
- **修复**：占位哨兵加入两处拒绝清单（config.py 内联清单同步）；3 个加载 example 的测试隔离 auth（`test_tool_whitelist_drift` / `test_config_manage_guard` / `test_timeout_guard`）
- **测试**：`test_placeholder_password_rejected_fail_closed` + `test_example_still_ships_placeholder`

#### F2 · [P1] GET /api/config 明文回传平台适配器密钥
- **位置**：`web/routers/config.py` `get_config`(:545) platforms 循环
- **触发**：任意已登录 `GET /api/config`
- **预期**：响应脱敏所有 secret
- **实际**：仅 mask 了 llm/embedding/web；`platforms[].adapter` 的 `corp_secret`/`token`/`encoding_aes_key` 明文回传（feishu/wecom 块仅为空值 decoy）
- **修复**：循环内对 `platforms[].adapter` 调 `_redact_secrets()`
- **测试**：`test_get_config_redacts_platform_adapter_secrets`

#### F3 · [P2] 轮询线程遇非预期异常永久停答
- **位置**：`src/poller.py` `run_loop`(:275) `except (RuntimeError, IMAdapterError)`
- **触发**：`poll_once`/派发链抛 `TypeError`/`KeyError`/`ValueError`/`sqlite3.Error`/`OSError` 等
- **预期**：单轮出错记日志，下一轮继续
- **实际**：异常冲出 `run_loop` 杀死轮询线程 → 该平台永久静默停答
- **修复**：`except` 放宽为 `Exception`（`KeyboardInterrupt`/`SystemExit` 属 `BaseException` 不受影响，仍可中断）
- **测试**：`test_poller_loop_survives_generic_exception`

#### F5 · [P1] 飞书/企微自身回复回声死循环（回声环根因修复）
- **位置**：`src/platform/runtime_dispatch.py` `_record_reply_success`（原 line 209 写死 `result["result"]["openTaskId"]`）
- **根因**：bot 持久化 assistant 行用本地 `reply_uuid` 作 `msg_id`，而下一轮轮询拉回自身消息的 `msg_id` 是平台真 id（飞书 `om_xxx` / 钉钉 `openMessageId` / 企微 `msgid`），二者永不相等 → self 检测（`_check_if_bot_message`）只能退守 content+time 兜底（±120s），时间/格式偏差即漏判 → 无限回声。`_record_reply_success` 对飞书/企微 `real_msg_id=None`，只标了本地 `reply_uuid`。
- **修复**：新增 `_extract_platform_msg_id(result, reply_uuid)` 归一化提取——钉钉 `result.openTaskId` / 飞书 `data.message_id`（兼容扁平 `message_id`）/ 企微 `noop_uuid`（=reply_uuid 退化）；并用该平台真 id 同时①持久化 assistant 行 `msg_id`、②`_mark_msg_processed` 去重标记。钉钉/飞书拿到真 id 后，下一轮拉回自身消息 `msg_id` 直接命中 `messages` 表第一道防线（`role='assistant'`），从根消除回声环；content+time 兜底保留为第二道防线。
- **附带修复 P2**：飞书/企微回复重拉后被误存为 user（历史污染）——随本修复一并落地（平台真 id 持久化 + `_check_if_bot_message` 按 id 命中）。
- **测试**：`tests/test_reply_self_dedup.py` 7 项（`_extract_platform_msg_id` 三平台形态 + `_record_reply_success` 持久化/去重用平台真 id、企微 fallback）；既有 `test_poller.py::test_check_if_bot_message_msg_id_match_takes_priority` 已覆盖 msg_id 命中优先。

### 8-31 轮次

#### F6 · [P0] 历史消息重放导致答非所问（commit `d176796`）
- **现场**：对方报「VDI 更新后黑屏」，AI 回「收到，问题已解决就好。」—— 投喂里混进了 **8-25 18:00** 的「桌面分配失败」旧截图。
- **根因**：查 `dedup_messages` 发现那 4 条 8-25 消息的 `processed_at` 全是 8-31 13:58:36（同一毫秒批量补标），即 **当天去重漏标 → 今天被 list-all 重放**，与当天真消息合并成同一轮投喂；LLM 把新故障认成已完结的旧话题。
- **三道防线（数据层）**：
  1. 新增 `poll_new_message_max_age_hours`（默认 24h），`_max_new_message_age_days` 取 `min(history_days, 该值/24)`。`history_days` 是上下文取数窗口（7 天合理），不再拿来判定「消息够不够新」，杜绝几天前的老消息被当新消息重放。`poller_strategy` / `poller_core_discovery` 年龄门槛改用新阈值。
  2. `message_loop._drop_stale_messages_in_batch` 在防抖合并成轮前剔除远超最新消息时间戳（>1 天）的旧记录；正常连发、时间戳缺失一律保守保留，绝不丢用户真消息。
  3. `poller_core_ocr` 的 caption 兜底不再把 `[图片消息](mediaId=...) 注意：如需下载使用dws命令下载` 整串误判为「随图文字」灌进 LLM 和消息库（含 dws CLI 指令 / base64 噪声）。
- **行为层兜底**：`system_prompt` 新增故障口径指令（core 末尾 + 完整 prompt 末尾双压）—— 围绕现象给排查方向、不替对方下「已解决」结论、不把过去某次结论当成这次结论。
- **配置同步**：live `config.yaml`（已先备份 `data/config-backups/`）与 `config.yaml.example` 均加 `poll_new_message_max_age_hours: 24`。
- **测试**：OCR 噪声清理 18 项 + 年龄门槛 + 防抖重放防护 + 故障口径指令，共 30+ 项回归。

#### F7 · [P2] 长回复排版混乱（commit `c890647`）
- **现场**：IM 平台（钉钉/企微）长回复常出现编号挤同行、加粗随意、无段落间距、代码块缺语言标注，手机端难以扫读。
- **修复**：`system_prompt` 新增 `_REPLY_FORMAT_DIRECTIVE`（core 末尾近因位）+ `_REPLY_FORMAT_REMINDER`（完整 prompt 末尾，防 few-shot 原话往回拽），约束 6 条规范：标题层级统一（`##`/`###`）、列表逐项独占一行（`1. ` 格式）、段落间空一行、长内容用列表分块（勿用表格）、代码块标语言 + 反引号标记术语、视觉节奏统一。短回复（≤3 行）不受约束。
- **测试**：`tests/test_system_prompt_fault.py` 5 项。
- **性质**：质量/UX 增强，非崩溃或安全缺陷。

#### F8 · [P3] 并发写配置共用临时文件名
- **位置**：`web/api.py` `_write_config`（原 `tmp_path = CONFIG_PATH + ".tmp"`）
- **触发**：两个 `update_config` 并发 → 共用同一 `.tmp` 互相覆盖，可能写出半截配置
- **修复**：`tempfile.mkstemp(dir=目标目录)` 生成唯一临时名，`os.replace` 原子替换，`finally` 清理崩溃残留；`import tempfile`
- **测试**：既有 `_write_config` 集成路径覆盖；手动验证文件名唯一

#### F9 · [P3] 登出不吊销令牌 → 实现黑名单 + 路由
- **位置**：`web/auth_middleware.py` `logout`（原空 `TODO`，且根本没接到任何路由 → token 永不吊销）
- **修复**：模块级内存黑名单 `_revoked_token_hashes`（存令牌 SHA-256）；`verify_token` 校验时拒绝命中；`logout(token)` 真实吊销合法令牌、非法/空返回 False；新增 `POST /api/logout` 路由提取 `Bearer` 调 `logout`。
- **已知限制**：黑名单为进程内内存集合，多 worker 部署下各 worker 独立（登出仅本 worker 生效）；令牌本身 24h 过期，对本地管理工具可接受。
- **测试**：`tests/test_web_auth.py` `test_logout` 语义更新 + `TestTokenRevocation` 2 项

#### F10 · [P3] `img_token` Cookie 缺 `Secure`
- **位置**：`web/routers/image.py` `issue_image_token`（仅 `httponly=True, samesite="lax"`，未 `secure=True`）
- **修复**：`set_cookie` 加 `secure=`，按 `request.url.scheme` 推断（仅 HTTPS 置），避免本地 HTTP（localhost 开发）下图片 Cookie 被浏览器丢弃致图片加载失败
- **测试**：`tests/test_image_cookie_secure.py` 2 项（HTTP 不加 / HTTPS 加）

#### F11 · [P3] `RBAC require_role` 死代码清理
- **位置**：`web/auth_middleware.py` `require_role` 装饰器定义后零处使用（grep 全仓仅定义；RBAC 实际由 `api.py` 内联 `if role != ROLE_ADMIN` 生效）
- **修复**：移除死代码 + 清理因此变未用的 `functools.wraps` / `typing.Callable` 导入；无行为变化

#### F12 · [P3] 引用回复降级频发 → 静默化
- **位置**：`src/platform/runtime_dispatch.py` `_send_single_chat_reply` / `_dispatch_reply_send`
- **现象**：日志中 `topic_quote_guard_unavailable` 反复出现（8-31 样本 6 次），该账号（郭建辉单聊）每条回复都走「尝试原生引用回复 → 守卫拦截 → 降级为分片发送」并刷 ERROR
- **根因**：DWS 引用回复内部校验 `convThreadEnabled`（话题圈），而单聊永远不是话题圈 → 必被守卫拦截后降级。单聊引用气泡本就无信息增量
- **修复**：单聊直接跳过原生引用回复（走普通发送，回复永不丢失）；群聊保留尝试，但话题圈守卫拒绝降级为 `debug`（不再刷 warning）
- **影响**：消息送达不受影响；ERROR 日志噪声消除

#### F13 · [P3] 东财 token 硬编码 → 环境变量
- **位置**：`data/skills/stock-price-query/scripts/stock_query.py:50`（**未 tracked 本地文件**，`git ls-files` 为空 → 不进仓库，非公开泄露）
- **修复**：`EASTMONEY_TOKEN = os.environ.get("EASTMONEY_TOKEN", "")`，缺失则名称解析优雅降级跳过；属本地 hygiene
- **注意**：该文件未入库，改动仅作用于本地运行实例，**不进提交**

---

## 二、待处理 / 计划中（按优先级）

### 已知限制（非阻塞，按需处理）
- **多 worker 登出黑名单一致性**：`/api/logout` 黑名单为进程内集合，多 worker 部署下登出仅本 worker 生效（`web.jwt_secret` 已在 live config 配置，跨进程令牌签发一致性已满足；黑名单跨进程一致需外接 Redis，当前不引入）。单 worker 本地管理工具不受影响。
- **东财 token 轮换**：`data/skills/.../stock_query.py` 的 `EASTMONEY_TOKEN` 仍建议用户本地设环境变量并视情况轮换（F13 已消除硬编码，仅剩 hygiene 收尾）。

---

## 三、审计确认干净（无需改动）
- 配置原子写（`os.replace`+`fsync`）、import 深合并保留独有键、启动未知 key 告警（`validate_config_keys`）
- 历史 ASC/DESC 顺序（`history.py` / `prompt_builder.py` 一致，无反转）
- 媒体检测 `_detect_media_kind` 覆盖飞书/企微
- SSRF（`utils/net.py` + `image.py` 钉死 IP、禁重定向、禁私网/回环/链路本地；CodeQL 误报以 `codeql-config.yml` 忽略 net.py + 排除 full-ssrf 抑制，防护由 32 个 net 单测兜底）
- JWT 用 `pyjwt` 非 `eval`；密码 `PBKDF2` + `hmac.compare_digest`
- 全量测试 **3911 passed / 2 skipped / 2 xfailed**（collect 3915）
