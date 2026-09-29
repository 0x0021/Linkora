// ============ pages/cost_quality.js ============
// 成本 / 质量看板（Roadmap ③）：成本(¥) + 质量标记率 + 反馈有用率 + 置信度分布
// 数据流：ObservabilityService.loadAll() → store 切片(data.observability.*) → 本页读取渲染
// 组件：KpiCard / ChartCard / DataTable（消除原直连 api.fetch + 四套 KPI 手写实现）

let _cqPolling = null;

// 百分比格式化
function cqFmtPct(v) {
    if (v == null || v === 0) return "0%";
    return (v * 100).toFixed(1) + "%";
}

// ¥ 成本格式化（CNY）
function cqFmtCostCny(cny) {
    if (cny == null || cny === 0) return "¥0.00";
    if (cny < 0.01) return "¥" + cny.toFixed(4);
    return "¥" + cny.toFixed(2);
}

// KPI 卡片定义（统一 KpiCard；容器 id 即卡片 id，由 index.html 提供空壳）
// 成本/Token 四卡读「LLM 用量台账」全口径（含对话外消耗：摘要/记忆/画像/技能等），
// routing_quality 口径仅覆盖回复链路，保留在平台对比表中。
const _CQ_KPIS = [
    { id: "cq-kpi-cost",          label: "总成本（近 24h）", icon: '<i class="fa-solid fa-yen-sign"></i>',   sub: "全口径 LLM 成本折合人民币（USD×汇率）" },
    { id: "cq-kpi-tokens",        label: "总 Token 消耗",   icon: '<i class="fa-solid fa-coins"></i>',      sub: "全口径（回复+摘要+记忆+画像+技能等）" },
    { id: "cq-kpi-input-tokens",  label: "输入 Token",      icon: '<i class="fa-solid fa-arrow-down"></i>', sub: "全口径累计输入 Token" },
    { id: "cq-kpi-output-tokens", label: "输出 Token",      icon: '<i class="fa-solid fa-arrow-up"></i>',   sub: "全口径累计输出 Token" },
    { id: "cq-kpi-handoff",       label: "低置信转人工率",  icon: '<i class="fa-solid fa-hand"></i>',       sub: "触发草稿推主人占比" },
    { id: "cq-kpi-rag",           label: "RAG 命中率",      icon: '<i class="fa-solid fa-book-open"></i>',  sub: "知识库命中占比" },
    { id: "cq-kpi-cited",         label: "引文页脚命中率",  icon: '<i class="fa-solid fa-quote-right"></i>', sub: "实际追加溯源占比" },
    { id: "cq-kpi-feedback",      label: "反馈有用率",      icon: '<i class="fa-solid fa-thumbs-up"></i>',  sub: "用户正向反馈占比" },
];

// 用途中文标签（后端 usage_ledger purpose 标识 → 展示名）
const _CQ_PURPOSE_LABELS = {
    reply: "对话回复",
    summary: "对话摘要",
    memory: "记忆提取/合并",
    persona: "主人画像",
    skill: "技能意图生成",
    tool: "工具结果清洗",
    kb: "知识库处理",
    other: "其他",
};

function cqRenderEmptyKpis() {
    _CQ_KPIS.forEach(k => renderKpiCard(k.id, { label: k.label, icon: k.icon, sub: k.sub, value: "—" }));
}

function cqRenderKpis(summary) {
    const t = (summary && summary.totals) || {};
    // 全口径（台账）：含对话外消耗；台账不可用（旧数据/异常）时回退 routing_quality 口径
    const u = (summary && summary.llm_usage && summary.llm_usage.available && summary.llm_usage.totals) || null;
    const costCny = u ? u.cost_cny : (t.total_cost_cny || 0);
    const map = {
        "cq-kpi-cost":          cqFmtCostCny(costCny),
        "cq-kpi-tokens":        metricsFmtTokens(u ? u.total_tokens : (t.total_tokens || 0)),
        "cq-kpi-input-tokens":  metricsFmtTokens(u ? u.input_tokens : (t.total_input_tokens || 0)),
        "cq-kpi-output-tokens": metricsFmtTokens(u ? u.output_tokens : (t.total_output_tokens || 0)),
        "cq-kpi-handoff":       cqFmtPct(t.handoff_rate),
        "cq-kpi-rag":           cqFmtPct(t.rag_grounded_rate),
        "cq-kpi-cited":         cqFmtPct(t.cited_rate),
        "cq-kpi-feedback":      cqFmtPct(t.feedback_useful_rate),
    };
    _CQ_KPIS.forEach(k => renderKpiCard(k.id, {
        label: k.label, icon: k.icon, sub: k.sub, value: (k.id in map ? map[k.id] : "—"),
    }));
    cqRenderUsageTable(summary && summary.llm_usage);
}

// 用途分布表：全口径按用途聚合（调用数 / 输入 / 输出 / 总量 / 成本），含估算标记
function cqRenderUsageTable(llmUsage) {
    const host = document.getElementById("cq-usage-table");
    if (!host) return;
    if (!llmUsage || !llmUsage.available) {
        host.innerHTML = '<div class="metrics-empty">台账暂不可用（重启服务产生新调用后逐步生成）</div>';
        return;
    }
    const byPurpose = llmUsage.by_purpose || {};
    const order = Object.keys(byPurpose).sort((a, b) => (byPurpose[b].total_tokens || 0) - (byPurpose[a].total_tokens || 0));
    if (order.length === 0) {
        host.innerHTML = '<div class="metrics-empty">暂无用量记录（服务产生 LLM 调用后开始统计）</div>';
        return;
    }
    const rows = order.map(p => {
        const v = byPurpose[p];
        return {
            purpose: _CQ_PURPOSE_LABELS[p] || p,
            calls: v.calls || 0,
            input_tokens: v.input_tokens || 0,
            output_tokens: v.output_tokens || 0,
            total_tokens: v.total_tokens || 0,
            cost: cqFmtCostCny(v.cost_cny || 0),
            est: (v.estimated_calls || 0) > 0 ? `（${v.estimated_calls} 次估算）` : "",
        };
    });
    renderDataTable("cq-usage-table", {
        columns: [
            { key: "purpose", label: "用途", render: r => escapeHtml(r.purpose) },
            { key: "calls", label: "调用次数", render: r => String(r.calls) },
            { key: "input_tokens", label: "输入", render: r => metricsFmtTokens(r.input_tokens) },
            { key: "output_tokens", label: "输出", render: r => metricsFmtTokens(r.output_tokens) },
            { key: "total_tokens", label: "合计", render: r => metricsFmtTokens(r.total_tokens) },
            { key: "cost", label: "成本", render: r => {
                // 透明化：本地/未计价模型成本记为 ¥0，标注「不计费」避免误读为真实零花费
                if ((r.cost_cny || 0) === 0 && (r.total_tokens || 0) > 0) {
                    return '<span style="color:#64748b;font-size:11px;" title="该用途使用本地或未计价模型，不计入 API 成本；Token 为真实全口径统计">不计费</span>' + escapeHtml(r.est);
                }
                return escapeHtml(r.cost) + escapeHtml(r.est);
            } },
        ],
        rows,
        emptyText: "暂无用量记录",
    });
    // 成本透明化：全口径有真实 Token 消耗但成本恒 ¥0 → 提示用户这是本地/未计价模型
    const noteEl = document.getElementById("cq-usage-cost-note");
    if (noteEl) {
        const totTokens = order.reduce((s, p) => s + (byPurpose[p].total_tokens || 0), 0);
        const totCost = order.reduce((s, p) => s + (byPurpose[p].cost_cny || 0), 0);
        if (totTokens > 0 && totCost === 0) {
            noteEl.textContent = "⚠️ 当前全部 LLM 调用使用本地 / 未计价模型，成本显示为 ¥0（不计费）；Token 消耗为真实全口径统计。如需折算本地算力成本，可在 config.llm.model_pricing 配置名义单价。";
            noteEl.style.display = "";
        } else {
            noteEl.style.display = "none";
        }
    }
}

function cqChartsEmpty() {
    ChartCard.showEmpty("wrap-chart-cq-confidence", "暂无数据");
    ChartCard.showEmpty("wrap-chart-cq-quality", "暂无数据");
    ChartCard.showEmpty("wrap-chart-cq-trend", "暂无数据");
}

// 置信度分布柱状图（10 桶 0~1）
async function cqRenderConfidenceChart(hist) {
    const id = "chart-cq-confidence";
    const wrap = document.getElementById("wrap-" + id);
    if (!wrap) return;
    if (!hist || hist.length === 0) { ChartCard.showEmpty(wrap, "暂无置信度数据"); return; }
    const ctx = ChartCard.ensureCanvas(wrap, id);
    if (!ctx) return;
    await window.loadChart();
    const ct = chartTheme();
    ChartCard.destroy(id);
    const labels = hist.map(h => h.bucket);
    const values = hist.map(h => h.count);
    const palette = ["#8b5cf6", "#06b6d4", "#f59e0b", "#ec4899", "#16a34a", "#2563eb", "#dc2626", "#0891b2", "#7c3aed", "#ea580c"];
    const chart = new Chart(ctx.canvas, {
        type: "bar",
        data: {
            labels,
            datasets: [{ label: "消息数", data: values, backgroundColor: palette.map(c => c + "cc"), borderColor: palette, borderWidth: 1, borderRadius: 4 }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ctx.parsed.y + " 条" } } },
            scales: {
                y: { beginAtZero: true, ticks: { color: ct.tick, stepSize: 1 }, grid: { color: ct.grid } },
                x: { ticks: { color: ct.tick, font: { size: 10 } }, grid: { display: false } },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
    ChartCard.setChart(id, chart);
}

// 质量率环形图（转人工 / RAG命中 / 引文页脚 / 反馈有用）
async function cqRenderQualityChart(t) {
    const id = "chart-cq-quality";
    const wrap = document.getElementById("wrap-" + id);
    if (!wrap) return;
    const ct = chartTheme();
    ChartCard.destroy(id);
    const items = [
        { label: "转人工率", v: (t.handoff_rate || 0) * 100 },
        { label: "RAG命中率", v: (t.rag_grounded_rate || 0) * 100 },
        { label: "引文页脚率", v: (t.cited_rate || 0) * 100 },
        { label: "反馈有用率", v: (t.feedback_useful_rate || 0) * 100 },
    ];
    if ((t.decision_total || 0) === 0 && (t.feedback_total || 0) === 0) {
        ChartCard.showEmpty(wrap, "暂无质量数据");
        return;
    }
    const ctx = ChartCard.ensureCanvas(wrap, id);
    if (!ctx) return;
    await window.loadChart();
    const palette = ["#f59e0b", "#16a34a", "#8b5cf6", "#ec4899"];
    const chart = new Chart(ctx.canvas, {
        type: "doughnut",
        data: {
            labels: items.map(i => i.label),
            datasets: [{ data: items.map(i => i.v), backgroundColor: palette, borderColor: ct.bg || "#fff", borderWidth: 2 }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "right", labels: { color: ct.tick, font: { size: 11 }, padding: 8 } },
                tooltip: { callbacks: { label: ctx => ctx.label + ": " + ctx.parsed.toFixed(1) + "%" } },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
    ChartCard.setChart(id, chart);
}

// 每日成本趋势折线图
async function cqRenderTrend(series) {
    const id = "chart-cq-trend";
    const wrap = document.getElementById("wrap-" + id);
    if (!wrap) return;
    const ct = chartTheme();
    ChartCard.destroy(id);
    if (!series || series.length === 0) { ChartCard.showEmpty(wrap, "暂无趋势数据"); return; }
    const ctx = ChartCard.ensureCanvas(wrap, id);
    if (!ctx) return;
    await window.loadChart();
    const labels = series.map(s => s.date);
    const costData = series.map(s => s.cost_cny || 0);
    const handoffData = series.map(s => (s.handoff_rate || 0) * 100);
    const chart = new Chart(ctx.canvas, {
        type: "line",
        data: {
            labels,
            datasets: [
                { label: "每日成本(¥)", data: costData, borderColor: "#2563eb", backgroundColor: "rgba(37, 99, 235, 0.12)", fill: true, tension: 0.3, yAxisID: "y" },
                { label: "转人工率(%)", data: handoffData, borderColor: "#f59e0b", backgroundColor: "rgba(245, 158, 11, 0.12)", fill: false, tension: 0.3, yAxisID: "y1" },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { labels: { color: ct.tick, font: { size: 12 } } } },
            scales: {
                y: { beginAtZero: true, position: "left", ticks: { color: ct.tick, callback: v => "¥" + v }, grid: { color: ct.grid } },
                y1: { beginAtZero: true, position: "right", ticks: { color: ct.tick, callback: v => v + "%" }, grid: { drawOnChartArea: false } },
                x: { ticks: { color: ct.tick }, grid: { display: false } },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
    ChartCard.setChart(id, chart);
}

// 引文页脚命中表格（统一 DataTable）
function cqRenderCitations(items) {
    const rows = (items || []).map(it => ({
        sender_name: it.sender_name,
        conversation_name: it.conversation_name,
        intent: it.intent,
        reply_preview: it.reply_preview,
        created_at: it.created_at,
    }));
    renderDataTable("cq-citations-table", {
        columns: [
            { key: "sender_name", label: "发送者", render: r => escapeHtml(r.sender_name || "—") },
            { key: "conversation_name", label: "会话", render: r => escapeHtml(r.conversation_name || "—") },
            { key: "intent", label: "意图", render: r => escapeHtml(r.intent || "—") },
            { key: "reply_preview", label: "回复预览", tdCls: "cq-preview", render: r => escapeHtml((r.reply_preview || "").slice(0, 40) || "—") },
            { key: "created_at", label: "时间", render: r => escapeHtml((r.created_at || "").slice(0, 16)) },
        ],
        rows,
        emptyText: "暂无引文页脚命中记录",
    });
}

// 加载（经 ObservabilityService，统一时间窗）
async function loadCostQualityPage() {
    try {
        const res = await ObservabilityService.loadAll({ days: 7, limit: 20 });
        const summary = res.summary;
        const hist = (res.hist && res.hist.length) ? res.hist : ((summary && summary.confidence_hist) || []);
        const trend = res.trend || [];
        const citations = res.citations || [];
        if (!summary || summary.available === false) {
            cqRenderEmptyKpis();
            cqChartsEmpty();
            cqRenderCitations([]);
            return;
        }
        cqRenderKpis(summary);
        cqRenderConfidenceChart(hist);
        cqRenderQualityChart(summary.totals || {});
        cqRenderTrend(trend);
        cqRenderCitations(citations);
        // Re-apply any active filter
        filterCQTable();
    } catch (e) {
        cqRenderEmptyKpis();
        cqChartsEmpty();
        cqRenderCitations([]);
        showToast("成本质量看板加载失败: " + (e.message || e), "error");
    }
}

/** Client-side filter for cost/quality citations table */
function filterCQTable() {
    var input = document.getElementById('cq-search');
    var query = (input ? input.value : '').trim().toLowerCase();
    var table = document.getElementById('cq-citations-table');
    if (!table) return;
    var rows = table.querySelectorAll('tbody tr');
    rows.forEach(function(row) {
        var text = (row.textContent || '').toLowerCase();
        row.style.display = (!query || text.indexOf(query) >= 0) ? '' : 'none';
    });
}
window.filterCQTable = filterCQTable;

// 轮询
function startCostQualityPolling() {
    stopCostQualityPolling();
    _cqPolling = setInterval(loadCostQualityPage, 30000);
}
function stopCostQualityPolling() {
    if (_cqPolling) {
        clearInterval(_cqPolling);
        _cqPolling = null;
    }
    ["chart-cq-confidence", "chart-cq-quality", "chart-cq-trend"].forEach(ChartCard.destroy);
}

async function exportCostQualityCSV() {
    try {
        await api.exportCostQuality(720);
    } catch (e) {
        showToast('导出失败：' + (e && e.message ? e.message : e), 'error');
    }
}

// 暴露纯格式化函数到命名空间，便于单元测试（延续 Phase 2 护栏；不改运行行为）
window.Linkora = window.Linkora || {};
window.Linkora.cqFmtPct = cqFmtPct;
window.Linkora.cqFmtCostCny = cqFmtCostCny;
