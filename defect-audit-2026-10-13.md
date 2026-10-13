# Linkora 缺陷系统梳理（续）· 2026-10-13 轮

> 范围：基于 8-9 月 `defect-audit.md`（F1-F13 / D1-D10）与用量台账之后，对 10 月最新代码做第二轮查漏补缺。
> 方法：三个只读扫描 agent（安全 / 并发与数据完整性 / 死代码与配置漂移）全仓扫描 + 主代理逐项交叉验证（读源码确认）。
> 门禁（本次改动后）：ruff 全路径 pass / pyright 0 error（18 历史 warning 与本次改动无关）/ check_no_bare_except 638 基线未新增 / check_deps --strict 通过 / 针对性 pytest 20 passed。

---

## 一、本次已落地修复（8 处，已 commit）

### R1 · [P2] 技能图标下载 SSRF（`web/routers/image.py::_download_skill_icon`）
- **原**：`is_ssrf_safe` 校验后，用 `httpx.AsyncClient(follow_redirects=True)` 直接抓取，**未钉 IP**。存在 DNS 重绑定 TOCTOU（校验时公网、建连时内网）+ 重定向 SSRF（可指向 `169.254.169.254` 云元数据）。同文件 `/api/proxy/image` 是安全的（`follow_redirects=False` + 钉死 IP），此处防护不一致。
- **修**：改用 `src.utils.net.ssrf_safe_get`（解析一次即钉死公网 IP、默认不跟随重定向），经 `run_in_threadpool` 调用，与 `/api/proxy/image` 同源防护。新增 `ssrf_safe_get` import。

### R2 · [P2] 配置漂移对齐（`config.yaml.example` vs `src/config_models.py` 默认值）
下方问题会导致「复制模板」与「省略配置走默认」行为矛盾，甚至运行时报错：
- `embedding.model`：example `bge-base-zh-v1.5`(768维) → 对齐代码默认 `bge-small-zh-v1.5`(512维)，避免预建 FAISS 索引维度不匹配。
- `rag_intent_only`：example `false`(总是注入) → `true`(仅意图注入)，消除**语义反向**。
- `rag_min_similarity` 0.30→0.6、`rag_max_results` 4→1、`max_chars_daily_chat` 512→50、`max_chars_tech_issue` 1024→100、`hard_truncation_chars` 800→300，全部对齐代码默认；注释同步自洽。

### R3 · [P2] 主库 `linkora.db` 漏做 WAL checkpoint（`src/platform/memory.py`）
- **原**：WAL checkpoint 调度器只遍历会话分库，主库（memories / decisions / tool_execution_logs / drafts 等高频写表）WAL 长期累积，与当初修复分库 WAL 的初衷相反。
- **修**：`checkpoint_loop` 每轮补 `self.store.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")`，带 `sqlite3.Error` / `RuntimeError` 优雅退出。

### R4 · [P3] `run()` 缺重复启动防护（`src/platform/lifecycle.py`）
- **原**：`run()` 被误二次调用会再次 start 各调度并 append 线程 → 后台线程翻倍。
- **修**：入口 `if self._running: return`（与子调度器守卫对齐；`stop()` 置 False 后下次可正常再启动）。

### R5 · [P3] `upload_image` 漏出幂等护栏（`src/tools/idempotency.py`）
- **原**：`upload_image` 是对外副作用工具但不在 `SIDE_EFFECT_TOOLS`，at-least-once 重放可能重复上传。
- **修**：加入 `SIDE_EFFECT_TOOLS`（dws 当前已下线该能力，属前瞻加固）。

### R6 · [P3] 开发态 models 页缺 CSS 链接（`web/templates/index.html`）
- **原**：生产 bundle 含 `models.css`，但开发态 CSS link 段遗漏 → `python main.py` 直出时 models 页缺样式（非白屏）。
- **修**：补 `<link ... models.css?v={{ models_css_v }}>`（`models_css_v` 由 `_auto_page_versions` 自动注入）。不影响生产 bundle。

（R1-R6 共改动 6 个文件：`web/routers/image.py`、`src/platform/memory.py`、`src/platform/lifecycle.py`、`src/tools/idempotency.py`、`config.yaml.example`、`web/templates/index.html`）

---

## 二、扫描发现但未擅自改动的项（建议，按优先级）

### 待专项处理（设计决策 / 大面改动）
- **[P1] 主库写路径系统绕过写入闸门**（并发 agent 核心发现）：`_write_gate` / `write_with_retry` / `with self.store._lock` 当前只施加到 4 个会话分库 repo（message / conversation / blacklist / external_friend）。主库全部写 repo（`kb` / `decisions` / `draft` / `docs` / `routing_quality` / `keyword_rule` / `gate_rule` / `baseline` / `few_shot` / `memory` / `feedback` / `tool_execution`）均为 `conn.cursor() + conn.commit()` 直写，与 10-05 会话分库事故的「写侧未收口」**同源缺陷在主库复现**；且 `test_write_gate_coverage.py` 的 `REPO_FILES` 对主库 repo 零断言。建议：将主库高频写 repo 收口到统一 `_write` / `write_with_retry` 封装，并把主库 repo 纳入 AST 断言。涉及面大、需压测验证，**未擅自改**。
- **[P2] 读/写游标未关闭在热路径复发**：`message_repo.py:166/187/347/359/560`、`sqlite_store.py:555/568`、`tool_execution_repo.py:41/89/109` 等多处游标未 `finally: cur.close()`。CPython 引用计数下实际风险低，但破坏事故后不变量，建议统一补 `finally` 并新增「写/读方法创建 cursor 必须 close」的 AST 门禁。
- **[P3] `display_summary_scheduler` 写回绕过进程内闸门**：`_write_with_retry` 直调 `upsert_display_summary`，未走 `store._write_gate` 串行化，与其它会话写路径不共享同一把锁。建议改为经 `conversation_repo._write` 走闸门。

### 文档 / 配置一致性（低风险，建议）
- **[P2] `docs/configuration.md:109`** 仍写 `embedding_model: bge-base-zh-v1.5`，与代码默认、已修的 example 三方不一致，需同步为 bge-small。
- **[P3] `config.yaml.example` 总览** 称有顶层 `poller(legacy)` 段，正文仅 dws/storage legacy，建议补段或修正注释。
- **[P3] 模型有而 example 缺失字段**：`poll_concurrency` / `min_conversation_poll_rounds` / `chat_rate_limit_cooldown_seconds` / `memory.summary_merge`。
- **[P3] `package.json` 缺 `version` 字段**（与 pyproject/CHANGELOG 无法对齐）。
- **[P3] `docs/TEST_COVERAGE.md` 严重过时**（称 267 文件/3952 用例 vs pyproject 基线 2173 passed）。
- **[P3] Web 子路由测试覆盖不均**：多数 `web/routers` 子路由无独立 endpoint 测试，仅靠 `test_web_api_endpoints.py` 间接覆盖。
- **[P3] 2 个长期 xfail**（弱模型回归 G1/G4）挂起，建议设定期复核。

### 已确认干净（无需改动）
SQL 注入（参数化 + `allowed_fields` 白名单）、命令注入（无 `shell=True` / 无 `eval` / `exec`）、硬编码密钥（无明文 secret）、认证/RBAC（JWT+PBKDF2、logout 黑名单进程内集合为已知限制）、XSS 转义、CORS、GET /api/config 脱敏、路径净化、宽异常静默吞噬（129 处候选均为有意 fail-soft）、写入闸门**会话分库侧**到位、索引自愈到位、WAL **分库** checkpoint 到位、备份正确性、at-least-once 语义、subprocess 均带 timeout。

---

## 三、门禁验证
- ruff：All checks passed!
- pyright：0 errors / 18 warnings（均为第三方包 `reportMissingModuleSource`，与本次改动无关）
- check_no_bare_except：638 基线未新增
- check_deps --strict：依赖一致性通过
- 针对性 pytest（idempotency / image_cookie / orphan_cleanup）：20 passed

---

## 四、对比前一轮
8-9 月缺陷审计（F1-F13 / D1-D10）聚焦配置读写 / Web 安全 / 轮询适配器 / 资源清理；本轮在已加固基础上发现「**修复被局限在会话分库、主库整面写路径复现同源缺陷**」这一实质风险（P1），并补齐主库 WAL、SSRF 图标下载、配置漂移、重复启动防护、幂等漏网等边缘项。
