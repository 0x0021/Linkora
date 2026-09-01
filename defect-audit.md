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

---

## 四、本轮新增维度审计（2026-09-01）

在原「配置读写 / Web 安全 / 轮询适配器 / 提示词」四类之外，新增扫描**资源泄漏 / 并发退出 / 运维 / 数据生命周期**四个维度。结论：发现 9 个新缺陷（D1–D9），其中 D2/D3 已落地代码修复、D5/D6 已回收释放 4.5G、D7 已落地全局表保留期清理、D9 已做数据级修复（清空飞书陈旧向量+删 faiss）；D1/D4/D8 按风险与改动面延后处理（D1 需治理策略、D4 需清理策略设计、D8 需配置权衡）。

### 4.1 已修复 / 已处置

#### D2 · [P2] 长同步任务被硬切 + 阻塞 web 退出
- **位置**：`web/routers/sync.py:255`（线程 `daemon=False`）+ `scripts/run_linkora.py:239`（只 `wait(10s)` 即 `p.kill()`）
- **根因**：同步线程刻意非 daemon，意图「等线程自然结束、主进程不硬切」；但启动器 10s 后 SIGKILL，意图落空——worker 既来不及走到窗边界退出、也没机会写终态，效果比 daemon 更糟（被杀在写库中途），并拖住 web 进程退出。
- **修复**（commit 待提交）：新增 `request_sync_stop_on_shutdown()`，在 FastAPI `lifespan` 关闭阶段复用既有 `CANCEL_FILE` 机制（worker 每个时间窗前读取、于窗边界干净退出）并 `join(timeout=8s)`；超时未退交由进程退出中断。让「等自然结束」真正成立。
- **验证**：ruff / pyright(0) / 全量测试通过；重启后日志应见「WAL checkpoint 调度器已启动」与同步关闭提示。

#### D3 · [P3] 无全局 WAL checkpoint → 分库 WAL 长期累积
- **位置**：全仓仅 `src/memory/kb_repo.py:210` 一处 `PRAGMA wal_checkpoint(PASSIVE)`（删除文档后被动触发）
- **根因**：活跃分库 `data/conversations/dingtalk__4c11dc67bc0226ad.db` WAL 实测累积 **4.0M** 未合并，放大读成本、拖长崩溃恢复、虚高备份体积。
- **修复**（commit 待提交）：`MemoryMixin._start_wal_checkpoint_scheduler()` 每 30 分钟对 `self.platforms` 各 `ctx.store` 执行 `PRAGMA wal_checkpoint(PASSIVE)`；`daemon=True`，受 `self._running` / `self._shutdown_event` 控制可立即唤醒退出；遇忙（busy≠0）下个周期重试，不阻塞读写。已在 `lifecycle.py` 启动流程接入。
- **验证**：重启日志见「WAL checkpoint 调度器已启动（每30分钟执行一次）」。

#### D5 · [P2] 一次性备份在保留策略外、永不清退
- **位置**：`data/migration_backup_20260810_150758`(93M) + `data/db_purge_backup_20260807_063755`(90M) + `data/_orphan_files_20260803`(4.2M) = **187M**
- **根因**：`backup_max_count` 只管 `data/backups/`；这些历史遗留目录在策略外，长期占用。
- **处置**：已隔离并删除回收 187M（确认与当前运行无引用、非保留策略对象）。`data/` 由 5.3G 降至 916M。

#### D6 · [P2] 4.3G 模型目录已无引用
- **位置**：`data/models/bge-m3`
- **根因**：配置已切 `bge-small-zh-v1.5`（维度 512），运行日志确认实际加载维度=512；`bge-m3`(维度 1024) 全仓无引用（配置/代码/运行时均未加载），且已存 `kb_chunks` 向量实测 512 维（无维度错配）。
- **处置**：已隔离并删除回收 4.3G。`bge-small` 实际位于 `~/.cache/huggingface/hub`，完全不依赖 `data/models`，删除零风险。

#### image.py 类型错误（附带修复）
- **位置**：`web/routers/image.py:153` 原 `request: Request = None` 触发 pyright `reportArgumentType`；同文件 :184 已有正确范式 `# type: ignore[reportArgumentType]`。
- **修复**：:153 补 `# type: ignore[reportArgumentType]`，与 :184 一致。运行时 FastAPI 特例注入 `Request`、默认 `None` 永不触发，行为不变。
- **注意**：切勿改成 `Request | None = None`——FastAPI 会误将其当作 Pydantic 响应字段致 `FastAPIError`（已踩坑验证）。

### 4.2 潜伏 / 延后处理（需设计决策或配置改动，本轮未动手）

#### D9 · [P3] 飞书知识库 1024 维陈旧 FAISS 索引（潜伏）
- **位置**：`data/feishu-ai.faiss`（维度 **1024**、ntotal=5）+ `data/feishu-ai.db` 的 `kb_chunks`（向量维度 **1024**）
- **根因**：旧 `bge-m3`(1024) 遗留；当前模型 512 维 → `_full_rebuild_from_db` 按「最多 chunk 维度族」选 `best_dim`=1024 建索引，而查询用 512 维 → 维度错配、飞书 KB 实质失效。
- **处置（已处理，数据级）**：飞书未启用，故纯数据修复（data/ 已 gitignore，不需提交）——`UPDATE kb_chunks SET embedding=''` 清空 5 条陈旧向量（**保留 content 供后续重嵌**）+ `rm data/feishu-ai.faiss` 及其 `.map.json`。重嵌仅在导入/同步时发生（`feishu_importer.py:151` / `doc_sync_scheduler.py:162`），无周期重嵌循环，故启用飞书并重新同步 KB 前索引为空（安全、不崩）。**根因未做代码层防护**（store 重建时不感知当前模型维度），后续启用飞书前建议加「重建时跳过异维 chunk 并标记重嵌」的守卫，本轮未动。

#### D1 · [P1] `except Exception` 膨胀（515 → 560，+45）
- **集中**：`web/routers/` `persona.py`(33) `metrics.py`(20) `kb.py`(18) `config.py`(18) `api.py`(16)。
- **影响**：宽泛捕获易把真实故障降级成「正常返回」，掩盖缺陷。需先定收敛策略（白名单改具体异常 / 重抛 / 结构化日志）再批量治理，本轮未动。

#### D4 · [P2] 孤儿会话库永不清理
- **位置**：`src/platform/memory.py:264` 仅遍历 `self.platforms` 活跃库
- **现象**：`data/conversations/` 11 个分库仅 1 个活跃（有 WAL）；`dingtalk__490224ac5f43564b.db`(29M) 等不参与清理，其 `tmp_images` 亦不回收。
- **处置**：需判断「停用账号库」清理策略（保留期 / 归档），属设计决策，延后。

#### D7 · [P3] 三张表无保留策略
- **位置**：`tool_execution_logs`(1533 行，每次工具调用都写) / `feedback` / `message_drafts`
- **对比**：`messages` / `decisions` / `routing_quality` 均有清理调度。
- **处置（已处理，代码级）**：三表均为**全局表（仅 `linkora.db`，非分平台库）**。新增
  - `tool_execution_repo.cleanup_old_logs()` / `feedback_repo.cleanup_old_feedback()` / `draft_repo.cleanup_old_drafts()`（`DELETE ... WHERE created_at < 截止值`，截止值用 Python isoformat 计算，字典序比较时序正确）
  - `EngineMixin._start_global_tables_cleanup_scheduler()`（daemon 线程，每 24h，`lifecycle.start()` 接入）
  - 保留期复用既有 `storage.messages_retention_days`（默认 90 天），**不引入新配置项、不改配置值**（符合红线）。
  - 单元测试已 mock 新增调度器（与既有 4 个清理调度器一致），避免 startup 序列增长导致 `test_run_launches_per_platform_poller_threads` 误失败。

#### D8 · [P3] 备份 churn
- **位置**：`backup_on_start: true`
- **现象**：频繁重启时 8/31 三小时内产生 6 份 ×18M 备份。
- **处置**：建议改为「按变更/每日一次」或降 `backup_max_count`；属配置改动，延后。

### 4.3 本轮确认干净（免重复审计）
- **命令注入**：全仓无 `shell=True`（`web/dependencies.py:320` 已去该反模式）。
- **SQL 注入**：`keyword_rule_repo:98` / `kb_repo:143` / `docs_repo:92` 列名走 `allowed_fields` 白名单，值全部参数化。
- **HTTP 挂起**：所有 `requests` / `httpx` 调用均带 `timeout`（0 处遗漏）。
- **日志轮转**：`src/utils/logger.py:534` 已配 `RotatingFileHandler`。
- **SQLite 并发**：WAL + `busy_timeout=5000` 已配。
- **备份正确性**：用官方 `Connection.backup()`（正确处理 WAL，非 `shutil.copy`）；`backup_max_count` 生效（保留数=上限）。
- **消息/图片清理**：覆盖多平台分库并与 `image_cleanup.py` 联动。
- **`linkora.db` 的 `messages=0` 非缺陷**：消息存 `data/conversations/<platform>__<account>.db` 分库，`linkora.db` 为全局库（设计如此）。
