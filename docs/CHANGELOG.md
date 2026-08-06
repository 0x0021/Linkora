# 更新日志 · Changelog

本项目以内部协作方式演进，提交采用中文 `type(scope)` 规范（见 README「贡献指南」）。
本文件记录近期主要变更；更早的架构演进与设计文档见 `docs/` 下各 `*-design.md` / `*-audit*.md`。

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
