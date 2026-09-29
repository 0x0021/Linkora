# 缺陷梳理：LLM 全口径用量台账（usage_ledger）

梳理对象：2026-09-28/29 落地的「指标监控 / 成本质量 两页 token 全口径统计」功能
（`src/llm/usage_ledger.py` + `src/llm/client.py` + 12 处 `purpose_scope` 调用点 +
`web/routers/cost_quality.py` + `web/routers/metrics.py` + 前端两页）。

结论先行：**功能口径正确**（确实补齐了对话外的摘要/记忆/画像/技能/工具/知识库消耗，流式回复也不再漏计），但发现 **1 个 HIGH 级真实缺陷（已修复）+ 2 个需决策的方案项**。其中一处最初的怀疑（指标页平台 Token 图标签）经核对**不是缺陷**。

---

## ✅ 已确认正确的部分（排除误报）

- **流式回复用途归属正确**：`client.chat()` 返回生成器，但 `record_usage` 在 `_do_chat_stream` / `_do_chat_impl` 内于生成器迭代当帧调用，仍处 `with purpose_scope(...)` 作用域；`chat()` 公开签名未加 `purpose` 参数（守护了所有 fake client 测试桩）。已通过 `9070095` 验证。
- **指标页两图口径无混淆（非缺陷）**：`index.html:2591` 平台 Token 图明确标注「（仅回复链路）」，全口径图另立「LLM 用量分布（按用途 · 全口径）」（`:2600`）。前端 `metrics.js` 中 `renderTokenChart` 读 `routing_quality`（仅回复），`renderUsageDistribution` 读 `ledger`（全口径），二者分离清晰，不存在标签误导。

---

## 🔴 Defect A（HIGH，已修复）：`llm_usage` 表无保留期清理 + 无时间索引

**根因**：全局表清理调度器 `src/platform/memory.py::_start_global_tables_cleanup_scheduler`（D7 阶段）只清理 `tool_execution_logs` / `feedback` / `message_drafts` 三张表，**漏掉了新建的 `llm_usage`**。该表存于主库 `linkora.db`，随每次 LLM 调用（含摘要/记忆/画像等后台调用）持续增长，长期运行会：

1. **主库 `linkora.db` 无限膨胀**——无清理；
2. **时间范围查询全表扫描**——`get_stats` 的 `WHERE ts >= ?` 与 `get_daily_cost_usd` 的 `WHERE ts LIKE ?` 均无 `ts` 索引，表大后变慢。

**修复（已落地，commit 待提交）**：
- `src/llm/usage_ledger.py`：
  - `_ensure_table()` 追加 `CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts)`（对已有库幂等，下次写入即补索引）；
  - 新增常量 `USAGE_RETENTION_DAYS = 365`（成本台账需支撑年度对账，且成本页趋势最大窗口 365 天，故长于消息保留期，并与 `messages_retention_days` 解耦）；
  - 新增 `cleanup_old_usage(retention_days=None)`：短连接 + `busy_timeout` 模型，删除超期记录，失败返回 0 不阻塞。
- `src/platform/memory.py`：清理循环内接入 `cleanup_old_usage()`，日志补 `llm_usage=%d`。
- 测试：`tests/test_usage_ledger.py` 新增 `test_ts_index_created` / `test_cleanup_old_usage`（含超期删除 + 近期保留）。**16 项全过，ruff 通过。**

---

## 🟡 Defect B（LOW，仅多平台部署）：平台归属经 dingtalk 默认值兜底

**现象**：`usage_ledger._current_platform()` 调用 `get_current_platform()`，其兜底读到 `base.py:67` 的 `_active_platform_ctx` 默认值 `"dingtalk"`。因此 **web 后台触发的 LLM 调用**（主人画像 `persona.py:217`、知识库 `kb.py:588` 仅包了 `purpose_scope`，未设平台作用域）会被记成 `platform="dingtalk"`。

**为何暂不修**：
- **单平台（钉钉）部署下这是正确行为**——所有调用（含后台操作）本就属钉钉。`tests/test_usage_ledger.py::test_by_platform_aggregation` 明确断言并守护该兜底；改动会破坏该测试与单平台正确性。
- 真正的歧义只在**多平台**部署下：无法区分「后台管理操作」与「某平台会话」。

**改进方案（待决策，不擅自改）**：多平台场景下，由 web 路由在 `purpose_scope` 之外显式打平台标记——例如在 `persona.py` / `kb.py` 的 LLM 调用处包 `with with_platform(get_current_platform() or "web")` 或显式传 `platform="web"` 哨兵值。当前用户主部署为钉钉单平台，**无紧迫性**，列为已知限制。

---

## 🟠 Defect C（MEDIUM，方案）：本地模型成本恒为 $0，成本被低估

**现象**：`history.estimate_cost` / `get_model_price` 只认内置 `_MODEL_PRICING` 与 `config.llm.model_pricing`（按模型名子串匹配）。本地模型（bge-m3 嵌入、rerank/CrossEncoder、本地 Ollama/qwen 服务等）不匹配 → 单价 `{"input":0,"output":0}` → **成本恒为 $0**。用户本次特别点名的「技能提示词生成 / 主人画像生成」若走云端模型则正常计费；但若任一层路由到本地模型，其 token 计入而成本记为 0，成本页 `cost_cny` 系统性低估。

**影响**：对「成本/质量」页的成本口径准确性有持续偏倚，但不影响 token 计数与用途分布（那些是真实的）。

**改进方案（待决策）**：
1. **最简**：在 `config.yaml`（live）的 `llm.model_pricing` 中为本地模型补名义单价（即便 $0 也显式写出，语义明确），或按本地推理的电费/折算填一个近似 $/M token；
2. **展示侧**：对 `is_estimated` 或本地模型行在成本页标注「本地模型不计费 / 估算未含」，避免误导；
3. 不擅自改计价默认值——这是配置决策，需你拍板。

---

## 附：其他观察（非缺陷，记录备查）

- **台账与热库同文件**：`llm_usage` 与 `routing_quality` / `tool_execution_logs` 同在 `linkora.db`，清理/记账共享同一库锁。当前 LLM 调用频率低，短连接 + `busy_timeout=5000` 可接受；若未来高频（如大量工具结果清洗并发），需关注锁竞争。
- **`get_daily_cost_usd` 用 `ts LIKE ?`**：随 A 的索引落地，已走 `idx_llm_usage_ts` 前缀匹配，效率可接受。
- **跨线程 purpose 传播**：`purpose_scope` 基于 ContextVar，若未来 `client.chat()` 被丢进 `run_in_executor` 且生成器在另一线程迭代，用途归属会丢失。当前所有调用路径均在设置作用域的同一线程内，暂无风险。

---

## 改动文件清单（本轮）

| 文件 | 改动 |
|------|------|
| `src/llm/usage_ledger.py` | +`ts` 索引、`USAGE_RETENTION_DAYS`、`cleanup_old_usage()` |
| `src/platform/memory.py` | 全局清理调度器接入 `cleanup_old_usage()` |
| `tests/test_usage_ledger.py` | +索引测试、+保留期清理测试（16 passed） |
