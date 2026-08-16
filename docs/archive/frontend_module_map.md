# 前端业务架构与数据组织（重构后模块划分 + card↔endpoint 映射）

> 本文档是「前端架构梳理」方案的实施落地记录与映射表。
> 原则：**轻量重构、保持 15 页路由不变（ALLOWED_PAGES）、后端端点零改动**，仅前端收敛与导航优化。

## 一、业务域导航分组（已实现）

| 分组 | 页面（data-page） | 说明 |
|---|---|---|
| 总览 | dashboard | 系统健康 + 最近活动 |
| 消息中心 | messages / deadletters / drafts | 消息域 |
| 知识管理 | rag | KB + 钉钉文档 + 记忆 |
| 规则与意图 | keywords / intent | 关键词规则 + 意图路由 + 决策 |
| 智能引擎 | skills / tools | 技能 + 工具链路 |
| 可观测性 | metrics / cost-quality | 从「智能引擎」拆出，集中指标/成本/质量 |
| 主人画像 | persona | 从「智能引擎」拆出，独立域 |
| 系统 | config | 配置 |
| 调试工具 | simulate / logs | 模拟测试 / 运行日志 |

 persona 导航文案统一为「主人风格画像」（与 app.js titles 一致）。

## 二、共享组件层（已实现，`web/static/js/components/`）

| 组件 | 文件 | 职责 | 消费方 |
|---|---|---|---|
| KpiCard | `kpiCard.js` + `css/components/kpi.css` | 统一 KPI 卡片渲染 | cost-quality（已采用） |
| ChartCard | `chartCard.js` | 图表 canvas 生命周期（ensure/destroy/空态） | cost-quality（已采用） |
| DataTable | `dataTable.js` | 通用数据表 + 空态 | cost-quality 引文表（已采用） |
| DecisionTable | `decisionTable.js` | 决策行统一渲染（lite/full） | dashboard(lite) / intent(full)（已采用） |
| Panel | `panel.js` | 面板与空态辅助 | 预留 |
| StateBadge | `stateBadge.js` + `css/components/badge.css` | 状态徽标 | 预留 |

> `routetrace.js` / `persona.js` 原先各自重复定义 `setText`/`escapeHtml`/`showToast`，
> 规范实现位于 `core/app.js`（全局），组件统一复用之。

## 三、领域服务层（已实现骨架，`web/static/js/services/`）

`ObservabilityService`（`observabilityService.js`）：
- 持有共享时间窗 `state.timeRangeHours`（默认 24）。
- `loadAll()` → 并行拉取 summary / confidence-hist / trend / citations → 写入 store 切片 `data.observability.*`。
- cost-quality 页已切换为经 `ObservabilityService` 取数，杜绝「一卡双源」。

`store.js` 新增 `slice / setSlice / subscribeSlice` 领域切片 API（兼容既有 `subscribe/set/get`）。

## 四、card↔endpoint 规范映射（去重后）

| 业务域 | 规范端点 | 归属页面 | 共享组件 |
|---|---|---|---|
| 成本 / Token | `/api/cost-quality/summary`（含 ¥/$ + 时间窗） | cost-quality（主） | KpiCard / ChartCard |
| 置信度分布 | `/api/cost-quality/confidence-hist` | cost-quality | ChartCard（柱状） |
| 质量率 | `summary.totals`（来自 `/api/cost-quality/summary` 响应字段） | cost-quality | ChartCard（环图） |
| 成本/转人工趋势 | `/api/cost-quality/trend` | cost-quality | ChartCard（折线双轴） |
| 引文页脚 | `/api/cost-quality/citations` | cost-quality | DataTable |
| 决策 | `/api/decisions`（+stats/history） | intent（全） / dashboard（lite） | DecisionTable |
| 路由质量 | `/api/routing-quality/{stats,aggregate}` | routetrace | ChartCard / DataTable |
| 延迟 / 来源 / 技能 | `/api/llm-metrics` | metrics | ChartCard |
| 消息分析 | `/api/stats/messages` | 消息中心 | ChartCard |
| 工具 | `/api/metrics/tools` `/api/tools-chain` | tools | DataTable / KpiCard |

**已消除的重叠组**
- G（KPI 手写四套）→ cost-quality 已用 `KpiCard`；dashboard/persona/metrics 也已接入 `KpiCard`（P4 ✅，见下方待办表）。
- F（决策历史双源）→ `DecisionTable` 统一，dashboard(intent lite) 与 intent(full) 共用。

## 五、待办（P4 / P5，已完成 & 剩余）

| 项 | 内容 | 风险 | 状态 |
|---|---|---|---|
| P4 可观测三页 service 化 | routetrance 改走 `RoutingQualityService` | 低 | ✅ **完成** |
| P4 其余页接入组件 | persona KPI 改用 `KpiCard` | 低 | ✅ **完成** |
| P4 metrics KPI 接入 KpiCard | 成本卡统一读 `/api/cost-quality/summary`，¥/$ 归一化 | 低 | ✅ **完成** |
| P4 Store 切片消费端打通 | routetrace / dashboard 订阅 routingQuality 切片 | 低 | ✅ **完成** |
| P5 Dashboard 精简 | 背压/防抖下沉至 Service | 低 | ✅ **完成** |
| P5 死面端点标注 | backend-only 端点清单见下方 | 无 | ✅ **完成** |

> 以上均保持「后端端点不变」。P4/P5 核心改造已全部落地，余下需浏览器手验的是视觉一致性（KpiCard 替换 span 后的布局微调）。


### Backend-only 端点清单（前端无独立页面直接消费）

- `/api/backpressure-metrics` – 背压指标，仅被 metrics/dashboard 可靠性面板消费
- `/api/debounce-metrics` – 防抖指标，同上
- `/api/poller-status` – 轮询器状态，被 metrics 可靠性面板消费

