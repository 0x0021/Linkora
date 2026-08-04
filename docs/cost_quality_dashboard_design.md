# 成本 / 质量看板设计（Roadmap ③，可并入 P2-7 Web 引文面板）

> 设计者：主理人齐活林（架构师岗位因 Agent 运行时不可用，由主理人亲自调研并产出）
> 调研方式：仅本地读取源码 + 查询 `data/linkora.db` 实际数据，未做任何修改
> 目标：集中展示 LLM 调用成本与回复质量，用于观测灰度中的「引文页脚」(`citation_enabled=true`) 与「低置信转人工」策略效果

---

## 0. 实测数据画像（基于 data/linkora.db）

| 表 | 行数 | 关键列填充情况 |
|----|------|----------------|
| `routing_quality` | 457 | `primary_score` 非空 237；`total_tokens`>0 仅 101；`cost_usd` 均值 **0.0**（空壳） |
| `decisions` | 484 | `action`∈{skip,reply-rule,llm}；**无 handoff / cited / rag_grounded 标记列** |
| `message_drafts` | 0 | 结构含 `rag_confidence`/`rag_threshold`/`rag_best_chunk`，但**当前 0 行**（低置信转人工未落库或尚未触发） |
| `feedback` | 12 | `rating`∈{1,-1}；有用 11 / 无用 1 |

**结论**：置信度分布、工具/路由指标已可直取；**成本与质量缺口集中在三处**：
1. `cost_usd` 从未计算（列存在但恒 0）→ 成本看板为空。
2. 「低置信转人工率」「RAG 命中率」「引文页脚命中率」**无结构化标记**，无法聚合。
3. `message_drafts` 当前空表，转人工链路可能未落库（需确认 + 补计数器）。

---

## 1. 看板指标清单与数据来源映射

### A. 成本类（复用 `MetricsCollector.token_stats` + `routing_quality` 既有列）
| 指标 | 来源 | 现状 |
|------|------|------|
| LLM Token 总消耗（in/out/total） | `routing_quality.total_tokens/input_tokens/output_tokens` | 101 行有值，可直接聚合 |
| 折算成本（¥） | `routing_quality.cost_usd` | **链路已通**：`agent._mk_reply` 经 `update_routing_quality_trace` 写入 `cost_usd`（agent.py:537-550），`_estimate_cost` 按 `_MODEL_PRICING` 估算 |
| 调用次数 | `COUNT(routing_quality)` | 457 行 |
| 按模型分布 | `routing_quality.llm_model` | 列已存在 |
| 按会话 Top20 | `token_stats.by_chat` | 已实现 |

**实测关键修正**：当前 `data/linkora.db` 中 101 条有 token 的行 `cost_usd` 全为 **0.0**，但**这不是代码 bug**——
所有实际调用的模型均为免费模型（`kenari-free` / `deepseek-v4-flash:free` / `mimo-v2-5:free` / `agnes-2.0-flash`），
而 `_MODEL_PRICING` 中这些模型的单价均为 `{"input":0,"output":0}`（见 `src/llm/history.py:30`）。
即**成本追踪链路已端到端可用**，仅因运行模型免费而显示 ¥0。切到付费模型（如 `gpt-4o`）即会自动出数。

**结论**：成本侧**无需补算根因**，只需在展示层把 `cost_usd`（USD）换算为 **¥（CNY）** 以提升可读性（见 2.1）。

### B. 质量类
| 指标 | 来源 | 现状 |
|------|------|------|
| 回复置信度分布 | `routing_quality.primary_score` | 237 行有值，分桶即可 |
| 低置信转人工率 | 需新增标记 `decisions.handoff` 或 `routing_quality.handoff` | **无**，需补采集 |
| RAG 命中率 | 需新增标记 `rag_grounded` | **无**，需补采集 |
| 引文页脚命中率 | 需新增标记 `cited` | **无**，需补采集 |
| 用户反馈有用率 | `feedback.rating` | 已可聚合（11/12 有用） |

---

## 2. 后端方案

### 2.1 成本展示（零 schema 变更，仅 ¥ 换算）
- **`src/metrics/collector.py::token_stats`**：在已有聚合基础上，将 `cost_usd`（USD）按固定汇率换算为 ¥（`cost_cny = cost_usd * USD_CNY_RATE`），并在返回结构新增 `total_cost_cny` / `avg_cost_cny` 字段（USD 字段保留兼容）。汇率常量放 `config.py` 或模块顶部，便于调。
- **无需补 `cost_usd` 写入点**：经实测，`agent._mk_reply`（agent.py:537-550）已通过 `update_routing_quality_trace` 正确写入 `cost_usd`；当前 DB 全为 0 仅因运行模型均为免费模型（`_MODEL_PRICING` 中单价 0）。成本链路已端到端可用，切付费模型即自动出数。

### 2.2 质量标记采集（新增 3 个轻量列）
在 `decisions` 表（或 `routing_quality`）追加：
- `handoff BOOLEAN DEFAULT 0` —— 本条是否触发低置信转人工（main.py `1959 _should_handoff_low_confidence` 为 True 时置 1）
- `rag_grounded BOOLEAN DEFAULT 0` —— RAG 是否命中（reply.confidence is not None 且 ≥ 某阈值，或 best_chunk 非空）
- `cited BOOLEAN DEFAULT 0` —— 是否实际追加了引文页脚（main.py `1964 _append_citation_footer` 返回文本≠原文本时置 1）

> 选 `decisions` 表原因：每条消息一次 record，与「率」的分母（总处理数）天然对齐；且 `decision_tracker.record` 已是统一入口，加 3 个可选字段成本极低。

**改动点（最小、向后兼容）**：
- `src/decision_tracker.py::DecisionRecord` 增 3 字段；`record()` 透传。
- `SQLiteStore._decisions_repo.record_decision` 增 3 列写入（ALTER TABLE ADD COLUMN，旧库兼容）。
- `main.py` 两处置位：`tracker.record(..., handoff=..., rag_grounded=..., cited=...)`。
- 灰度安全：默认不查询、不影响回复；仅看板读取。

### 2.3 新增端点 `web/routers/cost_quality.py`
复用现有 `MetricsCollector` + `run_in_threadpool`（T6 模式），不新造轮子：
- `GET /api/cost-quality/summary?hours=24`
  - 聚合各平台 `token_stats`（含 cost_cny）+ 质量标记（handoff/rag_grounded/cited 计数）+ `feedback` 有用率
  - 返回结构：`{ available, totals:{cost_cny, total_tokens, handoff_rate, rag_hit_rate, citation_rate, feedback_useful_rate}, by_platform:{...}, confidence_hist:[{bucket,count}] }`
- `GET /api/cost-quality/confidence-hist?hours=24` —— 置信度分桶（10 桶，0~1）
- `GET /api/cost-quality/trend?days=7` —— 每日成本(¥) / 转人工率 趋势（供折线图）

注册：`web/api.py` 的 `include_router`（参考 metrics 路由注册），并 `run_in_threadpool` 包裹阻塞查询。

---

## 3. 前端方案

### 3.1 新增页面 `web/static/js/pages/cost_quality.js`
- 严格复用 `metrics.js` 模式：`loadCostQualityPage()` + `startCostQualityPolling()/stopCostQualityPolling()`，调 `/api/cost-quality/summary`，用 `Chart` 渲染：
  - KPI 卡片：总成本(¥)、总 Token、转人工率、RAG 命中率、引文页脚命中率、反馈有用率
  - 成本趋势折线图（`trend` 接口）
  - 置信度分布柱状图（`confidence_hist`）
  - 质量率环形图（转人工/命中/引用/反馈）
- 复用既有 `chartTheme()` / `metricsFmtCost` / `metricsFmtTokens` / `setText` / `showToast`。
- 空状态兜底（与 metrics.js 一致）：接口 `available:false` 或字段为空 → 显示「暂无数据」。

### 3.2 注册（3 处，对齐 metrics 页）
1. `public/index.html`：`.nav-links` 内加 `<a data-page="cost-quality">成本/质量</a>`。
2. `web/static/js/core/app.js`：
   - `titles` map 加 `cost-quality: '成本/质量'`
   - `ALLOWED_PAGES` 数组加 `'cost-quality'`
   - 调度分支加 `if (page === 'cost-quality') { loadCostQualityPage(); startCostQualityPolling(); }` 与对应 stop。
3. `public/index.html` 的 `<script>` 区加 `<script src="/static/js/pages/cost_quality.js"></script>`（紧随 metrics.js）。

### 3.3 P2-7 引文面板并入
引文列表展示（`reply.citations`：source/score/snippet）可作为该页一个子卡片：从 `GET /api/cost-quality/citations?limit=20`（后端从 `decisions` 近 N 条 cited=1 的记录回查，或直接从现有 `routing_quality` 关联）拉取，前端表格列出「标题 / 相关度 / 片段」。

---

## 4. 文件清单（相对路径）

新增：
- `docs/cost_quality_dashboard_design.md`（本文件）
- `web/routers/cost_quality.py`
- `web/static/js/pages/cost_quality.js`

修改：
- `src/metrics/collector.py` —— `token_stats` 增 ¥ 换算字段
- `src/decision_tracker.py` —— `DecisionRecord` + `record()` 增 3 标记字段
- `src/memory/sqlite_store.py`（`_decisions_repo.record_decision`）—— 写入 3 列 + 迁移 ADD COLUMN
- `main.py` —— 两处 `tracker.record(...)` 置位 handoff/rag_grounded/cited
- `src/llm/agent.py` 或 LLM 客户端 —— 补 `cost_usd`（根因成本缺口）
- `web/api.py` —— `include_router(cost_quality.router)`
- `web/static/js/core/app.js` + `public/index.html` —— 注册页面与导航

依赖包：无新增（沿用 Chart.js / fastapi / sqlite3）。

---

## 5. 任务分解（有序、含依赖）

1. **T1 token_stats ¥ 换算**（小）：`collector.token_stats` 加 `cost_cny` 字段 + 汇率常量（成本链路已通，无需补写入）。
2. **T2 质量标记采集**：`decision_tracker` + `sqlite_store` 加 `handoff/rag_grounded/cited` 列与写入；`main.py` 置位（解锁转人工率/RAG命中率/引文命中率）。
3. **T3 后端端点**：`web/routers/cost_quality.py`（summary/hist/trend/citations），`web/api.py` 注册。
4. **T4 前端页面**：`cost_quality.js` + 三处注册。
5. **T5 测试**：`tests/test_cost_quality.py` 覆盖端点契约、¥ 换算、标记聚合、空状态；全量回归保绿。
6. **T6（P2-7）引文面板**：cost_quality 页引文子卡片 + `/api/cost-quality/citations`。

依赖：T1（成本展示）；T2→T3（质量）；T1+T3→T4（前端）；最后 T5 回归。

---

## 6. 硬约束（必须遵守）

- **零行为变更**：新字段默认不查询、不阻断回复；feature off 时完全无操作。`cost_usd` 补算仅在回写 routing_quality 时附加，不影响回复生成。
- **复用 1900+ 测试安全网**：每个改动跑 `KMP_DUPLICATE_LIB_OK=TRUE .venv/bin/python -m pytest`，全绿方可 commit。
- **异步模式**：所有新端点复用 T6 `run_in_threadpool` 包裹阻塞 SQLite 查询。
- **向后兼容迁移**：`decisions` 加列用 `ALTER TABLE ... ADD COLUMN`（SQLite 支持），旧库自动兼容，不重建表。
- **汇率/单价可配置**：先放常量，后续接 `config.yaml`（不在本看板 scope 内强求）。

---

## 7. 待明确事项

1. `cost_usd` 根因写入点具体位置（需 grep `_routing_quality_repo` 的 insert 调用，确认 LLM usage 是否已在别处计算）。
2. `message_drafts` 当前 0 行：是「尚未触发低置信」还是「`_notify_owner_draft` 落库失败」？需核对日志（影响 handoff 率是否改从 `decisions.handoff` 取数）。
3. 汇率/单价是否接 config 还是常量（建议常量先行）。
4. 看板默认时间窗（建议 24h，与 metrics 页一致）。
