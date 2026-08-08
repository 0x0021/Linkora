# 更新日志 · Changelog

本项目以内部协作方式演进，提交采用中文 `type(scope)` 规范（见 README「贡献指南」）。
本文件记录近期主要变更；更早的架构演进与设计文档见 `docs/` 下各 `*-design.md` / `*-audit*.md`。

---

## 2026-08-08 — 全面审计修复（P0/P1/P2）

> 项目完整性/一致性审查后的修复轮，覆盖依赖声明、Web 安全、启动超时、CI 门禁与测试盲区；本轮追加性能优化（H1–H4）。
> **全量回归 3384 通过（2 skipped / 2 xfailed），pyright 基线维持 94，ruff 全绿**。

### 依赖与构建（HIGH）
- **fix(deps)**: `web/routers/kb.py` / `src/tools/parse_document.py` / `src/memory/sqlite_store.py` 实读 `bs4` 但 requirements/pyproject/lock 全缺，导致「从 URL 导入知识库」直接 500。补 `beautifulsoup4==4.15.0`（requirements.txt + pyproject.toml 文档解析段）并重生成 `requirements.lock` / `uv.lock`。
- **fix(deps)**: `scripts/lock_deps.sh` 的 `PY_FLOOR="3.12"` 与 pyproject `requires-python>=3.14` 不一致，本地重生成锁后 CI 变红 → 改为 `3.14`。

### 配置与安全（MEDIUM/HIGH）
- **docs(env)**: `.env.example` 移除个人绝对路径；补 `HF_TOKEN` 与 `LLM_SECONDARY_FALLBACK_API_KEY/BASE_URL/MODEL`（代码实读但模板缺席）。
- **fix(web)**: `GET /api/config` 脱敏遗漏 `llm.secondary_fallback_api_key`（仅 `_SECRET_KEYS` 有、GET 侧漏 → 明文回传）；补 `_mask` 并注释「新增 llm.*_api_key 须同步两处」。
- **fix(web)**: `web/dependencies.py` `datetime.utcnow()` → `datetime.now(timezone.utc)` 消 3.12+ 弃用告警；新增 `_BG_TASKS` 强引用池 + `_spawn_bg()`，后台 task 不再被 GC 丢弃。
- **fix(web)**: `web/routers/image.py` 同样 `_BG_TASKS` + `_spawn_bg`，防图标下载 task 被 GC 致 `safe_name` 永久卡 `_downloading`（永远走 SVG 兜底）。
- **fix(config)**: `main.py` 补全 `__all__`（原仅 10 个，导致其余 32 再导出被 ruff 判 F401；CI lint 不含根 main.py 故长期不可见），现 52 符号全可解析。

### 启动健壮性（P0）
- **fix(platform)**: `primary._init_primary_components`（SQLiteStore 初始化）与 `runtime_setup._resolve_own_open_dingtalk_id`（oid 解析）的 `future.result(timeout=N)` 超时保护在 `with ThreadPoolExecutor` 的 `shutdown(wait=True)` 下形同虚设（已复现：超时分支触发但主线程仍卡到 worker 结束）。抽出共享原语 `src/platform/_timeout.py::run_with_timeout`（显式 executor + `shutdown(wait=False, cancel_futures=True)`），两处改用它；超时/异常不再阻塞启动。超时常量提为模块级 `_DB_INIT_TIMEOUT=30` / `OPEN_DINGTALK_ID_RESOLVE_TIMEOUT=60` 便于测试注入。
- **fix(wecom)**: `src/im_adapter/wecom.py` `run()` 在 `subprocess.run` 后补 `returncode` 判定，镜像 base.py（非 0 → `_classify_error`；负数信号 → `_shutdown_error_class()`），CLI 崩溃不再被当「成功无数据」。

### 业务与配置收敛（P1/P2）
- **refactor(intent)**: `src/intent/registry.py` 移除 `TOOL_ACTION_MAP` 中已删除实现类的孤儿映射 `get_my_approvals` / `get_approval_detail`（40=38+2，仅 registry 引用）。
- **fix(web)**: `web/routers/dead_letters.py` 批量重放单条异常改回 `safe_detail(e)` 常量文案，不再 `str(e)[:200]` 把异常内部文本（路径/密钥/堆栈）回传响应体，复用 `web/errors.py` 脱敏 helper。
- **ci(deps)**: `.gitlab-ci.yml` 由 `py39`+`py313` 双矩阵收敛为单一 `test:py314` / `python:3.14-slim`（原两版本均不满足 `>=3.14`）；`scripts/check_deps.py::check_environments` 新增扫描 `.gitlab-ci.yml` 与 `scripts/lock_deps.sh` 的 Python 下限，依赖门禁不再盲。
- **docs(config)**: `config.yaml.example` 补全 feishu/wecom 完整注释块（`enabled: false` + `adapter.cli_path`），照启用为 copy-edit，CI 安全。

### 文档规范
- **docs(changelog)**: 合并 `2026-08-07` 同日 4 个重复 `## ` 小节为单一日期下的 `### ` 子节；`## v0.2.0 (2026-08-07)` 发布块从原中段位置移回日组末尾（`---` 分隔符前），恢复「一日期一 `##`、release 独立 `## vX`」规范。

### 测试加固（P2-3）
- **test(platform)**: 新增 `tests/test_timeout_guard.py`（7 例）覆盖 `run_with_timeout` 成功/超时非阻塞/直接断言 `shutdown(wait=False, cancel_futures=True)` 契约/异常降级，及两处调用点超时行为；`tests/test_web_routers_dead_letters.py`（3 例）覆盖批量重放异常脱敏。填补 `primary.py` / `runtime_setup.py` / `dead_letters.py` 测试盲区。

### 性能优化（H1–H4）

> 仅优化、不改外部行为。逐条按「用户可感知卡顿 → 平台轮询延迟 → 核心热路径」排序落地，均附行为保持测试。

- **perf(web)**: `web/routers/status.py`、`web/routers/conversations.py`、`web/routers/orgs.py` 中原本直接在 async 视图里调用的阻塞操作（DWS CLI 身份解析 `_get_current_profile_local` / `contact_user_get_self`、subprocess `git` 版本采集、飞书/钉钉 `list_orgs`）统一经 `run_in_threadpool` 移出事件循环，避免单请求阻塞整个 asyncio 事件循环（H1）。`status._get_git_info` 加 `functools.lru_cache(maxsize=1)`，版本号进程内固定、免重复 fork 子进程。
- **perf(gate)**: `src/platform/runtime_inbound.py` 消息热路径门控（前置过滤 + 发送前复核）原先每消息对 `has_user_message_from` 重复查库 4 次（`_has_user_taken_over` / `_is_owner_present` 各算两遍）。改为前置过滤一次性算出 `taken_over` / `owner_present` 并透传给 `_reply_gate_reason`（新增可选形参），重复查询减半；发送前复核 `_should_reply_now` 不传参 → 实时重算，保留生成期竞态保护（绝不跨 pre-LLM / send-time 复用旧值）（H2）。
- **perf(poller)**: `src/poller_strategy.py::_feishu_correct_chat_type` 每群每轮都调 `dws.chat_conversation_info`（subprocess CLI），在 `_build_group_list_all_cache`（遍历所有群）与 `_fetch_conversation_messages` 中被重复触发。新增按 `conv_id` 的每轮内存缓存（`Poller.__init__` 加 `_feishu_conv_info_cache`，`poll_once` 开头清空），同一会话单轮只打一次 CLI（H3）。
- **perf(memory)**: `src/memory/memory_repo.py::recall_memory` 与 `check_memory_duplicate` 原先对 `memories` 全表 `fetchall` + 逐行 `json.loads` + `cosine_similarity` + Python 排序，记忆量大时拖慢召回/去重热路径。新增按 `created_at` 倒序的候选上限 `_MEMORY_CANDIDATE_CAP=500`（模块常量，配 `idx_memories_created` 索引），先截断到最近一批候选再算相似度；记忆表通常很小，cap 内行为完全不变，超大表退化为「近期优先」（符合个人记忆场景）（H4）。
- **test(perf)**: 新增 `tests/test_perf_fixes_2026_08_08.py`（17 例）覆盖四类优化——H1（用户/版本解析 helper 与三个端点断言值正确、git 信息 lru_cache）、H2（`_reply_gate_reason` 传入标志后不再重复查库、缺省仍各算一次）、H3（同 conv_id 单轮 CLI 仅 1 次、跨 conv_id 再打、poll 轮清空缓存）、H4（recall/去重 SQL 含 `ORDER BY created_at DESC LIMIT ?` 且上限参数正确、小表召回 top_k 排序正确）。

### 前端性能优化（F-H1/F-H2/F-H4/F-H5）与缺陷修复

> 依据 2026-08-08 前端性能审查报告的高 ROI 项落地；另修 RAG 记忆页滚动条 bug 与类型检查漂移。
> **全量回归 3390 通过（2 skipped / 2 xfailed；1 例 `test_web_search_utils::test_queries_cross_backend_merging` 为 SearXNG 联网依赖、与本轮改动无关、环境性失败）**。

- **fix(web,css)**: RAG「AI 记忆管理」页列表无法滚动、标题栏多出多余滚动条。根因：`#rag-tab-memory` 缺省走全局 `.panel-body{overflow:auto}`，且 `.panel` 无 `flex:1;min-height:0` 无法填满 tab 高度 → 外层 `.section-tab-content` 接管滚动（视觉上即「标题栏带滚动条」），`.table-wrap` 自身无独立纵向滚动。对齐已验证的 `#rag-tab-documents` 修复：`.panel` 填高、`panel-body` 改 `overflow:hidden`、`.table-wrap` 独占 `overflow-y:auto`，filters/rules 固定不压缩（`web/static/css/pages/rag.css`）。
- **perf(web,cache)**: `web/api.py::VersionedStaticFiles` 缓存判定修正——生产态 `dist/` 内容哈希单 bundle（哈希在文件名、URL 无 `?v=`）原先误落入 `else` 分支被设 `no-cache` 且**删除 ETag/Last-Modified**，导致连 304 都走不了、比开发态更差。改为「`?v=` 或路径含 `dist/` 或匹配 `bundle.<hex>.`」→ `immutable`；其余未版本化资源（vendor/fontawesome）保留 ETag/Last-Modified 走 `no-cache`，允许 304 协商（F-H1）。
- **perf(web)**: 全链路 `GZipMiddleware(minimum_size=1024)`（`web/api.py`），HTML/JSON API/静态 bundle 一并压缩，首屏文本体积约降 75–86%（F-H2）。
- **perf(web,image)**: `/api/image/` 鉴权由 URL 拼 `?it=` token 改为后端下发 **HttpOnly Cookie(img_token)**（`web/routers/image.py::issue_image_token` 设 Cookie、`serve_image` 优先读 Cookie 兼容 `?it=` 回退）；前端 `imgTokUrl` 不再把 token/platform 拼进 URL，图片地址稳定 → 浏览器可长期缓存、消除每 4 分钟 token 轮换引发的整屏图片重下；`serve_image` 补 `Cache-Control: private, max-age=300`（FileResponse 自带 ETag 支持 304）（F-H4）。
- **perf(web,search)**: 死信/草稿/日志三个搜索框 `oninput` 直接触发整页服务端重载（但过滤本就是客户端），中文 IME 下 8–10 次/键击 = 8–10 次全量请求。套 `debounce(300)`（`index.html` 改用 `debouncedLoadDeadLettersPage`/`debouncedLoadDraftsPage`，`logs.js` oninput 包 `debounce`），请求收敛到输入停顿后一次（F-H5）。
- **fix(web,js)**: `app.js` 同步求值时 `window.debouncedLoadDraftsPage = debounce(loadDraftsPage, 300)` 抛 `ReferenceError: Can't find variable: loadDraftsPage`。根因：`drafts.js` 是 `type=module`（defer 执行），晚于普通 `<script>` 的 `app.js` 执行，导致 `loadDraftsPage` 尚未暴露到全局。把 `debouncedLoadDraftsPage` 的创建移到 `app.js::init()`（DOMContentLoaded 后，此时 module 已执行），死信页（`dead_letters.js` 为普通脚本、先执行）不受影响。

### 前端性能优化（F-H6 仪表盘三路轮询合并为单通道）

> 仪表盘原先对 `/api/logs`(2s) / `/api/decisions`(5s) / `/api/messages`(5s) 三路独立 `setInterval` ≈ 54 req/min，且 `loadDashboardData` 内又单独 fetch 一次 decisions，存在冗余。合并为单端点 + 单 `setInterval`(5s) ≈ 12 req/min，请求量降约 78%。

- **feat(web)**: 新增聚合端点 `GET /api/dashboard/stream-data`（`web/routers/dashboard_live.py`，注册于 `web/api.py`），一次性返回三路增量数据；复用 `logs.get_logs` / `decisions.recent_decisions` / `conversations.messages` 现有逻辑，不重复实现业务。`get_logs` 返 `JSONResponse` 故 `body` 经 `bytes(...).decode` 解析（`json.loads` 不接受 `memoryview`），类型门禁维持 94。
- **perf(web,js)**: `dashboard.js` 抽出 `applyNewMessages` / `applyRealtimeLogs` 复用渲染；新增单通道 `fetchDashboardStream()`（带 `last_message_id` / `last_log_id` 增量游标）+ `startDashboardLivePolling` / `stopDashboardLivePolling`。删除废弃的 `startRecentMessagesPolling` / `stopRecentMessagesPolling` / `startDecisionPolling` / `stopDecisionPolling` / `startRealtimeLogPolling` / `stopRealtimeLogPolling` 及其 interval 变量（`app.js` 全部调用点改指单通道；登录态切换 `showLoginOverlay` / `doLogin` 统一停 `stopDashboardLivePolling`，防 401 风暴）。
- **test(web)**: 新增 `tests/test_web_dashboard_live.py`（3 例）覆盖三路合并、消息增量游标（`last_message_id` 只回传新增、`max_message_id` 取最大）、游标透传（`last_log_id→since`、`decisions_platform→platform`、`platform=all`）。
- **fix(web,type)**: `web/routers/conversations.py:193` H1 重构时误删 `platform = get_current_platform()` 赋值，导致 `backfill_missing_image_path(..., platform)` 引用未定义 `platform` → pyright 95 > 基线 94。改为 `get_current_platform()` 直接调用，CI 类型检查恢复 94（与基线持平）。
- **fix(web,type)**: F-H4 将 `serve_image` 签名加 `request: Request = None` 以承载 Cookie 鉴权，但 `None` 不能赋给非 Optional 的 `Request` 类型 → pyright 比基线多 1（95）。保留 FastAPI 标准写法（`request: Request` 由框架注入、`= None` 仅用于非请求上下文直调），补 `# type: ignore[reportArgumentType]` 压住该误报，pyright 回到 94 基线；`Optional[Request]` 会令 FastAPI 在路由注册时把 `Request|None` 当 Pydantic 字段建模而抛 `FastAPIError`，故不可取。
- **test(frontend)**: 新增 `tests/test_frontend_perf_fixes_2026_08_08.py`（7 例）覆盖 F-H1（`dist/` 哈希 bundle→immutable、vendor 未版本化→`no-cache` 且保留 ETag/Last-Modified、带 `?v=`→immutable）与 F-H4（token 下发 HttpOnly Cookie、Cookie 优先 + `?it=` 回退、成功响应带 `private` 缓存头、缺 token→401）。
### 前端性能优化（F-H7 Chart.js 按需懒加载）

> Chart.js(`/static/vendor/chart.umd.min.js`, ~205KB) 原先在 `index.html` 用 `<script defer>` 直接拉，每页首屏都下载，无图表页面（日志/设置/草稿）也跟着付带宽。

- **perf(web,js)**: 新增 `core/util.js::loadChart()`——首次调用动态注入 `<script src="/static/vendor/chart.umd.min.js">`，`Promise` 缓存，`window.Chart` 就绪后 resolve；重复进入不再重复下载。`index.html` 删除该 eager `<script defer>` 标签。
- **refactor(web,js)**: 11 处 `new Chart` 渲染函数（dashboard/messages/intent 各 1、metrics 5、cost_quality 3）改为 `async` 并在 `new Chart` 前 `await window.loadChart()`，且把加载置于 canvas 空态早退之后——仅在确有图表时才拉 Chart.js。各函数均 fire-and-forget，调用方不依赖同步返回值，回归风险可控。

### 前端性能优化（F-H8 FontAwesome 双字重子集）

> `all.min.css`(FontAwesome Free 7.3.0, 90169B) 含全量图标 + brands 字型，但应用仅用 72 个图标（solid+regular 双字重、`fa-regular` 在 persona/simulate/routetrace/index 多处使用、0 brands），且大部分 base utility/keyframes 未用。生成双字重子集替换，体积 -63%。

- **perf(web,css)**: 新增 `scripts/gen_fa_subset.py`——扫描 `web/static` + `web/templates` 全部 `fa-*` token（跳过 `fontawesome/` 自身与 `dist/`、`build/` 产物，避免把被裁 CSS 的图标名又收回来），按规则裁剪 `all.min.css`：保留 solid/regular 双 `@font-face`（含 FontAwesome 5/4 兼容面，字节极小）+ 仅 72 个在用图标 `.fa-NAME{--fa:...}` + 仅用到的 base utility（如 `fa-spin` spinner）+ 仅用到的 `@keyframes`，丢弃 brands `@font-face` 与全部未用图标/工具类/动画。产出 `web/static/fontawesome/fa-subset.min.css` = 33042B（**-63%，省 ~57KB/请求**），校验 72 个在用图标**零缺失**。
- **perf(web,html)**: `index.html` 的 FontAwesome `<link>` 由 `all.min.css` 切到 `fa-subset.min.css`；删除上一轮「solid-only」废弃子集 `solid-subset.min.css`（仅含 solid 字型、`fa-regular` 会缺字，前提有误）。

### 前端性能优化（F-H3 图片缩略图 + WebP）

> OCR 原图常为数 MB 的 PNG（曾观测 11MB），而前端容器仅 320px，原图直出严重浪费带宽。服务端按 `?w=` 按需生成缩略图、按 `Accept`/显式 `?fmt=` 输出 WebP，前端 `srcset` 配合 1x/2x；灯箱仍看原图。

- **perf(web)**: `web/routers/image.py::serve_image` 新增可选查询参数 `?w=<width>`（目标最大宽度，等比不放大，上限 2000px 防滥用）+ `?fmt=webp|jpeg|png`（缺省按 `Accept` 协商，浏览器通常 image/webp）；Pillow 阻塞操作经 `run_in_threadpool` 移出事件循环。**无 `w`/`fmt` → 原图直出路径完全不变** → 灯箱放大/向后兼容/既有测试零破坏。路径穿越护栏保留（仍 403）。
- **perf(web,cache)**: 缩略图落盘缓存于 `<image_temp_dir>/.thumbs/<rel>__w<w>.<ext>`，按原图 mtime 判定新鲜度，重复访问走缓存/304；`src/memory/image_cleanup.py::purge_orphan_images` 删除原图时**连带清理 `.thumbs` 变体**（保持磁盘泄漏防护约定不变）。
- **perf(web,js)**: `messages.js` 两处对话图 + 卡片图改为请求 `?w=&fmt=webp` 缩略图，加 `srcset`(1x/2x retina)+`sizes`，灯箱(`onclick`)打开原图(`/api/image/<rel>` 无 `w`)保留放大能力；重建 bundle `bundle.8cf1a86195eb.js`（CSS 哈希不变 `c7a77f28d3df`）。
- **test(web)**: 新增 `tests/test_image_thumbnail.py`（8 例）覆盖 `_make_thumb` 缩放/WebP/不放大/JPEG 压平 alpha、`serve_image` 集成（`w`+`fmt`→WebP 变小、内容协商 PNG、无参→原图、路径穿越仍 403、缩略图落盘）、`purge_orphan_images` 清理 `.thumbs`。pyright=94=基线。

### 前端性能优化（记忆列表分页）

> 记忆量大时原列表一次拉取 `limit=200` 全量渲染、无翻页；新增后端 offset 分页 + total 计数 + 前端分页条。

- **perf(web)**: `web/routers/memories.py::memories` 新增 `offset:int=0` 入参，调用新增的 `src/memory/memory_repo.py::count_memories_filtered`（复用抽取出的 `_build_memories_where` 公共 WHERE 子句，与 `get_memories_filtered` 同源避免漂移），返回 `{memories, total, limit, offset}`。`get_memories_filtered` 增加 `offset` 形参并改 `LIMIT ? OFFSET ?`，默认 0 向后兼容（既有调用方/测试不受影响）。
- **perf(web,js)**: `api.js::getMemories` 转发 `offset`；`rag.js::loadMemoryList` 维护 `_memoryPage`/`_MEMORY_PAGE_SIZE=20` 状态，按页请求并渲染 `#memory-pager` 分页条（复用 `.marketplace-pager` 样式：首页/上一页/页码窗口/下一页/末页 + `共 N 条 · 第 X/Y 页`）；筛选/重置回到第 1 页，末页删空自动回退首屏。重建 bundle `bundle.1726717f676c.js`（CSS 哈希不变 `c7a77f28d3df`）。
- **test(web)**: `test_web_async.py::test_memories_list_shape_unchanged` 同步新响应结构（mock `count_memories_filtered`）；`test_sqlite_store.py` 新增 `test_pagination_offset_and_count` 验证 offset 切片/两页不重叠/越界空/`count` 按筛选计数。`test_web_api_endpoints.py::TestMemories` 9 例全过（仅取 `memories` 字段）。pyright=94=基线。

## 2026-08-07

- **docs(tools)**: 重写 `docs/tools.md`——内置工具数 27→38（补齐 AI 听记 list/get_minutes、钉钉知识库 wiki_space/wiki_node 共 4 个、OA 审批查询 approval_* 共 7 个）；速率限制整表以 `config.yaml.example` 为准（send_message 30 / create_todo 20 / web_search 50 / get_weather 30 / transfer_approval 10 等），纠正原先 128/512 等错误数值。
- **docs(architecture)**: Python 版本 3.11+→3.14+（仅 3.14 系列）；路由模块 30→29、端点 150+→153；架构图内置工具数 27→38。
- **docs(design)**: 翻新核心模块表——`src/intent.py`→`src/intent/`、`src/config.py(1030行)`→`src/config_models.py`+`src/config.py` 兼容重导出层、`src/dws_adapter.py`→`src/dws_adapter/`；删除不存在的 `src/auth_monitor.py`（认证在 `auth_org.py`）；poller 补充分拆现状；§9 工具清单改为指向 `tools.md`（消除双份漂移）；删除 `processed_msg_ids` 表条目（实际为进程内内存缓存）；Python 3.13+→3.14+。
- **docs(deployment)**: Python 版本 3.9+→3.14+；构建命令 `./build.sh`→`./docker/build.sh`。
- **docs(web-api)**: 端点 150→153；删除 `processed_msg_ids` 表；`/api/intents` 示例 `tools_count` 27→38。
- **docs(faq)**: `send_message` 速率限制示例 128→30 次/小时。
- **docs(configuration)**: `tools` 分组补 `kb_search_enabled` 参数。
- **docs(memory)**: 删除 `processed_msg_ids` 表条目；移除 `memory.retrieval` 下错误的 `top_k: 5`（实际属 `embedding.top_k`）。
- **docs(frontend_module_map)**: 工具统计端点 `/api/tool-stats`→实际 `/api/metrics/tools`。
- **docs(intent-model)**: `src/intent.py`→`src/intent/`；`main.py` 注入点改为 `src/platform/`。
- **docs(DEV_GUIDE)**: Python 版本 3.12+→3.14+。
- **docs(BINARY_PACKAGING_PLAN)**: 构建环境 / Docker 基础镜像 / CI `python-version` 的 Python 3.13 统一改为 3.14（与 `pyproject.toml` 及 CI 矩阵一致）。
- **docs(readme)**: 移除 `README.md` 顶部对 `docs/banner.png` 的图片引用（该资源已缺失、GitHub raw 返回 404），保留 badges 与导航之间单一 `<br>` 分隔。

### 文档站（Pages）商业级重设计

- **feat(docs)**: 重写 `docs/index.html` 落地页，从原 Apple 极简 PPT 吸附风（信息架构偏平）升级为商业产品级水准。设计语言统一为品牌渐变 **indigo→cyan**（沿用 `public/index.html` 管理台既立品牌色），深色为基底 + aurora 光晕 + 玻璃拟态 + 细网格背景。
- 新增 `docs/assets/site.css`（设计系统：双主题 tokens、玻璃组件、aurora/网格背景、滚动进场与微交互动画、完整响应式）与 `docs/assets/site.js`（主题三态切换 light/dark/system 首帧防闪、IntersectionObserver 滚动进场 stagger、磁性按钮、导航玻璃态、移动菜单、数字计数、轻量 canvas 粒子背景——尊重 `prefers-reduced-motion` 与页面可见性）。
- 信息架构：玻璃导航+主题切换 → Hero（渐变标题+对话流 mock）→ 指标条（38 工具/39 意图/3 平台/15 页）→ 核心能力网格 → 工作原理管道图 → 多平台物理隔离 → 人工接管门控演示（把「双重校验」做成产品卖点）→ 快速开始 → 文档矩阵 → CTA → 页脚。
- 约束遵守：全程规避 Jekyll/Liquid 语法（{% raw %}`{{`/`{%`{% endraw %}）以免 Pages 构建失败；保留 `docs/` 既有 `.md` 并互链；未新增 `docs/index.md`（避免与静态首页抢路径）。
- **fix(docs)**: 修复文档中残留 Jekyll/Liquid 模板字面量导致的 Pages 构建失败。
- **fix(docs)**: 修复 `body` 未应用 `--bg`/`--text` token 导致暗黑模式未真正渲染、整页呈浏览器默认白色的 bug；深色模式现在为默认首屏体验。
- **refactor(docs)**: 重写 `docs/assets/site.css` 视觉系统，从「高对比繁杂商业风」收敛为「深色优先、干净高级」：更克制的 aurora/网格、更精致的卡片层级与留白、light 模式同步精致化而非简单反色。
- **feat(docs)**: 主题切换改为分段按钮（浅色 / 深色 / 跟随系统），提升可发现性；默认主题改为 `dark`，首次访问直接呈现品牌深色主视觉。
- **style(platform)**: 消除 `src/platform/runtime_inbound.py` 已读闸门哨兵 `_unread_conv_unknown` 赋值的 ruff `B010`（`setattr` 常量属性名）告警，改为普通属性赋值；行为不变。
- **feat(frontend)**: `scripts/build_frontend.mjs` 新增 `--watch` 监听模式（Node 内置 `fs.watch` 递归监视 `web/static`，防抖 120ms 自动重建 `web/static/dist`）；`package.json` 增加 `build:frontend:watch` 脚本。前端源码（`web/static/css`、`web/static/js`）变动后本地自动出编译产物，免手动构建即可调试。
- **fix(wecom)**: 企业微信 `850003`「消息」能力授权过期，原仅打印技术栈报错（`authorization expired ... e=850003`）且每轮询刷屏；改为实例级人话提醒日志（含机器人 ID、授权链接与「无需重启、下一轮轮询自动恢复」说明），并按 10 分钟最小间隔降噪（轮询高频场景不再刷屏）；授权恢复后打印一条「已恢复」日志。配套单测 `TestAuthExpiredLogging`（判定 / 消息构造 / 降噪间隔 / 恢复通知 / 端到端）覆盖。
- **feat(frontend)**: 仪表盘「系统概览」次级指标卡片（关键规则 / 知识库文档 / 钉钉文档 / 长期记忆 / 路由质量）由上下堆叠改为横向紧凑布局：icon 居左、value+label 居右，整体高度降低约 40%；保留圆角卡片、品牌渐变条 hover 高亮、彩色 icon 与深色/light 双主题质感，解决用户反馈的「占用过高」问题。
- **docs(docs)**: 更新落地页 Hero 区聊天 mockup 示例对话：将「考勤异常」场景替换为「北京公司 9 楼打印机 IP 直连」场景；联系人由「吴锦明」改为「冯晶」；AI 回复给出具体 IP（10.0.80.21）与 Windows 添加打印机步骤；为 `.demo__body` 增加 `max-height: 420px` 与优雅滚动条，避免长回复撑变形 Hero 布局。
- **style(docs)**: 精简 Hero 聊天 mockup 的 AI 回复气泡（长步骤改为一行核心信息），并优化布局：`.msg` 气泡 `max-width` 92%→88%、`line-height` 1.6、加微阴影；bot 气泡左侧加品牌色边；`.demo__body` `max-height` 420px→380px 更紧凑。
- **docs(docs)**: Hero 主文案重写为更口语化、人性化的表达：标题「让 AI 分身，接管你的日常沟通」；新增一行平台覆盖提示「群聊、私聊都在线 · 钉钉、飞书、企业微信全覆盖」；正文改为「灵桥把 AI 分身带到团队每天使用的沟通平台。它能理解企业知识、调用工具完成工作，并与真人无缝协作，让沟通和业务持续运转。」（新增 `.hero__desc` 段落样式，信息与视觉层次更清晰）。

### Pages 部署迁移到自定义 GitHub Actions 工作流

- **ci(pages)**: 新增 `.github/workflows/deploy-pages.yml`，将 Pages 部署从「分支部署（GitHub 默认 `pages-build-deployment` 工作流，内部使用 `checkout@v4` / `upload-artifact@v4`，触发 `Node.js 20 is deprecated` 告警）」改为「GitHub Actions 部署」。改用 `checkout@v7` / `configure-pages@v6` / `jekyll-build-pages@v1.0.13` / `upload-pages-artifact@v5` / `deploy-pages@v5`，消除该弃用告警。仓库 Pages `build_type` 由 `legacy` 切到 `workflow`。

### 会话门控重设计（发送前复核 + 已读闸门）与 CI 依赖安装加速

> 现象：**人工正在沟通、消息已读的会话里，机器人仍插话自动回复**。
> 排查后定位为「两层门控之间存在生成期竞态」+「已读信号在历史重构中被摘除且无替代」。
> 本轮做门控重设计并重接 DWS 已读信号；**38 项门控相关测试 + 84 项相关回归全绿，pyright 维持基线 94**。

### 门控失效根因（排查结论）

| 编号 | 根因 | 证据 |
| --- | --- | --- |
| R1 | 「已读则不回」闸门 `reply_single_only_when_unread` 在历史重构中被整体移除，**无任何替代** | `config_models.py` / `poller_strategy.py` / `poller_core_discovery.py` 三处相关代码已删净 |
| R2 | **门控只在消息入站时判一次**，`_send_reply` 只复查已读回执/退避/限流/冷却，不复查「真人在场 / 人工接管」 | LLM 生成耗时 5~30s，人工在此窗口内回复 → bot 仍抢先发出（**本次主因**） |
| R3 | `owner_present_cooldown_seconds` 为固定 600s 窗口，粒度粗 | 长会话中易误判 |
| R4 | `_has_user_taken_over` 是「比手速」的被动检测（bot 5s 轮询 vs 人工打字） | 代码注释自认此局限 |
| R5 | `current_open_dingtalk_id` / `current_user_id` 为空时，接管与在场检测**静默返回 False**（等于门控整体失效） | 身份解析失败时无任何告警 |

### 发送前最后一刻复核（核心修复，HIGH）
- **fix(gate)**: 新增 `InboundMixin._should_reply_now(message)` 作为**发送前的权威裁决**，并在 `ReplyDispatchMixin._send_reply` 真正投递前调用；不通过则 `_mark_inbound_processed` 后 `return False`（标记已处理，避免该消息被轮询反复重试刷屏）。这直接关闭 R2 的生成期竞态——**LLM 生成期间人工已回复的，bot 不再补刀**。
- 裁决按优先级短路：`消息来自自身` → `人工已接管` → `真人在场` → `DWS 判定已读`，任一命中即放弃发送，并各自打 `[门控] 发送前复核：…` 日志便于事后追责。
- 已确认所有回复路径都汇聚于 `_handle_message_with_rid` → `_send_reply`，不存在绕过该复核的第二出口。

### 重接 DWS 已读闸门（MEDIUM）
- **feat(gate)**: 新增 `_owner_conversation_is_read` / `_unread_conversation_ids`，基于 DWS `chat_message_list_unread_conversations` 判断「本会话是否已无未读」。**保守语义**：只有会话不在未读列表中才判为已读并抑制回复；对方一旦有新消息，会话会重新回到未读列表 → 照常回复，以此规避历史事故「bot 回复后会话移出未读、对方追问再不回填导致漏回」。
- 未读集合带 **30s TTL 缓存**，避免热路径高频调用 DWS；查询异常时保守放行（不抑制）并保留旧缓存，防止 DWS 抖动误杀正常回复。
- **feat(config)**: 新增 `poller.suppress_when_owner_read`（默认 `true`），同步写入 `config.yaml.example`；若所在环境 DWS 未读状态失真、出现漏回，可置 `false` 单独关闭该闸门。

### 身份解析失败的 fail-safe 暴露（LOW）
- **fix(gate)**: `_has_user_taken_over` / `_is_owner_present` 在 `current_open_dingtalk_id`、`current_user_id` 均为空时，改为**打一次性 WARNING** 再返回 False（R5）。此前是彻底静默，门控整体失效却毫无痕迹，属于典型的「安静失败」。
- **fix(gate)**: `_is_message_from_self` 改为防御性取值（`getattr` 兜底 `sender_id`/`sender_name`/`raw`），因发送前复核会以更宽松的消息对象调用它，缺字段时按「非自身」处理而不是抛异常。

### 测试
- **test(reply_gate_sendtime)**: 新增 `tests/test_reply_gate_sendtime.py`（11 例），覆盖 `_should_reply_now` 各闸门组合、`_owner_conversation_is_read` 三态（不在未读列表=已读 / 在未读列表=未读 / 查询异常=保守放行），以及两条**行为级**用例：门控在发送前失败时 `_send_reply` 返回 False、`_mark_inbound_processed` 被调用且 `_dispatch_reply_send` **未**被调用；门控通过时正常发送。
- **chore(types)**: `engine_mixins_base.py` 补 `_should_reply_now` / `_owner_conversation_is_read` / `_unread_conversation_ids` 抽象桩，使跨 mixin 调用不产生新的 pyright `reportAttributeAccessIssue`（类型基线维持 94）。

### CI 依赖安装加速
- **ci(ci)**: `test` 与 `type-check` 两个作业的依赖安装由 `pip install -r requirements.lock` 改为 **`uv pip install --system`**，并用 `astral-sh/setup-uv@v9.0.0` 的 `enable-cache` 缓存。此前 `setup-python` 的 `cache: pip` 只缓存 pip 的**下载目录**，每次仍需重新解析+解包安装；uv 缓存的是已解包产物，命中后整段依赖安装从 ~1-2min 降到 ~10-30s。
- 显式设置 `cache-dependency-glob: requirements.lock`（setup-uv 默认 glob 只匹配 `*requirements*.txt`，不含本项目的 `.lock` 后缀），并按作业设 `cache-suffix` 隔离缓存。
- action 版本**钉全版本号**（与 ruff/pyright 同策略）：setup-uv 自 v8 起不再发布 `v8`/`v9` 这类浮动大版本别名标签，写 `@v9` 会解析失败。
- `pip-audit` 安装同步改用 uv。`lint` 作业只装单个 ruff wheel，收益有限，保持 pip 不动。

### 门控规则速查（当前设计）

| 优先级 | 闸门 | 触发条件 | 判定点 |
| --- | --- | --- | --- |
| 0 | 自身消息过滤 | sender 为本账号 | 入站 + 发送前 |
| 1 | 回复冷却 / 退避 / 限流 | 见 `runtime_reply_guard` | 入站 + 发送前 |
| 2 | 去重 `_has_replied_after` | 该消息之后 bot 已回过 | 入站 |
| 3 | 人工接管 `_has_user_taken_over` | 该消息之后本人已手动回复 | 入站 + **发送前（新增）** |
| 4 | 真人在场 `_is_owner_present` | 冷却窗口内本人有发言 | 入站 + **发送前（新增）** |
| 5 | DWS 已读闸门（新增） | 会话不在 DWS 未读列表中 | 入站(前置过滤) + 发送前 |

### 双重校验·前置过滤（08-07 续）
- **enhance(gate)**: 将门控判定抽为共用方法 `InboundMixin._reply_gate_reason(message)`，返回当前应抑制回复的闸门原因（自身/接管/在场/已读）或 `None`；`_should_reply_now`（发送前复核）与新增的**前置过滤**共用同一套逻辑，保证两道校验语义绝对一致，杜绝「两处逻辑漂移」。
- **enhance(gate)**: 在 `_handle_message_with_rid` **进入 LLM 之前**新增前置过滤——命中 `_reply_gate_reason` 任一闸门即 `return`、**不调 LLM**、并 `_mark_inbound_processed` 标记已处理，避免无效 Token 消耗。这正是用户日志中「先吃 7704 token、发送前才被已读闸门拦下」的反面：会话在入站时已读 → 直接跳过 LLM。
- 形成 **「前置过滤（省 Token）+ 发送前复核（并发兜底）」双重校验**：前置过滤抓「到达时已是已读/已回复」的常态；发送前复核抓「LLM 生成期间人工状态变化」的竞态。二者共用 `_reply_gate_reason`，任一命中都放弃发送。
- 新增 `tests/test_reply_gate_sendtime.py` 的 `_PrefilterHost` 行为级用例：门控命中时 `_process_llm_reply` **未**被调用、`_mark_inbound_processed` 被调用一次；门控放行时正常进入 LLM。并补全 `_reply_gate_reason` 六态单测。
- **config**: 按用户授权，将 `suppress_when_owner_read: true` **新增写入 live `config.yaml`** 三个平台 poller 段（dingtalk/feishu/wecom）——此前仅改了 `config.yaml.example`。旧的死键 `reply_single_only_when_unread` 未删除（遵循「删除参数需人工确认」红线）。

---

### 业务逻辑查缺补漏（账号隔离/会话清理/配置模板/死代码）

> 继 8/6 缺陷修复轮后，继续梳理业务主流程（消息入站→意图识别→工具调度→回复→持久化），
> 核实并修复 5 处真实缺陷/坏味道；**179 项相关回归测试全绿**。

### 账号隔离与可移植性（MEDIUM）
- **fix(account_identity)**: `_CLI_FALLBACKS["dingtalk"]` 原硬编码开发者机器绝对路径 `…/bin/dws`；非开发者机器若 `dws` 不在 PATH，该路径不存在 → `_find_cli` 返回 None → 钉钉账号恒解析为 `dingtalk:unknown` → **所有钉钉账号共用一个命名空间，per-account 隔离被破坏**。改为去掉绝对路径、保留 `which` 发现，并支持 `DWS_BIN` 环境变量兜底；`_find_cli` 跳过空候选。

### 会话库清理（LOW，休眠缺陷）
- **fix(conversation_repo)**: 单删 `delete_conversation` 原只删 `conversations` 行，漏 messages/conversation_summaries/dedup_messages 与本地图片，违背项目「删消息须连带删图」铁律（其唯一生产调用方 `poller_core_access` 已注释停用，故当前休眠，但重新启用即漏孤儿数据）。改为直接复用 `delete_conversations` 的级联清理逻辑，保证两路径行为一致。

### 资源泄漏（LOW）
- **fix(sqlite_store_conn)**: `conv_conn` 同线程同平台换账号时，直接覆盖 `(tid, platform)` 旧连接、从不 `close()`，导致 fd/WAL 句柄泄漏（与 `_CACHE` 永不过期耦合，极少触发）。覆盖前先关闭旧连接。

### 配置模板幽灵工具（LOW）
- **fix(config)**: `config.yaml.example` 的 `tools.available` 与 `rate_limit` 残留 2 个已移除工具 `get_my_approvals` / `get_approval_detail`（无实现类，CI 漂移测试此前只校验默认/live 值不校验 example 而漏网）。已从 example 移除，避免照模板新建配置时启动报「无对应工具」警告。

### 死代码清理（INFO）
- **refactor(poller)**: `_dispatch_one` 的 `finally` 中 `if msg.raw.get("merged"):` / `else:` 两分支体完全相同，删除无意义的 `merged` 分支，统一标记已处理。

### 测试加固
- **test(tool_whitelist_drift)**: 新增 `test_example_config_has_no_unknown_tool_entries`，校验 `config.yaml.example` 的 `tools.available` / `rate_limit` 不含非 manifest 幽灵条目且全部在 `TOOL_ACTION_MAP` 有映射，把"审批工具收敛漏改 example"类缺陷纳入 CI 拦截。

## v0.2.0 (2026-08-07)

> 距 v0.1.0（2026-08-04）以来的累计发布：会话门控重设计、业务逻辑查缺补漏、多项缺陷修复与质量优化、CI 加速。

### 核心亮点
- **会话门控双重校验（破「人工沟通中 bot 仍插话」）**：抽出共用门控裁决 `_reply_gate_reason`，在「进入 LLM 前（前置过滤，省 Token）」与「发送前（并发兜底）」两道关卡接线，任一命中即放弃自动回复；重接 DWS 已读闸门（保守语义，规避历史漏回事故），身份解析失败改为告警暴露而非静默失效。
- **业务逻辑查缺补漏**：修复账号隔离（硬编码绝对路径导致钉钉账号共用命名空间）、会话库级联清理漏图、SQLite 连接泄漏、配置模板幽灵工具等 5 处真实缺陷。
- **缺陷修复轮**：配置回写密钥落盘泄漏、历史清理错库、恢复默认配置误清空、日志明文脱敏、路径可重定位等多个真实缺陷。
- **CI 加速**：依赖安装改用 `uv` 缓存，热路径由 ~1-2min 降至 ~10-30s；新增 `workflow_dispatch` 手动触发，绕过偶发的 push 事件丢失。

### 质量门禁
- 全量回归 **3300+ 通过**；pyright 类型基线维持 **94**（零新增）；gitleaks 每次提交均通过。

### 升级注意
- 新增配置项 `poller.suppress_when_owner_read`（默认 `true`），无需改动既有配置；若所在环境 DWS 未读状态失真导致漏回，可置 `false`。
- 无破坏性变更（breaking change）。

---

## 2026-08-06 — 缺陷修复（配置回写/历史清理/日志脱敏/路径可重定位）

> 聚焦真实缺陷扫描结果的修复轮，按严重度排序：**110 项配置相关测试 + 53 项 management/purge 测试全绿**。

### 配置安全（HIGH）
- **fix(config)**: `update_config` / `update_system_prompt` 写回磁盘前，先把「`.env` 注入的明文密钥」与「`****` 掩码格式字符串」还原为磁盘原始值（新增 `_collect_env_secret_values` / `_MASKED_RE` / `_load_disk_config_raw` / `_revert_env_masked_secrets_to_disk`），杜绝 `load_config` 经 `_apply_env_overrides` 注入的明文密钥被原样落盘到 `config.yaml`。用户**显式新设**的真实密钥仍保留（回归测试 `test_revert_*` 三例守护）。

### 历史清理（MEDIUM）
- **fix(purge)**: `scripts/purge_polluted_history.py` 原硬编码主库 `data/linkora.db`，但真实消息存于按账号隔离的会话库 `data/conversations/{platform}__{hash}.db`，导致主库 0 命中、脏数据永不清理。改为遍历主库 + 全部会话库，删消息时同步维护 `conversations.message_count`、清空 `conversation_summaries`，并调用 `src.memory.image_cleanup.purge_orphan_images` 清理孤儿图片；缺 `messages` 表的库安全跳过；默认 dry-run，需 `--apply` 才真正执行。

### 恢复默认配置（MEDIUM）
- **fix(config)**: `restore_default_config` 原用空 `AppConfig()` 整体覆盖，会清空全部用户设置。改为「出厂骨架 `_deep_merge` 当前配置」，仅补全缺失结构、保留全部现有设置，并兜底原配置。

### 日志脱敏（LOW）
- **fix(poller_strategy)**: `_mask_oid` 统一把 `openDingTalkId` 脱敏为「首尾各 2 位 + `***`」（与 `primary._oid_display` 同格式），修复两处 debug 日志明文打印对方 `openDingTalkId` 的隐私泄漏。

### 路径可重定位（LOW）
- **fix(management)**: `src/tools/management.py` 三处硬编码 `"config.yaml"`（view/update 回退读取、`update` 落盘）统一改为 `paths.get_config_path()`，尊重打包态/数据目录重定位，不再依赖 CWD 恰好是项目根。
- **fix(management)**: `get_config_path()` 返回 `Path`，对 `load_config(path: str)` 两处调用显式包 `str(...)`，消除 pyright `reportArgumentType`（类型检查门禁 CI 初跑曾因 +1 报红，已修）。
- **chore(types)**: `scripts/type_baseline.py::TYPE_ERROR_BASELINE` 由 95 下调至 94 固化本轮收敛（management 两处 Path→str 修复后实测 94）；同步修正 `ci.yml` 注释中过时的「1057 条」。
- **ci(ci)**: `.github/workflows/ci.yml` 增加 `workflow_dispatch` 手动触发事件，应对 push 事件偶发被 GitHub Actions 基础设施瞬时故障（`Service Unavailable`）吞掉、导致 CI run 未创建的情况；此后可手动 `gh workflow run ci.yml -r main` 重跑验证（本轮正是借此绕过两次 push 未触发的问题，使类型检查门禁得以重新校验）。

---

## 2026-08-06 — 代码质量深度优化（T1-T8）

> 本轮针对复杂度、导入顺序、类型安全进行系统性重构，**217 测试通过**，ruff/pyright 门禁全绿。

### 复杂度治理
- **refactor(config)**: `web/routers/config.py::update_config`（C901=145）拆分为 ~27 个 domain helper（`_apply_dws`/`_apply_feishu_platform`/`_apply_wecom_platform`/`_apply_poller_base`/`_apply_llm_base` 等），主函数复杂度降至 <5。
- **refactor(poller_strategy)**: `src/poller_strategy.py::poll_once`（complexity~93，816 LOC）拆分为 14 个 focused helpers（`_fetch_unread_conversations`/`_handle_list_all_fetch`/`_gather_conversations`/`_process_conv_messages` 等），poll_once 变为薄编排层。
- **chore(pyproject)**: ruff C901 阈值从 60 下调至 50，移除 `web/routers/config.py` 的已过期豁免。

### 导入顺序修复
- **fix(poller_core)**: 修复 6 个 `src/poller_core_*.py` 模块的 E402 问题（`logger = ...` 后出现 `from src.*` 导入）——将 mixin 导入移至 logger 定义之前。
- **fix(poller_strategy)**: 清理 `src/poller_strategy.py` 的重复/错位导入，移除多余空行。
- **fix(poller_core_discovery)**: 合并重复的 `from src.poller_mixins_base import PollerMixinBase`，统一导入顺序。
- **verify(web)**: 验证 `web/api.py` 与 `web/routers/kb.py` E402 已通过验证，无需修改（惰性 `_LazyApiModule` 代理模式为设计意图）。

### 类型安全改进
- **fix(config)**: `src/config.py::_build_dingtalk_platform` 中 `adapter=` 参数由 dict 字面量改为显式 `AdapterOverrideConfig(...)`，消除 pyright reportArgumentType。
- **fix(config)**: `web/routers/config.py::_apply_wecom_platform` 添加 `-> None` 返回注解 + `noqa: ARG001`，消除未使用参数告警。
- **fix(poller_strategy)**: 在 `self.dws.sync_external_contacts()` 调用处添加 `# type: ignore[attr-defined]`，抑制已知 mixin 属性动态绑定的 pyright 误报。

### 配置与文档
- **chore(ruff)**: `target-version` 从 py312 升级为 py314，匹配项目实际运行时版本。
- **docs(readme)**: Python badge 从 `≥3.9` 更新为 `≥3.14`。

---

## 2026-08-06 — 配置安全治理 / 备份策略 / CI 回归修复

> 全套测试 **3324 通过**（2 skipped / 2 xfailed），pyright 类型错误维持基线 95，CI 三盏灯全绿。

### 配置与备份安全
- **feat(backup)**: 配置每日滚动备份改为「启动触发 + 仅变更才备份」——bot/web 启动时 `maybe_backup()` 检查「今天是否已备份」与「内容相较最近备份是否有变化」两个门禁，命中其一即跳过；原子写入并滚动保留最近 16 份（已移除原来的固定时间 launchd 定时任务）。
- **fix(config)**: `import_config` 改为合并语义（`_deep_merge`），导入文件只覆盖出现的 key，其余段/参数全部保留，彻底杜绝「导入不完整配置 → 静默丢其余所有段参数」的高危缺陷（回归测试 `test_import_config_preserves_unmentioned_sections` 守护）。
- **fix(backup)**: 隔离测试配置备份（`tests/conftest.py` 将备份根重定向到临时目录），并清理 `data/config-backups/` 中 29 个测试产生的碎片备份（106~403B 假配置），仅保留完整配置。

### CI 回归修复与文档
- **fix(ci)**: 修复 ruff 全量清零（768→0）时误删重导出，导致 `test` 与 `type-check` 回归的问题，恢复 `web/api.py`、`src/platform/runtime.py` 等的 re-export。
- **docs**: README 添加灵桥宣传配图（GitHub Pages 相对引用 `docs/banner.png`）。

### 前端可访问性与性能打磨
- **a11y(web)**: 新增「跳到主内容」跳过链接（锚点 `#main-content`），键盘 Tab 首个可聚焦元素即可绕过侧栏导航，满足 WCAG 2.4.1 绕过区块。
- **a11y(web)**: 全局 toast 提示增加 `role="status"` + `aria-live="polite"` + `aria-atomic="true"`，状态消息可被屏幕阅读器播报（WCAG 4.1.3 状态消息）。
- **a11y(web)**: 侧栏导航当前项改用 `aria-current="page"` 标记（切换页与初始态均覆盖），替代仅视觉 `.active`（WCAG 4.1.2 名称/角色/值）。
- **perf(web)**: 消息/对话图片统一加 `decoding="async"`，与既有 `loading="lazy"` 配合降低主线程解码阻塞。

### 前端缺陷修复（深度审计）
- **security(web)**: 消息渲染的 markdown 链接增加协议白名单校验，仅允许 `http/https/mailto`，阻断 `javascript:`/`data:` 等存储型 XSS 注入（外部会话可诱导管理员点击窃取 `web_auth` 凭据）。
- **fix(web)**: `ApiClient` 构造与 `setAuth`/`clearAuth` 的 `localStorage` 访问全部包 `try/catch`，Safari 无痕/禁用存储抛 `SecurityError` 时降级为未登录而非整站白屏；引导标记 `localStorage` 写入同步兜底。
- **fix(web)**: 批量批准/拒绝草稿、批量删除会话消息、安装技能市场等写操作，由「api 永不 reject 导致失败仍报成功」改为显式检查 `res.error` 再提示成功，杜绝不可恢复操作的误报成功。
- **fix(web)**: `switchPage` 离开 cost-quality 页时显式 `stopCostQualityPolling()`，修复离开后 `setInterval` 永久运行持续请求并回写已隐藏 DOM 的轮询泄漏。
- **fix(web)**: 关键词列表加载失败时（返回 `{error}` 真值）不再误渲染「暂无规则」空态，正确提示加载失败。
- **fix(web)**: `doLogin`、模拟发送、关键词保存增加「进行中」标志防连按/快捷键重复提交（避免重复昂贵 LLM 请求与重复规则）。
- **fix(web)**: 登录成功路径补 `startDecisionPolling()`，与 `switchPage` 路径一致，避免登录后仪表盘决策流不刷新。
- **perf(web)**: 消息页 `.chat-sidebar` 增加 `max-width:768px` 媒体查询，窄屏改为顶部可滚动区域，正文区不再被挤压至不可操作。
- **a11y(web)**: 消息会话项、技能平台下拉项、仪表盘钉钉文档卡、导入上传区等可点击元素补 `role`/`tabindex`/键盘 `Enter`·`Space` 激活；意图页 tab 加 `role="tab"` 且 `switchIntentTab` 同步 `aria-selected`。
- **fix(web)**: 钉钉文档导入项 `onclick` 由内联 JS 字符串字面量改为 `data-doc-id` 属性 + 事件读取，杜绝 `doc_id` 含单引号越出属性的注入；日志级别 `title` 属性增加 `escapeHtml` 防御。

### 前端工程化与可访问性深化（工具层 / 焦点陷阱 / 全局错误 / CI 门禁）
- **refactor(web)**: 抽公共工具层 `web/static/js/core/util.js`（`escapeHtml`/`setText` 单一来源），在模板中于 `store.js` 之后、`app.js` 之前加载；删除 `app.js` 与 `routetrace.js` 的重复定义（原靠加载顺序覆盖，routetrace 版本为死代码），消除脆弱性；`api.py` 模板上下文补 `core_util_js_v`。
- **a11y(web)**: 模态框增加焦点陷阱（Tab/Shift+Tab 在 `role="dialog"` 内循环），补齐 WCAG 2.4.3 焦点顺序；错误类 toast 动态切 `role="alert"`，与既有 `role="status"` 区分紧急度。
- **fix(web)**: `ApiClient._fetchWithRetry` 增加全局错误反馈层（`_notifyGlobalError`），网络错误/超时/5xx 重试耗尽时统一 `showToast(..., 'error')`，替代原先仅 `console.error` 静默。
- **ci(web)**: `ci.yml` 新增 `frontend` job（Node 22 + `npm ci` + `npm run test:frontend`），与 Python lint/test/type-check 并列，防前端回归（不引入 eslint/axe 以免新依赖与存量告警阻塞 CI）。
- **perf(web)**: 评估脚本 `defer` 化——底部脚本已居 `</body>` 末，`defer` 收益可忽略；真正瓶颈为 40+ CSS/30+ JS 请求数，需构建链路合并，留作后续独立提案（动部署链路，审慎推进）。
- **a11y(web)**: 移动端响应式审计——dashboard 已有 760px、messages 已有 768px 断点；skills/cost-quality 无专属 CSS，依赖 Bootstrap 栅格与通用 `dataTable` 组件（已自带响应式），无需硬加断点。

### 前端构建链路（esbuild 合并，首屏请求 ~70 → 2）
- **feat(web)**: 新增 `scripts/build_frontend.mjs`（esbuild 合并）——将 40+ CSS / 30+ 经典 `<script>` 按模板加载顺序合并为单 `bundle.<hash>.css` / `bundle.<hash>.js`，内容哈希命名（长效缓存），写入 `web/static/dist/`；`drafts.js`（`type=module`）不参与合并仍单独加载。
- **perf(web)**: 合并在语义上等价于现有多 `<script>` 共享全局作用域（已审计确认无顶层同名 `const/let/class` 冲突，`DOMAIN` 等均在 IIFE 内）；逐文件剥离顶层 `'use strict'` 统一 sloppy，避免严格模式污染；esbuild 仅压缩不重命名顶层函数/var，故 `window.switchPage` 桥接与内联 `onclick` 处理器不受影响。
- **refactor(web)**: `api.py` 新增 `_read_bundle_manifest()` 读取 `dist/manifest.json` 注入 `bundle_css_v`/`bundle_js_v`；模板 `index.html` 加 `{% raw %}{% if bundle_*_v %}{% endraw %}` 分支——有 manifest 走单 bundle，缺失则自动回退逐文件加载（兼容未构建的开发态），`drafts.js` module 始终保留。
- **test(web)**: 新增 `scripts/smoke_bundle.mjs`（jsdom 求值打包产物，断言 `escapeHtml`/`api`/`store`/`switchPage` 等关键全局符号已挂载、无重复声明错误）；`ci.yml` 的 `frontend` job 增加 `build:frontend` + `smoke_bundle` 步骤，将构建链路纳入门禁。
    - **chore**: `.gitignore` 放行 `web/static/dist/`（根 `dist/` 仍忽略）；`package.json` 加 `esbuild` devDep 与 `build:frontend` 脚本。

### 前端性能实测（Lighthouse）与 defer 落地
- **perf(web)**: 给合并后的单 JS bundle 补 `defer`——脚本在 `DOMContentLoaded` 前按文档顺序执行，bootstrap(3425 立即) 先于它、drafts.js(module 默认 defer, 3472) 后于它，全局桥接（`window.api`/`window.switchPage`）不受影响，且不阻塞 HTML 解析。此前评估「收益可忽略」已被本轮实测佐证（本地 Lighthouse 性能已满分），但补齐规范、对未来脚本前置更 robust，且零功能风险。
- **perf(web)**: 用 Playwright + Lighthouse 对构建后首页做真实性能审计（本地 localhost，未做网络限速）：**性能 100 / 可访问性 91 / 最佳实践 96**；FCP 0.1s、LCP 0.2s、TBT 0ms、CLS 0、SI 0.5s；首屏总请求 **14**（JS 4 / CSS 3，含 bootstrap vendor 与字体）。较合并前审计基线「首屏 ~70 请求（40+ CSS + 30+ JS 逐文件）」大幅下降，证明 esbuild 合并已彻底消除请求数瓶颈（详见 `docs/frontend-perf-audit.md`）。

### CI 报错清理（lint F401 / Node20 弃用告警）
- **fix(web)**: 删除 `web/api.py::_read_bundle_manifest()` 内冗余的局部 `from pathlib import Path`（模块顶层已导入且 `Path(CONFIG_PATH)` 在用），消除 ruff `F401` 触发 `lint` job 失败（该导入在本函数内未被引用，非重导出陷阱，安全移除）。
- **ci(frontend)**: `ci.yml` 的 `actions/setup-node` 由 `@v4` 升 `@v7`，消除「Node.js 20 runtime 弃用、被强制跑在 Node 24」的 workflow 告警（与既有 `checkout`/`upload-artifact@v7` 一致）；`node-version: "22"` 与 `cache: npm` 保持不变。

### Pages 构建失败修复（冲突 workflow + 误改静态方向 + 首页被删 + 配置漂移 + 部署锁积压）
- **fix(pages)**: 根因一「双部署路径打架」——GitHub Pages 源已是 `branch: main /docs`（push 自动 Jekyll 构建 `docs/`），仓库却多了一个自定义 `.github/workflows/pages.yml`（`actions/deploy-pages@v4` 从 `docs/` 打 artifact）。它与官方自动 `pages-build-deployment`（`@v5`）抢同一个 `github-pages` 环境，且都 `concurrency: group: pages` 互相 `cancel-in-progress`，部署卡 `deployment_queued` 直至超时失败。已删除该冗余 workflow。
- **fix(pages)**: 根因二「误把站点改成纯静态、却删了首页」——前轮 `chore(pages): 移除大型静态文件` 把落地页 `docs/index.html`（Apple 极简风、39KB、零内嵌资源）删掉、只留 160B 占位 `index.md`，又加 `.nojekyll` 想走静态。但 `docs/` 里是 `.md` 文档、首页链接 `CHANGELOG.html` 等同名 `.html`，这套结构本为 **Jekyll 模式**设计（`.md`→`.html`、链接才通）。静态化不渲染 `.md` 又无 `index.html` → 根路径 404。已从 `08947d7~1` 恢复 `index.html`、删除 `docs/.nojekyll` 与占位 `index.md`，让 Jekyll 正常运行。
- **fix(pages)**: 根因三（决定性）「`_config.yml` 配置漂移」——多次来回改动中，文件丢失了 `skip_config_check: true` 及完整 `exclude`（`*.mermaid`/`audit/**/*`/`.git`/`*.yaml`/`*.yml`）/`destination: _site`。GitHub Pages 对 `_config.yml` 做严格校验，缺 `skip_config_check` 时遇到 `*.yml`/`*.yaml` 与 `audit` 大目录即 `errored`。实证：13:42 的 f4967c3 因带完整配置 `built` 成功；后续 6cab0cc 把配置砍到只剩 `exclude: ["*.mermaid"]` → 再次 `errored`。本次把 `_config.yml` 还原为 f4967c3 的完整配置，提交后 push 到 main 应恢复 `built`。
- **fix(pages)·排除干扰项**：一度在 f0b5ce5 加回 `theme: jekyll-theme-cayman` 并误判「cayman 主题导致失败」，但 6cab0cc 去主题后仍 `errored`——证明主题非元凶，真正差异在 `skip_config_check` 与 exclude 列表。故最终配置不加 `theme`，`.md` 以默认样式渲染（cayman 主题需单独排查，留作后续）。
- **fix(pages)·部署超时（第二类独立故障）**：还原配置后 Jekyll 构建已成功（`Build with Jekyll` 步骤 `success`，14:24:49Z），但 `Deploy to GitHub Pages` 步骤卡在 `deployment_in_progress` 直至 **10 分钟超时取消**（`##[error]Timeout reached, aborting!`）。根因是 13:33→14:24 在半小时内连推 6 次，`github-pages` 环境部署锁/队列积压，新部署一直排不到。排查确认环境无残留 `in_progress`/`queued` 部署（`wait_timer=None`、无 reviewers、旧部署均已是 terminal 状态）后，**重跑** `pages-build-deployment` 工作流，部署在环境空闲时顺利完成——站点恢复可访问（`https://0x0021.github.io/Linkora/` 与各 `.html` 文档页均 HTTP 200，`latest deployment state=success`）。后续密集迭代时避免短时间连推，以防再次触发部署锁积压。

### 首页文案润色（中文表达提质）
- **docs(web)**: 重写 `docs/index.html` 落地页文案，纠正数处「机翻感」直译与生硬口语，改为更自然、有中文质感的表达（保持 Apple 极简克制语气）：标题与首屏主句「已经上班了」→「已经就位」；首屏 lede「它待在你每天用的群里…」→「它就在你每日所用的群里待命…也懂得何时该请真人接手」；平台段「谁也看不见谁」→「彼此互不可见」、飞书「知识库自动跟着更新」→「知识库随之自动更新」；能力段「该它上的时候上，该让人来的时候退」→「该出手时出手，该让位时让位」、知识库「心里没底就不硬答」→「没有十足把握时，绝不出言妄断」、「知道什么时候闭嘴」→「懂得分寸」；数字段「都是实测的」→「皆有实测为证」；上手段「不用买服务，不用交数据」→「无需购买服务，也无需交出数据」、三步叙述规范化；文档段「都在这儿」→「尽在于此」；结尾「开源的 / 可以拿去用，可以改」→「开源，自由可塑 / 你可自由使用、修改，也欢迎一同将它打磨得更好」；页脚版权「基于 GPL-3.0 开源发布」→「基于 GPL-3.0 协议开源发布」。同步更新 `<title>`/`<meta name="description">`/`<meta property="og:description">` 等 SEO 文案。

---

## 2026-08-05 — 安全清零 / 类型收敛(F9) / UI 重做 / CI 版本统一

> 约 60+ 提交集中收敛质量与体验；全程测试持续通过。

### 安全与合规（CodeQL 清零）
- **fix(security)**: 修复 CodeQL 高危告警（SSRF / 敏感信息 / 路径穿越 / 异常泄露），路由响应统一走 `web/errors.py` 安全详情（真实错误只进服务端日志）。
- **fix(security)**: 第二轮清零——logging 脱敏补全、路径净化下沉、CI 权限收紧。
- **fix(security)**: 路径净化改用 `abspath`（CodeQL 认可的 sanitizer），清零 10 个误报告警。
- **fix(security)**: 全局 5xx 不泄露内部异常 + KB 问答响应脱敏；同步 5xx 测试断言到全局脱敏处理器。
- **fix(runtime)**: 回复锁令牌化 + 风格画像刷新 + 路径覆盖进程共享。

### 类型收敛（F9）
- **build(ci)**: 新增 pyright 非回归门禁（`type_baseline.py` 比对锁定基线，error 数只减不增）；修复门禁不被 pyright 非零退出码提前中止（`|| true`）。
- **refactor(types)**: 三小家族建共享基类（类型错误 334→205→96）；poller / platform 建共享基类消动态 MRO 类型错误；顺带修复若干被掩盖的真实缺陷。

### UI / 仪表盘重做
- **feat(ui)**: 系统概览卡片 Premium v2 / v3 整体重设计（Hero + 自适应次级卡 + 状态胶囊流），合并「工具」与「配置自检」去重，每个 chip 加 hover 解释。
- **refactor(metrics)**: 消除指标监控与成本/质量两页 KPI 卡重复；补齐 routetrace KPI 卡片 icon/sub。
- **fix(frontend)**: 配置保存崩溃修复 + 成本/质量引文卡等高滚动。

### CI / 构建 / 依赖
- **build(ci)**: 统一 Python 版本到 **3.14.6**，移除 3.12/3.13 矩阵。
- **fix(deps)**: 修正 Dependabot 升级导致的依赖锁漂移与 tokenizers 不可解析冲突；回退 `rapidocr-onnxruntime` 至 1.2.3 恢复 CI。
- **ci(github)**: Dependabot 改为 workflow 内审批 + 等待 test 通过再合并；启用 CodeQL / 依赖审计 / Pages 文档站。
- **style(lint)**: 全量清零 CI ruff annotations（768→0），含 `config.py` F401 抑制迁到 `pyproject.toml` per-file-ignores。

### 文档站点
- **feat(site)**: 重做 Pages 落地页为高级玻璃拟态 UI / Apple 极简风 + PPT 整页吸附滚动，启用赞助与自动发布。
- **docs**: 归档历史审计报告到 `docs/audit/`（去噪，避免新人被过时内容带偏）。

---

## 2026-08-04 — 仓库重新发布 / 社区化 / poller 对话体验

> 仓库以「清空历史、仅保留最新状态」方式重新发布为开源项目。

### 仓库发布与治理
- **chore**: 初始化仓库快照（清空历史，仅保留最新状态）；协议改为 **GPL-3.0**；移除源码/测试中的个人身份信息（PII），提交作者改中性署名。
- **docs(repo)**: 补齐 GitHub 社区/安全/治理文件与 LICENSE。
- **ci(github)**: 启用 CodeQL 扫描、依赖审计、Dependabot 自动合并与 Pages 文档站。
- **docs(readme)**: 精简文档、闭合顶部居中 div 避免正文全部居中。

### CI 修复
- **fix(ci)**: 修复 GitHub Actions 全部测试失败。

### poller / 对话体验
- **feat(poller)**: 新增**真人在场冷却**，防止 AI 穿插真人对话。
- **fix(poller)**: 避免业务请求里的「您好」和沟通结束后的表情误触发 keyword 回复；list-all 分页上限提示降为 INFO 并修复冷却失效。
- **test(dws)**: 适配 list-all 封顶提示的日志级别与冷却实现。

---

## 2026-08-03 — 稳定性 / 质量集中加固轮

> 约 25 个提交，全量测试 **3275 通过、0 失败**。

### 启动可靠性与稳定性
- **fix(poller)**: 黑名单对账自愈改用 `chat_conversation_info`，避免启动期对保密群触发 dws `list-all` 6 年跨度分窗全扫挂死（bot 启动卡死根因）。
- **fix(platform)**: 守护线程（备份 / 指标）增加循环级异常兜底，防单轮异常静默杀死整个线程。
- **fix(platform)**: 备份协调器启动首轮 `backup_on_start` 异常兜底，仅记日志并继续周期备份。
- **refactor(dws)**: `chat_message_list_all` 内部按 7 天窗口切片翻页并去重。
- **fix(sync)**: `sync_history` range 模式分窗，窗长 30→7 天，消除 list-all 分页触顶截断。
- **fix(dws)**: `chat_message_list` 透传 `timeout` 给 fallback 扫描。
- **fix(web,platform)**: status 链式 `get` 加固 + 自我检测 list-all 窗口收敛。

### 数据完整性与资源清理
- **fix(memory)**: 删消息 / 批量删会话 / 定期清理时连带删除 `data/tmp_images` 孤儿图片（新增 `src/memory/image_cleanup.py`，含 `../` 越界护栏）——修复长期磁盘泄漏。
- **feat(store)**: `init_db` 首跑自动清理无主库的孤儿 WAL/SHM 文件。
- **fix(poller)**: OA 审批卡片显式 `null` 字段导致解析崩溃 → 降级为原始正文。
- **fix(tools)**: `web_search` / `weather` 显式 `null` 字段兜底为默认容器。
- **fix(llm,platform)**: 工具失败 `result=None` 时的 `.get` 链式调用崩溃被静默捕获。

### 多平台上下文隔离
- **fix(platform)**: 回复锁重试 Timer / 异步记忆提取线程池 / 防抖 Timer 在新线程还原平台 ContextVar（或 `contextvars.copy_context().run`），避免飞书 / 企微记忆静默写入钉钉库、回复发错平台。
- **fix(llm)**: 修复具名主人数字分身身份泄漏（G2）。

### 架构解耦与可维护性
- **refactor(dws)**: `dws_adapter.py`（1281 行）拆为包（8 个 mixin + core + 组合根）。
- **refactor(config)**: `config.py`（1126 行）拆为 `config_models.py`（模型）+ 薄加载入口。
- **refactor(memory)**: `sqlite_store.py`（1145 行）拆出连接管理 + 向量索引两个 mixin。
- **refactor(llm)**: `agent.py` 拆出 `agent_steps` 子模块，`AgentReply` 独立为 `agent_reply.py`。
- **test(...)**: 对齐真实契约，修掉最后 5 个预存在失败，全量 3275 测试全绿。
- 新增回归测试拦截「`tools.available` 未全部出现在 `TOOL_ACTION_MAP`」类启动级漂移。

### 功能补齐
- **fix(tools)**: 补齐审批 10 个工具的五处接线（P0 防漂移），含「我执行的」审批。
- **fix(web)**: 补齐 `/api/messages/batch-delete` 端点，修复前端批量删除消息死链。
- **fix(llm)**: 空 RAG 激进清洗不误伤天气百分比，并重置跨请求 RAG 状态。
- **chore(config)**: 放宽低置信草稿审阅阈值 0.5→0.35，减少审签打扰。

---

## 更早的重要里程碑（摘要）

- **多平台物理隔离架构**：钉钉 / 飞书 / 企业微信各自独立适配器、独立 SQLite 库、独立轮询器，数据互不可见。
- **A1 双进程分离**：Web 与后台轮询器（worker）进程分离，由 `scripts/run_linkora.py` 拉起，改 Web 代码只重启 web 进程不打断 ingestion。
- **RAG 混合检索**：BGE 本地离线向量 + BM25 重排序（0.6 + 0.4），置信度门控。
- **审批转交（钉钉）**：10 个审批工具 + 通用审批子系统。
- **长期记忆 / 风格人格 / 图片 OCR / 异步摘要压缩** 等智能增强能力就绪。

> 详细设计动因与方案见 `docs/architecture.md`、`docs/design.md`、`docs/phase0_hardening_design.md`、`docs/phase2_citation_confidence_design.md`、`docs/audit/multi-platform-audit-report.md`。
