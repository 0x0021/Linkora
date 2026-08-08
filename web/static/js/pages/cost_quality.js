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
const _CQ_KPIS = [
    { id: "cq-kpi-cost",          label: "总成本（近 24h）", icon: '<i class="fa-solid fa-yen-sign"></i>',   sub: "折合人民币（USD×汇率）" },
    { id: "cq-kpi-tokens",        label: "总 Token 消耗",   icon: '<i class="fa-solid fa-coins"></i>',      sub: "累计 LLM Token" },
    { id: "cq-kpi-input-tokens",  label: "输入 Token",      icon: '<i class="fa-solid fa-arrow-down"></i>', sub: "累计输入 Token" },
    { id: "cq-kpi-output-tokens", label: "输出 Token",      icon: '<i class="fa-solid fa-arrow-up"></i>',   sub: "累计输出 Token" },
    { id: "cq-kpi-handoff",       label: "低置信转人工率",  icon: '<i class="fa-solid fa-hand"></i>',       sub: "触发草稿推主人占比" },
    { id: "cq-kpi-rag",           label: "RAG 命中率",      icon: '<i class="fa-solid fa-book-open"></i>',  sub: "知识库命中占比" },
    { id: "cq-kpi-cited",         label: "引文页脚命中率",  icon: '<i class="fa-solid fa-quote-right"></i>', sub: "实际追加溯源占比" },
    { id: "cq-kpi-feedback",      label: "反馈有用率",      icon: '<i class="fa-solid fa-thumbs-up"></i>',  sub: "用户正向反馈占比" },
];

function cqRenderEmptyKpis() {
    _CQ_KPIS.forEach(k => renderKpiCard(k.id, { label: k.label, icon: k.icon, sub: k.sub, value: "—" }));
}

function cqRenderKpis(summary) {
    const t = (summary && summary.totals) || {};
    const map = {
        "cq-kpi-cost":          cqFmtCostCny(t.total_cost_cny || 0),
        "cq-kpi-tokens":        metricsFmtTokens(t.total_tokens || 0),
        "cq-kpi-input-tokens":  metricsFmtTokens(t.total_input_tokens || 0),
        "cq-kpi-output-tokens": metricsFmtTokens(t.total_output_tokens || 0),
        "cq-kpi-handoff":       cqFmtPct(t.handoff_rate),
        "cq-kpi-rag":           cqFmtPct(t.rag_grounded_rate),
        "cq-kpi-cited":         cqFmtPct(t.cited_rate),
        "cq-kpi-feedback":      cqFmtPct(t.feedback_useful_rate),
    };
    _CQ_KPIS.forEach(k => renderKpiCard(k.id, {
        label: k.label, icon: k.icon, sub: k.sub, value: (k.id in map ? map[k.id] : "—"),
    }));
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
