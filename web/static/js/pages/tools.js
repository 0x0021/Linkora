// ============ pages/tools.js ============
// 工具调用链路可视化，展示每个已注册工具的状态：
// - whitelisted / disabled_by_whitelist
// - blocked_by_skill（停用技能级联屏蔽）
// 让「到底会不会调 dws」一目了然。

let _toolsData = [];  // 缓存全量数据用于客户端过滤

function statusBadge(status) {
    switch (status) {
        case 'active':
            return '<span class="badge badge-success">✅ 已启用</span>';
        case 'disabled_whitelist':
            return '<span class="badge badge-warning">⚠️ 白名单屏蔽</span>';
        case 'disabled_skill':
            return '<span class="badge badge-danger">🚫 技能停用屏蔽</span>';
        default:
            return '<span class="badge badge-secondary">❓ 未知</span>';
    }
}

function sourceLabel(source) {
    return source === 'builtin'
        ? '<span class="pill pill-intent">内置</span>'
        : '<span class="pill pill-skill">技能</span>';
}

function renderCategoryTags(categories) {
    if (!categories || categories.length === 0) return '<span class="text-gray-500 text-sm">—</span>';
    return categories.map(c => `<span class="pill pill-category">${escapeHtml(c)}</span>`).join(' ');
}

function renderToolsList() {
    const query = (document.getElementById('tools-filter-query') || {}).value || '';
    const statusFilter = (document.getElementById('tools-filter-status') || {}).value || 'all';
    const sourceFilter = (document.getElementById('tools-filter-source') || {}).value || 'all';

    const filtered = _toolsData.filter(function (t) {
        // 名称搜索
        if (query) {
            const q = query.toLowerCase();
            const name = (t.display_name || t.name || '').toLowerCase();
            const id = (t.name || '').toLowerCase();
            if (name.indexOf(q) === -1 && id.indexOf(q) === -1) return false;
        }
        // 状态过滤
        if (statusFilter !== 'all' && t.status !== statusFilter) return false;
        // 来源过滤
        if (sourceFilter !== 'all' && t.source !== sourceFilter) return false;
        return true;
    });

    const container = document.getElementById('tools-content');
    if (!container) return;

    const blockedCount = _toolsData.filter(function (t) { return t.status !== 'active'; }).length;

    const summaryHtml = '\n        <div class="tools-summary">\n'
        + '            <span class="tools-summary-item">📦 已注册 <strong>' + _toolsData.length + '</strong> 个</span>\n'
        + (blockedCount > 0
            ? '<span class="tools-summary-item warn">🚫 被屏蔽 <strong>' + blockedCount + '</strong> 个</span>\n'
            : '<span class="tools-summary-item ok">✅ 无屏蔽</span>\n')
        + '            <span class="tools-summary-item">筛选后 <strong>' + filtered.length + '</strong> 个</span>\n'
        + '        </div>';

    // 过滤栏
    const filterHtml = '\n        <div class="tools-filter-bar">\n'
        + '            <input type="text" id="tools-filter-query" class="form-control form-control-sm" placeholder="搜索…" value="' + escapeHtml(document.getElementById('tools-filter-query') ? document.getElementById('tools-filter-query').value : '') + '" oninput="renderToolsList()" style="width:140px;">\n'
        + '            <select id="tools-filter-status" class="form-select form-select-sm" onchange="renderToolsList()" style="width:100px;">\n'
        + '                <option value="all">全部状态</option>\n'
        + '                <option value="active">已启用</option>\n'
        + '                <option value="disabled_whitelist">白名单屏蔽</option>\n'
        + '                <option value="disabled_skill">技能停用屏蔽</option>\n'
        + '            </select>\n'
        + '            <select id="tools-filter-source" class="form-select form-select-sm" onchange="renderToolsList()" style="width:100px;">\n'
        + '                <option value="all">全部来源</option>\n'
        + '                <option value="builtin">内置</option>\n'
        + '                <option value="skill">技能</option>\n'
        + '            </select>\n'
        + '            <button class="btn btn-sm btn-outline-secondary" onclick="loadToolsPage()" style="margin-left:auto"><i class="fa-solid fa-rotate"></i> 刷新</button>\n'
        + '        </div>';

    // 恢复筛选框的值（页面重渲染时保留）
    const prevStatus = document.getElementById('tools-filter-status');
    const prevSource = document.getElementById('tools-filter-source');
    const statusVal = prevStatus ? prevStatus.value : 'all';
    const sourceVal = prevSource ? prevSource.value : 'all';

    // Always render list with header, even when empty
    let rows = '';
    if (filtered.length === 0) {
        rows = '\n            <div class="tool-row tool-empty-row">\n'
            + '                <div class="tool-name-col" style="grid-column:1/-1;text-align:center;padding:3rem 1rem;">\n'
            + '                    <div class="tool-empty-placeholder"><i class="fa-solid fa-screwdriver-wrench" style="font-size:2rem;opacity:.5;"></i><p style="margin-top:.5rem;color:var(--text-tertiary);font-size:.875rem;">无匹配工具</p></div>\n'
            + '                </div>\n'
            + '            </div>';
    } else {
    for (let i = 0; i < filtered.length; i++) {
        const t = filtered[i];
        const desc = t.description ? escapeHtml(t.description) : '—';
        rows += '\n            <div class="tool-row' + (t.status !== 'active' ? ' tool-row-blocked' : '') + '">\n'
            + '                <div class="tool-name-col">\n'
            + '                    <div class="tool-name" title="' + escapeHtml(t.display_name || t.name) + '">' + escapeHtml(t.display_name || t.name) + '</div>\n'
            + '                </div>\n'
            + '                <div class="tool-id-col">\n'
            + '                    <div class="tool-id" title="' + escapeHtml(t.name) + '">' + escapeHtml(t.name) + '</div>\n'
            + '                </div>\n'
            + '                <div class="tool-desc-col" title="' + desc + '">' + desc + '</div>\n'
            + '                <div class="tool-cats">' + renderCategoryTags(t.intent_categories) + '</div>\n'
            + '                <div class="tool-source">' + sourceLabel(t.source) + '</div>\n'
            + '                <div class="tool-status-col">' + statusBadge(t.status) + '</div>\n'
            + '            </div>';
    }
    } // end else

    container.innerHTML = summaryHtml + filterHtml
        + '\n        <div class="tools-list">\n'
        + '            <div class="tool-row tool-row-header">\n'
        + '                <div class="tool-name-col">工具名</div>\n'
        + '                <div class="tool-id-col">ID</div>\n'
        + '                <div class="tool-desc-col">描述</div>\n'
        + '                <div class="tool-cats">域类别</div>\n'
        + '                <div class="tool-source">来源</div>\n'
        + '                <div class="tool-status-col">状态</div>\n'
        + '            </div>\n'
        + rows + '\n        </div>';

    // 恢复筛选值
    const sf2 = document.getElementById('tools-filter-status');
    const sc2 = document.getElementById('tools-filter-source');
    if (sf2) sf2.value = statusVal;
    if (sc2) sc2.value = sourceVal;
}

async function loadToolsPage() {
    const container = document.getElementById('tools-content');
    if (!container) return;

    try {
        const data = await api.fetch('/api/tools-chain');
        if (!data || data.available === false) {
            _toolsData = [];
            container.innerHTML = '<div class="tools-summary"><span class="tools-summary-item">📦 已注册 <strong>0</strong> 个</span></div>'
                + '<div class="tools-list"><div class="tool-row tool-row-header"><div class="tool-name-col">工具名</div><div class="tool-id-col">ID</div><div class="tool-desc-col">描述</div><div class="tool-cats">域类别</div><div class="tool-source">来源</div><div class="tool-status-col">状态</div></div>'
                + '<div class="tool-row tool-empty-row"><div class="tool-name-col" style="grid-column:1/-1;text-align:center;padding:3rem 1rem;"><div class="tool-empty-placeholder"><i class="fa-solid fa-triangle-exclamation" style="font-size:2rem;opacity:.5;color:#f59e0b;"></i><p style="margin-top:.5rem;color:var(--text-tertiary);font-size:.875rem;">工具数据不可用: ' + escapeHtml((data && data.reason) || 'bot 未就绪') + '</p></div></div></div>'
                + '</div>';
            return;
        }

        _toolsData = data.tools || [];
        renderToolsList();
    } catch (e) {
        _toolsData = [];
        container.innerHTML = '<div class="tools-summary"><span class="tools-summary-item">📦 已注册 <strong>0</strong> 个</span></div>'
            + '<div class="tools-list"><div class="tool-row tool-row-header"><div class="tool-name-col">工具名</div><div class="tool-id-col">ID</div><div class="tool-desc-col">描述</div><div class="tool-cats">域类别</div><div class="tool-source">来源</div><div class="tool-status-col">状态</div></div>'
            + '<div class="tool-row tool-empty-row"><div class="tool-name-col" style="grid-column:1/-1;text-align:center;padding:3rem 1rem;"><div class="tool-empty-placeholder"><i class="fa-solid fa-triangle-exclamation" style="font-size:2rem;opacity:.5;color:#ef4444;"></i><p style="margin-top:.5rem;color:var(--text-tertiary);font-size:.875rem;">加载失败: ' + escapeHtml(e.message) + '</p></div></div></div>'
            + '</div>';
    }
}
window.loadToolsPage = loadToolsPage;

// ============ P5-C: 工具调用排名（与仪表盘同源 /api/stats/tools） ============

async function loadToolsToolStats() {
    const container = document.getElementById('tools-rank-container');
    if (!container) return;
    // HTML 模板残留 .ts-ranking-list（旧 5 列网格）会把内部的 .ts-rank-table 压成 ~1/5 宽，
    // 导致工具名被截断、数值被挤成横排。渲染新表格前移除该 class，让表格占满容器。
    container.classList.remove('ts-ranking-list');
    container.innerHTML = '<div class="empty-state" style="padding:16px"><i class="fa-solid fa-spinner fa-spin"></i><p>加载中…</p></div>';
    try {
        const data = await api.getToolStats({ period: 7, top_n: 20 });
        const tools = data.tools || [];
        if (tools.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon"><i class="fa-solid fa-chart-simple"></i></div><p>暂无工具调用数据</p></div>';
            setText('tools-ts-total', '0');
            setText('tools-ts-rate', '—');
            setText('tools-ts-duration', '—');
            setText('tools-ts-count', '0');
            return;
        }
        tools.sort((a, b) => b.total_calls - a.total_calls);
        const totalCalls = tools.reduce((s, t) => s + (t.total_calls || 0), 0);
        const avgSuccessRate = tools.reduce((s, t) => s + (t.success_rate || 0), 0) / tools.length;
        const avgDuration = tools.reduce((s, t) => s + (t.avg_duration_ms || 0), 0) / tools.length;
        setText('tools-ts-total', totalCalls.toLocaleString());
        const rateEl = document.getElementById('tools-ts-rate');
        if (rateEl) {
            rateEl.textContent = avgSuccessRate.toFixed(0) + '%';
            rateEl.className = 'ts-summary-value ' + (avgSuccessRate >= 90 ? 'rate-high' : avgSuccessRate >= 70 ? 'rate-medium' : 'rate-low');
        }
        setText('tools-ts-duration', Math.round(avgDuration) + 'ms');
        setText('tools-ts-count', String(tools.length));
        const maxCalls = tools[0].total_calls || 1;
        // 优化布局：使用更紧凑的表格格式，减少视觉噪音
        let html = '<div class="ts-rank-table">';
        // 表头
        html += '<div class="ts-rank-header">';
        html += '<span class="ts-rank-col ts-rank-col-rank">排名</span>';
        html += '<span class="ts-rank-col ts-rank-col-name">工具名称</span>';
        html += '<span class="ts-rank-col ts-rank-col-calls">调用次数</span>';
        html += '<span class="ts-rank-col ts-rank-col-rate">成功率</span>';
        html += '</div>';
        
        // 数据行（只显示前 10 名）
        const displayCount = Math.min(tools.length, 10);
        for (let i = 0; i < displayCount; i++) {
            const tool = tools[i];
            const successRate = tool.success_rate || 0;
            const rateClass = successRate >= 90 ? 'rate-high' : successRate >= 70 ? 'rate-medium' : 'rate-low';
            const rankClass = i < 3 ? `rank-${i + 1}` : '';
            html += `<div class="ts-rank-row ${rankClass}">`;
            html += `<span class="ts-rank-col ts-rank-col-rank"><span class="ts-rank-badge ${rankClass}">${i + 1}</span></span>`;
            html += `<span class="ts-rank-col ts-rank-col-name" title="${escapeHtml(tool.tool_name)}">${escapeHtml(tool.display_name || tool.tool_name)}</span>`;
            html += `<span class="ts-rank-col ts-rank-col-calls">${tool.total_calls.toLocaleString()}</span>`;
            html += `<span class="ts-rank-col ts-rank-col-rate ${rateClass}">${successRate.toFixed(0)}%</span>`;
            html += '</div>';
        }
        html += '</div>';
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<div class="empty-state"><div class="empty-icon"><i class="fa-solid fa-triangle-exclamation"></i></div><p>加载失败: ' + escapeHtml(e.message) + '</p></div>';
    }
}
// 在 loadToolsPage 完成后自动加载工具排名
const _origLoadTools = window.loadToolsPage;
window.loadToolsPage = function() {
    _origLoadTools();
    setTimeout(loadToolsToolStats, 100);
};
