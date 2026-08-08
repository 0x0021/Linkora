// ============ pages/metrics.js ============
// 指标监控页：LLM 推理延迟、路由质量、技能命中分布、多平台聚合
// 数据来自 /api/llm-metrics（多平台聚合路由质量统计）

let _metricsPolling = null;
let _latencyChart = null;
let _skillChart = null;
let _sourceChart = null;
let _tokenChart = null;
let _tokenTrendChart = null;

// 延迟单位格式化
function metricsFmtMs(ms) {
    if (ms == null || ms === 0) return "—";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
    return Math.round(ms) + "ms";
}

// 数字格式化
function metricsFmtNum(n) {
    if (n == null) return "—";
    return n.toLocaleString("zh-CN");
}

// Token 格式化
function metricsFmtTokens(n) {
    if (n == null || n === 0) return "—";
    if (n >= 1000000) return (n / 1000000).toFixed(2) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "K";
    return n.toLocaleString("zh-CN");
}

// 成本格式化
function metricsFmtCost(usd) {
    if (usd == null || usd === 0) return "$0.00";
    if (usd < 0.01) return "$" + usd.toFixed(4);
    return "$" + usd.toFixed(2);
}

// 加载指标数据（路由/延迟/技能质量指标；Token 与成本归属「成本 / 质量」页，避免跨页重复）
async function loadMetricsPage() {
    try {
        const data = await api.fetch("/api/llm-metrics");
        if (!data || data.available === false) {
            metricsRenderEmptyKpis();
            ["chart-metrics-skill","chart-metrics-source","chart-metrics-tokens"].forEach(id => {
                const wrap = document.getElementById(id)?.parentElement;
                if (wrap) wrap.innerHTML = '<div class="metrics-empty">暂无数据</div>';
            });
            const tableEl = document.getElementById("metrics-platform-table");
            if (tableEl) tableEl.innerHTML = '<div class="metrics-empty">暂无数据</div>';
            return;
        }
        renderMetricsKPI(data);
        renderLatencyChart(data);
        renderSkillChart(data);
        renderSourceChart(data);
        renderTokenChart(data);
        renderPlatformBreakdown(data);
        renderTokenTrendChart();
    } catch (e) {
        metricsRenderEmptyKpis();
        showToast("指标加载失败: " + (e.message || e), "error");
    }
}

// KPI 卡片（统一 KpiCard；容器 id 即卡片 id，由 index.html 提供空壳）
const _METRICS_KPIS = [
    { id: "metrics-kpi-total",          label: "路由记录总数", icon: '<i class="fa-solid fa-database"></i>',            sub: "累计路由质量记录" },
    { id: "metrics-kpi-llm",            label: "平均 LLM 延迟", icon: '<i class="fa-solid fa-brain"></i>',              sub: "LLM 推理耗时" },
    { id: "metrics-kpi-total-lat",      label: "平均总延迟",   icon: '<i class="fa-solid fa-stopwatch"></i>',           sub: "含路由+LLM+回复" },
    { id: "metrics-kpi-max-lat",        label: "最大总延迟",   icon: '<i class="fa-solid fa-triangle-exclamation"></i>', sub: "历史峰值" },
    { id: "metrics-kpi-combo",          label: "组合路由",     icon: '<i class="fa-solid fa-layer-group"></i>',         sub: "多技能组合触发" },
    { id: "metrics-kpi-convergence",    label: "收敛触发",     icon: '<i class="fa-solid fa-compress"></i>',            sub: "工具轮次收敛" },
    { id: "metrics-kpi-avg-score",      label: "平均匹配分",   icon: '<i class="fa-solid fa-bullseye"></i>',            sub: "技能路由置信度" },
    { id: "metrics-kpi-avg-candidates", label: "平均候选数",   icon: '<i class="fa-solid fa-list-check"></i>',         sub: "技能匹配候选" },
];

function metricsRenderEmptyKpis() {
    _METRICS_KPIS.forEach(k => renderKpiCard(k.id, { label: k.label, icon: k.icon, sub: k.sub, value: "—" }));
}

function renderMetricsKPI(data) {
    const pct = (data.avg_total_ms > 0) ? ((data.avg_llm_ms / data.avg_total_ms) * 100).toFixed(0) + "%" : "—";
    const vals = {
        "metrics-kpi-total":           metricsFmtNum(data.total || 0),
        "metrics-kpi-llm":             metricsFmtMs(data.avg_llm_ms || 0),
        "metrics-kpi-total-lat":       metricsFmtMs(data.avg_total_ms || 0),
        "metrics-kpi-max-lat":         metricsFmtMs(data.max_total_ms || 0),
        "metrics-kpi-combo":           metricsFmtNum(data.total_combo || 0),
        "metrics-kpi-convergence":     metricsFmtNum(data.total_convergence || 0),
        "metrics-kpi-avg-score":       (data.avg_score || 0).toFixed(3),
        "metrics-kpi-avg-candidates":  (data.avg_candidates || 0).toFixed(1),
    };
    const subs = {
        "metrics-kpi-llm": "占总延迟 " + pct,
    };
    _METRICS_KPIS.forEach(k => {
        let value = (k.id in vals) ? vals[k.id] : "—";
        let sub = subs[k.id] || k.sub;
        renderKpiCard(k.id, { label: k.label, icon: k.icon, sub, value });
    });
}

// 延迟分布柱状图
async function renderLatencyChart(data) {
    const ctx = document.getElementById("chart-metrics-latency");
    if (!ctx) return;
    await window.loadChart();
    const ct = chartTheme();
    if (_latencyChart) _latencyChart.destroy();

    const platforms = Object.keys(data.platforms || {});
    const labels = platforms.length > 0 ? platforms : ["全局"];
    const llmData = platforms.length > 0
        ? platforms.map(p => (data.platforms[p].avg_llm_ms || 0))
        : [data.avg_llm_ms || 0];
    const totalData = platforms.length > 0
        ? platforms.map(p => (data.platforms[p].avg_total_ms || 0))
        : [data.avg_total_ms || 0];

    _latencyChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [
                {
                    label: "LLM 推理延迟",
                    data: llmData,
                    backgroundColor: "rgba(139, 92, 246, 0.7)",
                    borderColor: "#8b5cf6",
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: "总延迟",
                    data: totalData,
                    backgroundColor: "rgba(37, 99, 235, 0.7)",
                    borderColor: "#2563eb",
                    borderWidth: 1,
                    borderRadius: 4,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: ct.tick, font: { size: 12 } } },
                tooltip: {
                    callbacks: {
                        label: function (ctx) {
                            return ctx.dataset.label + ": " + metricsFmtMs(ctx.parsed.y);
                        },
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: { color: ct.tick, callback: v => metricsFmtMs(v) },
                    grid: { color: ct.grid },
                },
                x: { ticks: { color: ct.tick }, grid: { display: false } },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
}

// 技能命中分布（横向柱状图 Top 10）
async function renderSkillChart(data) {
    const wrap = document.getElementById("chart-metrics-skill")?.parentElement;
    if (!wrap) return;
    const ct = chartTheme();
    if (_skillChart) { _skillChart.destroy(); _skillChart = null; }

    const entries = Object.entries(data.by_skill || {}).sort((a, b) => b[1] - a[1]).slice(0, 10);
    if (entries.length === 0) {
        wrap.innerHTML = '<div class="metrics-empty">暂无技能命中数据</div>';
        return;
    }
    // 确保 canvas 存在（空状态可能覆盖过 canvas）
    let canvas = wrap.querySelector("canvas");
    if (!canvas) {
        wrap.innerHTML = '<canvas id="chart-metrics-skill"></canvas>';
        canvas = wrap.querySelector("canvas");
    }
    await window.loadChart();

    const labels = entries.map(e => e[0]);
    const values = entries.map(e => e[1]);
    const palette = [
        "#8b5cf6", "#06b6d4", "#f59e0b", "#ec4899", "#16a34a",
        "#2563eb", "#dc2626", "#0891b2", "#7c3aed", "#ea580c",
    ];

    _skillChart = new Chart(canvas, {
        type: "bar",
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: labels.map((_, i) => palette[i % palette.length] + "cc"),
                borderColor: labels.map((_, i) => palette[i % palette.length]),
                borderWidth: 1,
                borderRadius: 4,
            }],
        },
        options: {
            indexAxis: "y",
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { label: ctx => ctx.parsed.x + " 次" } },
            },
            scales: {
                x: { beginAtZero: true, ticks: { color: ct.tick, stepSize: 1 }, grid: { color: ct.grid } },
                y: { ticks: { color: ct.tick, font: { size: 11 } }, grid: { display: false } },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
}

// 路由来源分布（环形图）
async function renderSourceChart(data) {
    const wrap = document.getElementById("chart-metrics-source")?.parentElement;
    if (!wrap) return;
    const ct = chartTheme();
    if (_sourceChart) { _sourceChart.destroy(); _sourceChart = null; }

    const entries = Object.entries(data.by_source || {});
    if (entries.length === 0) {
        wrap.innerHTML = '<div class="metrics-empty">暂无路由来源数据</div>';
        return;
    }
    let canvas = wrap.querySelector("canvas");
    if (!canvas) {
        wrap.innerHTML = '<canvas id="chart-metrics-source"></canvas>';
        canvas = wrap.querySelector("canvas");
    }
    await window.loadChart();

    const labels = entries.map(e => e[0]);
    const values = entries.map(e => e[1]);
    const palette = ["#2563eb", "#16a34a", "#f59e0b", "#8b5cf6", "#06b6d4", "#ec4899", "#dc2626"];

    _sourceChart = new Chart(canvas, {
        type: "doughnut",
        data: {
            labels,
            datasets: [{
                data: values,
                backgroundColor: labels.map((_, i) => palette[i % palette.length]),
                borderColor: ct.bg || "#fff",
                borderWidth: 2,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { position: "right", labels: { color: ct.tick, font: { size: 11 }, padding: 8 } },
                tooltip: {
                    callbacks: {
                        label: function (ctx) {
                            const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
                            const pct = ((ctx.parsed / total) * 100).toFixed(1);
                            return ctx.label + ": " + ctx.parsed + " 次 (" + pct + "%)";
                        },
                    },
                },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
}

// 平台分解表
function renderPlatformBreakdown(data) {
    const el = document.getElementById("metrics-platform-table");
    if (!el) return;
    const platforms = data.platforms || {};
    const ids = Object.keys(platforms);
    if (ids.length === 0) {
        el.innerHTML = '<div class="metrics-empty">暂无平台数据</div>';
        return;
    }

    const rows = ids.map(pid => {
        const p = platforms[pid];
        return `<tr>
            <td><span class="metrics-pid-badge">${escapeHtml(pid)}</span></td>
            <td>${metricsFmtNum(p.total || 0)}</td>
            <td>${metricsFmtMs(p.avg_llm_ms || 0)}</td>
            <td>${metricsFmtMs(p.avg_total_ms || 0)}</td>
            <td>${metricsFmtMs(p.max_total_ms || 0)}</td>
            <td>${(p.avg_score || 0).toFixed(3)}</td>
            <td>${(p.avg_candidates || 0).toFixed(1)}</td>
            <td>${metricsFmtNum(p.total_combo || 0)}</td>
            <td>${metricsFmtNum(p.total_convergence || 0)}</td>
            <td>${metricsFmtTokens(p.total_tokens || 0)}</td>
            <td>${metricsFmtCost(p.total_cost_usd || 0)}</td>
        </tr>`;
    }).join("");

    el.innerHTML = `
        <table class="metrics-table" id="metrics-platform-tbody">
            <thead>
                <tr>
                    <th>平台</th>
                    <th>总数</th>
                    <th>平均LLM</th>
                    <th>平均总延迟</th>
                    <th>最大延迟</th>
                    <th>平均分</th>
                    <th>平均候选</th>
                    <th>组合</th>
                    <th>收敛</th>
                    <th>Token</th>
                    <th>成本</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

/** Client-side filter for metrics platform breakdown table */
function filterMetricsTable() {
    var input = document.getElementById('metrics-search');
    var query = (input ? input.value : '').trim().toLowerCase();
    var rows = document.querySelectorAll('#metrics-platform-tbody tbody tr');
    rows.forEach(function(row) {
        var text = (row.textContent || '').toLowerCase();
        row.style.display = (!query || text.indexOf(query) >= 0) ? '' : 'none';
    });
}
window.filterMetricsTable = filterMetricsTable;

// Token 消耗柱状图
async function renderTokenChart(data) {
    const ctx = document.getElementById("chart-metrics-tokens");
    if (!ctx) return;
    await window.loadChart();
    const ct = chartTheme();
    if (_tokenChart) _tokenChart.destroy();

    const platforms = Object.keys(data.platforms || {});
    const labels = platforms.length > 0 ? platforms : ["全局"];
    const inputData = platforms.length > 0
        ? platforms.map(p => (data.platforms[p].total_input_tokens || 0))
        : [data.total_input_tokens || 0];
    const outputData = platforms.length > 0
        ? platforms.map(p => (data.platforms[p].total_output_tokens || 0))
        : [data.total_output_tokens || 0];

    _tokenChart = new Chart(ctx, {
        type: "bar",
        data: {
            labels,
            datasets: [
                {
                    label: "输入 Token",
                    data: inputData,
                    backgroundColor: "rgba(37, 99, 235, 0.7)",
                    borderColor: "#2563eb",
                    borderWidth: 1,
                    borderRadius: 4,
                },
                {
                    label: "输出 Token",
                    data: outputData,
                    backgroundColor: "rgba(139, 92, 246, 0.7)",
                    borderColor: "#8b5cf6",
                    borderWidth: 1,
                    borderRadius: 4,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: ct.tick, font: { size: 12 } } },
                tooltip: {
                    callbacks: {
                        label: function (ctx) {
                            return ctx.dataset.label + ": " + metricsFmtTokens(ctx.parsed.y);
                        },
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: { color: ct.tick, callback: v => metricsFmtTokens(v) },
                    grid: { color: ct.grid },
                },
                x: { ticks: { color: ct.tick }, grid: { display: false } },
            },
            animation: { duration: 600, easing: "easeOutQuart" },
        },
    });
}

// 轮询
function startMetricsPolling() {
    stopMetricsPolling();
    _metricsPolling = setInterval(loadMetricsPage, 30000);
}
function stopMetricsPolling() {
    if (_metricsPolling) {
        clearInterval(_metricsPolling);
        _metricsPolling = null;
    }
    if (_latencyChart) { _latencyChart.destroy(); _latencyChart = null; }
    if (_skillChart) { _skillChart.destroy(); _skillChart = null; }
    if (_sourceChart) { _sourceChart.destroy(); _sourceChart = null; }
    if (_tokenChart) { _tokenChart.destroy(); _tokenChart = null; }
    if (_tokenTrendChart) { _tokenTrendChart.destroy(); _tokenTrendChart = null; }
}

// P5 下沉：可靠性面板数据获取改用 DashboardReliabilityService（原 inline fetch → service），同时移除独立 poller fetch
async function loadMetricsReliability() {
    try {
        const section = document.getElementById('metrics-reliability-panel');
        if (section) section.style.display = '';

        // 一次性获取背压 + 防抖 + poller 状态
        const rel = await DashboardReliabilityService.loadReliability();
        if (!rel || rel.available === false) {
            ['metrics-reliability-backpressure','metrics-reliability-debounce'].forEach(id => {
                const g = document.getElementById(id);
                if (g) g.innerHTML = '<div class="empty-state" style="grid-column:1/-1">监控未启用</div>';
            });
            // poller 也置空
            setText('metrics-ps-last-poll', '—');
            setText('metrics-ps-poll-count', '—');
            setText('metrics-ps-queue-depth', '—');
            setText('metrics-ps-last-error', '—');
            setText('metrics-ps-running', '—');
            return;
        }
        const { bp, db, poller } = rel;

        if (!bp || bp.available === false) {
            const g = document.getElementById('metrics-reliability-backpressure');
            if (g) g.innerHTML = '<div class="empty-state" style="grid-column:1/-1">背压监控未启用</div>';
        } else {
            setText('metrics-bp-max-dispatch', bp.max_dispatch_per_cycle ?? '—');
            setText('metrics-bp-max-concurrent', bp.max_concurrent_replies ?? '—');
            setText('metrics-bp-last-dispatched', bp.last_cycle_dispatched ?? '—');
            setText('metrics-bp-last-deferred', bp.last_cycle_deferred ?? '—');
        }

        if (!db || db.available === false) {
            const g = document.getElementById('metrics-reliability-debounce');
            if (g) g.innerHTML = '<div class="empty-state" style="grid-column:1/-1">防抖监控未启用</div>';
        } else {
            setText('metrics-db-pending', db.pending_batches ?? '—');
            setText('metrics-db-delay-count', db.delay_count ?? '—');
            setText('metrics-db-extra-sec', db.extra_sec != null ? db.extra_sec : '—');
            setText('metrics-db-fired-with', db.fired_with_request ?? '—');
        }

        // poller 状态直接用 rel.poller（loadMetricsPoller 已移除，逻辑合并至此处）
        if (!poller || poller.available === false) {
            setText('metrics-ps-last-poll', '—');
            setText('metrics-ps-poll-count', '—');
            setText('metrics-ps-queue-depth', '—');
            setText('metrics-ps-last-error', '—');
            setText('metrics-ps-running', '—');
        } else {
            const lastPoll = poller.last_poll_at;
            let pollTimeStr = '—';
            if (lastPoll) {
                const delta = (Date.now() - new Date(lastPoll).getTime()) / 1000;
                if (delta < 60) pollTimeStr = Math.round(delta) + 's 前';
                else if (delta < 3600) pollTimeStr = Math.round(delta / 60) + 'm 前';
                else pollTimeStr = Math.round(delta / 3600) + 'h 前';
            }
            setText('metrics-ps-last-poll', pollTimeStr);
            setText('metrics-ps-poll-count', poller.poll_count ?? '—');
            setText('metrics-ps-queue-depth', poller.queue_depth ?? '—');
            const errEl = document.getElementById('metrics-ps-last-error');
            if (errEl) {
                if (poller.last_error) {
                    errEl.textContent = '⚠️ 有异常';
                    errEl.className = 'warn';
                    errEl.title = poller.last_error.slice(0, 300);
                } else {
                    errEl.textContent = '无';
                    errEl.className = 'ok';
                    errEl.title = '';
                }
            }
            const runEl = document.getElementById('metrics-ps-running');
            if (runEl) {
                const isRunning = lastPoll && (Date.now() - new Date(lastPoll).getTime()) < 300000;
                runEl.textContent = isRunning ? '✓ 运行中' : '—';
                runEl.className = isRunning ? 'ok' : '';
            }
        }
    } catch (e) {
        console.error('Failed to load reliability metrics:', e);
        ['metrics-bp-max-dispatch','metrics-bp-max-concurrent','metrics-bp-last-dispatched','metrics-bp-last-deferred',
         'metrics-db-pending','metrics-db-delay-count','metrics-db-extra-sec','metrics-db-fired-with',
         'metrics-ps-last-poll','metrics-ps-poll-count','metrics-ps-queue-depth','metrics-ps-last-error','metrics-ps-running']
            .forEach(id => setText(id, '—'));
    }
}

window.loadMetricsReliability = loadMetricsReliability;

async function exportMetricsCSV() {
    try {
        await api.exportMetrics(24, 10000);
    } catch (e) {
        showToast('导出失败：' + (e && e.message ? e.message : e), 'error');
    }
}

// Token 消耗趋势折线图（从 /api/metrics/tokens 取 hourly 数据）
async function renderTokenTrendChart() {
    const id = "chart-metrics-token-trend";
    const wrap = document.getElementById("wrap-" + id);
    if (!wrap) return;
    const ct = chartTheme();
    if (_tokenTrendChart) { _tokenTrendChart.destroy(); _tokenTrendChart = null; }

    try {
        var tr = await api.fetch("/api/metrics/tokens?time_range_hours=24");
    } catch (e) {
        ChartCard.showEmpty(wrap, "趋势数据加载失败");
        return;
    }
    if (!tr || !tr.available) {
        ChartCard.showEmpty(wrap, "暂无 Token 追踪数据");
        return;
    }

    // 取第一个平台的 hourly 数据，或合并所有平台
    var platforms = Object.keys(tr.platforms || {});
    var hourly = [];
    if (platforms.length > 0) {
        // 合并所有平台的同一小时数据
        var merged = {};
        platforms.forEach(function (p) {
            var platData = tr.platforms[p];
            (platData.hourly || []).forEach(function (h) {
                var key = h.hour;
                if (!merged[key]) merged[key] = { hour: key, total_tokens: 0, record_count: 0, total_cost_usd: 0 };
                merged[key].total_tokens += (h.total_tokens || 0);
                merged[key].record_count += (h.record_count || 0);
                merged[key].total_cost_usd += (h.total_cost_usd || 0);
            });
        });
        hourly = Object.values(merged).sort(function (a, b) { return a.hour.localeCompare(b.hour); });
    } else {
        hourly = tr.hourly || [];
    }

    if (!hourly.length) {
        ChartCard.showEmpty(wrap, "暂无逐时数据");
        return;
    }

    var labels = hourly.map(function (h) { return h.hour.substring(11, 16); });
    var tokenData = hourly.map(function (h) { return h.total_tokens || 0; });

    await window.loadChart();
    const ctx = ChartCard.ensureCanvas(wrap, id);
    if (!ctx) return;
    _tokenTrendChart = new Chart(ctx.canvas, {
        type: "line",
        data: {
            labels: labels,
            datasets: [{
                label: "Token 消耗",
                data: tokenData,
                borderColor: "#2563eb",
                backgroundColor: "rgba(37, 99, 235, 0.1)",
                borderWidth: 2,
                pointRadius: 3,
                pointHoverRadius: 5,
                fill: true,
                tension: 0.3,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: ct.tick, font: { size: 12 } } },
                tooltip: {
                    callbacks: {
                        label: function (c) { return "Token: " + metricsFmtTokens(c.parsed.y); },
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    ticks: { color: ct.tick, callback: function (v) { return metricsFmtTokens(v); } },
                    grid: { color: ct.grid },
                },
                x: { ticks: { color: ct.tick, maxTicksLimit: 12 }, grid: { display: false } },
            },
            animation: { duration: 800, easing: "easeOutQuart" },
        },
    });
}

// 显式挂到 window：经典 <script> 中顶层 function 本就是全局，这里仅为在 ESM/测试环境下也能访问，
// 便于 vitest 对纯格式化函数做单元断言（浏览器内等价于 no-op）。
if (typeof window !== 'undefined') {
    window.metricsFmtMs = metricsFmtMs;
    window.metricsFmtNum = metricsFmtNum;
    window.metricsFmtTokens = metricsFmtTokens;
    window.metricsFmtCost = metricsFmtCost;
}
