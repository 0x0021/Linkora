# 更新日志 · Changelog

本项目以内部协作方式演进，提交采用中文 `type(scope)` 规范（见 README「贡献指南」）。
本文件记录近期主要变更；更早的架构演进与设计文档见 `docs/` 下各 `*-design.md` / `*-audit*.md`。

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
