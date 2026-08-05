# 更新日志 · Changelog

本项目以内部协作方式演进，提交采用中文 `type(scope)` 规范（见 README「贡献指南」）。
本文件记录近期主要变更；更早的架构演进与设计文档见 `docs/` 下各 `*-design.md` / `*-audit*.md`。

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
