// ============ pages/intent.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ Intent & Routing ============

let _intentPollTimer = null;
let _activeIntentTab = 'overview';
window._activeIntentTab = _activeIntentTab;

async function loadIntentPage() {
    switchIntentTab('overview');
    try {
        await Promise.all([loadIntentTaxonomy(), loadDecisions()]);
        startIntentPolling();
    } catch (e) {
        console.error('loadIntentPage failed:', e);
        showToast('意图页面加载失败', 'error');
    }
}

async function loadIntentTaxonomy() {
    try {
        const data = await api.fetch('/api/intents');
        // 概览卡
        const mode = data.meta?.routing_mode || 'smart';
        const modeEl = document.getElementById('routing-mode-value');
        const modeDescEl = document.getElementById('routing-mode-desc');
        if (modeEl) modeEl.textContent = mode;
        if (modeDescEl) modeDescEl.textContent = data.meta?.routing_mode_desc || '';
        const tc = document.getElementById('tools-count-value');
        if (tc) tc.textContent = (data.meta?.tools_count ?? '—');

        renderIntentLayer(data.layers?.disposition || [], 'disposition-intents', 'disposition');
        renderIntentLayer(data.layers?.action || [], 'action-intents', 'action');
        renderToolActionMap(data.tool_action_map || {});
        loadSkillIntentMap();
    } catch (e) {
        console.error('加载意图体系失败', e);
        const el = document.getElementById('disposition-intents');
        if (el) el.innerHTML = `<div class="alert alert-danger">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

function renderIntentLayer(cats, containerId, kind) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!cats.length) {
        el.innerHTML = `<div class="empty-state"><i class="fa-solid fa-inbox" style="font-size:1.75rem;opacity:.4;"></i><p style="margin-top:.5rem;">暂无</p></div>`;
        return;
    }
    el.innerHTML = kind === 'disposition' ? renderDisposition(cats) : renderAction(cats);
}

// 证据词芯片：截断展示 + 剩余计数
function icEvidenceHtml(c, max) {
    const ev = c.evidence_keywords || [];
    if (!ev.length) return '';
    const shown = ev.slice(0, max).map(k => `<span class="ic-badge">${escapeHtml(k)}</span>`).join(' ');
    const total = c.evidence_keyword_count || ev.length;
    const more = ev.length > max ? ` <span class="ic-more">+${ev.length - max}</span>` : '';
    return `<div class="ic-meta"><b>证据词：</b>${shown}${more}<span class="ic-count">共 ${total}</span></div>`;
}

// 处置层：business / social 两大类的真实层级（social 含子型，路由跳过）
function renderDisposition(cats) {
    const roots = cats.filter(c => !c.parent);
    const kidsByParent = {};
    for (const c of cats) if (c.parent) (kidsByParent[c.parent] ||= []).push(c);
    const totalEv = cats.reduce((s, c) => s + (c.evidence_keyword_count || 0), 0);

    let html = `<div class="layer-summary">处置层 · <b>${roots.length}</b> 大类` +
        (kidsByParent['social'] ? ` / 社交含 <b>${kidsByParent['social'].length}</b> 子型` : '') +
        ` · 共 <b>${totalEv}</b> 证据词 · 业务优先于社交</div>`;

    for (const root of roots) {
        const kids = kidsByParent[root.id] || [];
        const skip = root.id === 'social';
        html += `<div class="intent-group ${root.id}">`;
        html += `<div class="ig-head"><span class="ic-id">${escapeHtml(root.name || root.id)}</span>` +
            `<span class="ic-badge">${escapeHtml(root.id)}</span>` +
            (skip ? `<span class="pill pill-skip">跳过处理</span>` : '') + `</div>`;
        html += `<div class="ic-def">${escapeHtml(root.definition || '')}</div>`;
        if (kids.length) {
            html += `<div class="ig-children">`;
            for (const k of kids) {
                html += `<div class="intent-subcard">`;
                html += `<div class="isc-head"><span class="isc-name">${escapeHtml(k.name || k.id)}</span>` +
                    `<span class="ic-badge">${escapeHtml(k.id)}</span></div>`;
                html += `<div class="ic-def">${escapeHtml(k.definition || '')}</div>`;
                html += `<div class="ic-meta"><b>触发：</b>${escapeHtml(k.trigger || '—')}</div>`;
                html += icEvidenceHtml(k, 6);
                html += `</div>`;
            }
            html += `</div>`;
        }
        html += `</div>`;
    }
    return html;
}

// 行动层：5 类正交意图，每张卡闭合「映射到哪些工具」（与 stage3 工具映射呼应）
function renderAction(cats) {
    const toolSet = new Set();
    cats.forEach(c => (c.tools || []).forEach(t => toolSet.add(t)));
    let html = `<div class="layer-summary">行动层 · <b>${cats.length}</b> 类正交意图` +
        (toolSet.size ? ` · 覆盖 <b>${toolSet.size}</b> 个工具 · 彼此可共存` : '') + `</div>`;

    for (const c of cats) {
        const proactive = c.id === 'action.monitor' || c.id === 'action.subscribe';
        html += `<div class="intent-card action${proactive ? ' proactive' : ''}">`;
        html += `<div class="ic-head"><span class="ic-id">${escapeHtml(c.name || c.id)}</span>` +
            `<span class="ic-badge">${escapeHtml(c.id)}</span>` +
            (proactive ? `<span class="pill pill-mode">主动</span>` : '') + `</div>`;
        html += `<div class="ic-def">${escapeHtml(c.definition || '')}</div>`;
        html += `<div class="ic-meta"><b>触发：</b>${escapeHtml(c.trigger || '—')}</div>`;
        html += icEvidenceHtml(c, 6);
        if (c.tools && c.tools.length) {
            const tools = c.tools.map(t => `<span class="ic-tool">${escapeHtml(t)}</span>`).join('');
            html += `<div class="ic-tools"><b>映射工具：</b><span class="ic-tool-wrap">${tools}</span></div>`;
        }
        html += `</div>`;
    }
    return html;
}

function renderToolActionMap(map) {
    const el = document.getElementById('tool-action-map');
    if (!el) return;
    const entries = Object.entries(map);
    if (!entries.length) {
        el.innerHTML = `<div class="empty-state"><i class="fa-solid fa-inbox" style="font-size:1.75rem;opacity:.4;"></i><p style="margin-top:.5rem;">暂无</p></div>`;
        return;
    }
    let html = '';
    for (const [tool, cats] of entries) {
        const tags = (cats || []).map(c => `<span class="tam-tag">${escapeHtml(c)}</span>`).join('');
        html += `<div class="tam-row"><span class="tam-tool">${escapeHtml(tool)}</span><span class="tam-tags">${tags}</span></div>`;
    }
    el.innerHTML = html;
}

// 技能 → 意图类别映射：从 /api/skills 读取每个技能的 intent_categories
async function loadSkillIntentMap() {
    const el = document.getElementById('skill-intent-map');
    if (!el) return;
    try {
        const data = await api.fetch('/api/skills');
        renderSkillIntentMap(data.skills || []);
    } catch (e) {
        console.error('加载技能意图映射失败', e);
        if (el) el.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#f59e0b;"></i><p style="margin-top:.5rem;">加载失败</p></div>`;
    }
}

function renderSkillIntentMap(skills) {
    const el = document.getElementById('skill-intent-map');
    if (!el) return;
    if (!skills.length) {
        el.innerHTML = `<div class="empty-state"><i class="fa-solid fa-puzzle-piece" style="font-size:1.75rem;opacity:.4;"></i><p style="margin-top:.5rem;">暂无技能或未启用</p></div>`;
        return;
    }
    // 启用优先，其次按名称排序
    const list = [...skills].sort(
        (a, b) => ((b.enabled ? 1 : 0) - (a.enabled ? 1 : 0)) || String(a.name).localeCompare(String(b.name))
    );
    let html = '';
    for (const s of list) {
        const cats = s.intent_categories || [];
        const tags = cats.length
            ? cats.map(c => `<span class="tam-tag">${escapeHtml(c)}</span>`).join('')
            : `<span class="sim-none">未声明</span>`;
        const cls = s.enabled === false ? ' tam-row sim-disabled' : ' tam-row';
        html += `<div class="${cls.trim()}"><span class="tam-tool">${escapeHtml(s.name)}</span><span class="tam-tags">${tags}</span></div>`;
    }
    el.innerHTML = html;
}

async function loadDecisions() {
    try {
        const platform = window.store?.getPlatform ? window.store.getPlatform() : 'dingtalk';
        const data = await api.fetch(`/api/decisions?n=60&platform=${encodeURIComponent(platform)}`);
        const list = data.decisions || [];
        const countEl = document.getElementById('decisions-count-value');
        if (countEl) countEl.textContent = (data.total ?? list.length);
        const feed = document.getElementById('decisions-feed');
        if (!feed) return;
        if (!list.length) {
            feed.innerHTML = `<div class="empty-state"><i class="fa-solid fa-message" style="font-size:1.75rem;opacity:.4;"></i><p style="margin-top:.5rem;">暂无决策记录，发一条消息试试</p></div>`;
            return;
        }
        renderDecisionFeed('decisions-feed', list, { emptyText: '暂无决策记录，发一条消息试试' });
    } catch (e) {
        console.error('加载决策失败', e);
        showToast('决策流加载失败', 'error');
    }
}

function actionLabel(a) {
    return ({ 'skip': '跳过', 'reply-rule': '规则回复', 'llm': 'LLM 处理' })[a] || a;
}

function startIntentPolling() {
    stopIntentPolling();
    _intentPollTimer = setInterval(async () => {
        if (currentPage !== 'intent') return;
        try { await loadDecisions(); } catch (e) { /* ignore */ }
    }, 5000);
}

function stopIntentPolling() {
    if (_intentPollTimer) { clearInterval(_intentPollTimer); _intentPollTimer = null; }
}

async function refreshIntentPage() {
    try {
        await Promise.all([loadIntentTaxonomy(), loadDecisions()]);
    } catch (e) {
        console.error('refreshIntentPage failed:', e);
        showToast('刷新失败', 'error');
    }
}
window.refreshIntentPage = refreshIntentPage;


// ============ 决策追踪子页面 ============

let _decisionsHistoryPage = 1;
let _decisionsHistoryFilters = {};

function switchIntentTab(tab) {
    document.querySelectorAll('.intent-tab-btn').forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
    });
    const btn = document.querySelector(`.intent-tab-btn[data-tab="${tab}"]`);
    if (btn) {
        btn.classList.add('active');
        btn.setAttribute('aria-selected', 'true');
    }
    document.querySelectorAll('.intent-tab-pane').forEach(p => {
        p.classList.remove('active');
        p.style.display = 'none';
    });
    const pane = document.getElementById(`intent-tab-${tab}`);
    if (pane) {
        pane.classList.add('active');
        pane.style.display = '';
    }
    _activeIntentTab = tab;
    window._activeIntentTab = tab;
    if (tab === 'history') {
        stopIntentPolling();
        stopRouteTracePolling();
        loadDecisionsStats();
        loadDecisionsHistory(1);
    } else if (tab === 'routetrace') {
        stopIntentPolling();
        loadRouteTrace(1);
        startRouteTracePolling();
    } else {
        stopRouteTracePolling();
        startIntentPolling();
    }
}
window.switchIntentTab = switchIntentTab;

async function loadDecisionsStats() {
    try {
        const platform = window.store?.getPlatform ? window.store.getPlatform() : 'dingtalk';
        const data = await api.fetch(`/api/decisions/stats?platform=${encodeURIComponent(platform)}`);
        document.getElementById('stats-total').textContent = data.total ?? '—';
        document.getElementById('stats-llm').textContent = (data.by_action?.llm ?? 0);
        const ruled = (data.by_action?.['reply-rule'] ?? 0) + (data.by_action?.skip ?? 0);
        document.getElementById('stats-ruled').textContent = ruled;
        document.getElementById('stats-senders').textContent = Object.keys(data.by_sender || {}).length;
        // 技能统计
        document.getElementById('stats-skill').textContent = data.skill_activated ?? 0;
        const bySkill = data.by_skill || {};
        const topSkill = Object.entries(bySkill).sort((a, b) => b[1] - a[1])[0];
        document.getElementById('stats-skill-top').textContent = topSkill ? `${topSkill[0]} (${topSkill[1]}次)` : '暂无激活记录';

        // 填充筛选下拉
        fillSelect('filter-sender', data.senders || []);
        fillSelect('filter-intent', data.intents || []);
        fillSelect('filter-action', data.actions || []);
    } catch (e) {
        console.error('加载决策统计失败', e);
        showToast('决策统计加载失败', 'error');
    }
}

function fillSelect(id, options) {
    const sel = document.getElementById(id);
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML = '';
    const defaultOpt = document.createElement('option');
    defaultOpt.value = '';
    defaultOpt.textContent = id === 'filter-sender' ? '全部发送者' : (id === 'filter-intent' ? '全部意图' : '全部动作');
    sel.appendChild(defaultOpt);
    for (const o of options) {
        const opt = document.createElement('option');
        opt.value = o;
        opt.textContent = o;
        sel.appendChild(opt);
    }
    sel.value = currentVal;
}

async function loadDecisionsHistory(page) {
    _decisionsHistoryPage = page || 1;
    const filters = {
        sender_name: document.getElementById('filter-sender')?.value || '',
        intent: document.getElementById('filter-intent')?.value || '',
        action: document.getElementById('filter-action')?.value || '',
        time_filter: document.getElementById('filter-time')?.value || '',
    };
    _decisionsHistoryFilters = filters;

    const platform = window.store?.getPlatform ? window.store.getPlatform() : 'dingtalk';
    let url = `/api/decisions/history?page=${_decisionsHistoryPage}&page_size=20&platform=${encodeURIComponent(platform)}`;
    if (filters.sender_name) url += `&sender_name=${encodeURIComponent(filters.sender_name)}`;
    if (filters.intent) url += `&intent=${encodeURIComponent(filters.intent)}`;
    if (filters.action) url += `&action=${encodeURIComponent(filters.action)}`;
    if (filters.time_filter) url += `&time_filter=${encodeURIComponent(filters.time_filter)}`;

    try {
        const data = await api.fetch(url);
        renderDecisionsHistory(data);
    } catch (e) {
        console.error('加载决策历史失败', e);
        document.getElementById('decisions-history-body').innerHTML = `<div class="alert alert-danger">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}
window.loadDecisionsHistory = loadDecisionsHistory;

function renderDecisionsHistory(data) {
    const body = document.getElementById('decisions-history-body');
    const items = data.items || [];
    if (!items.length) {
        body.innerHTML = `<div class="empty-state"><i class="fa-solid fa-clipboard-list" style="font-size:1.75rem;opacity:.4;"></i><p style="margin-top:.5rem;">暂无决策记录</p></div>`;
        document.getElementById('decisions-pagination').innerHTML = '';
        return;
    }

    let html = `<div class="table-wrap"><table class="table table-sm table-hover" style="font-size: 13px; margin: 0; width: 100%;">
        <thead><tr>
            <th style="min-width: 100px;">时间</th>
            <th style="min-width: 70px;">发送者</th>
            <th style="min-width: 80px;">会话</th>
            <th style="min-width: 50px;">动作</th>
            <th style="min-width: 120px;">内容预览</th>
            <th style="min-width: 80px;">意图</th>
            <th style="min-width: 60px;">技能</th>
            <th style="min-width: 70px;">路由模式</th>
            <th style="min-width: 80px;">路由工具</th>
            <th style="min-width: 80px;">回复预览</th>
        </tr></thead><tbody>`;

    for (const d of items) {
        const ts = (d.created_at || '').replace('T', ' ');
        const intentPill = d.intent ? `<span class="pill pill-intent">${escapeHtml(d.intent)}</span>` : '';
        const modePill = d.routing_mode ? `<span class="pill pill-mode">${escapeHtml(d.routing_mode)}</span>` : '';
        const tools = (d.routed_tools || []).map(t => `<span class="tam-tag">${escapeHtml(t)}</span>`).join('');
        const actionPill = `<span class="pill pill-${escapeHtml(d.action)}">${actionLabel(d.action)}</span>`;
        const skillBadge = d.skill_name ? `<span class="pill pill-skill" title="来源: ${escapeHtml(d.skill_source || '')}">${escapeHtml(d.skill_name)}</span>` : '<span style="color:var(--text-tertiary);font-size:12px">—</span>';
        html += `<tr>
            <td style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 12px; color: var(--text-tertiary);" title="${escapeHtml(ts)}">${escapeHtml(ts)}</td>
            <td style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(d.sender_name || d.sender_id || '')}">${escapeHtml(d.sender_name || d.sender_id || '—')}</td>
            <td style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: 12px; color: var(--text-tertiary);" title="${escapeHtml(d.conversation_name || d.conversation_id || '')}">${escapeHtml(d.conversation_name || d.conversation_id || '—')}</td>
            <td style="white-space: nowrap;">${actionPill}</td>
            <td style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(d.content_preview || '')}">${escapeHtml(d.content_preview || '')}</td>
            <td style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(d.intent || '')}">${intentPill}</td>
            <td style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${skillBadge}</td>
            <td style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${escapeHtml(d.routing_mode || '')}">${modePill}</td>
            <td style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px;" title="${escapeHtml((d.routed_tools || []).join(', '))}">${tools || '—'}</td>
            <td style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--text-tertiary); font-style: italic;" title="${escapeHtml(d.reply_preview || '')}">${escapeHtml(d.reply_preview || '')}</td>
        </tr>`;
    }
    html += '</tbody></table></div>';
    body.innerHTML = html;

    // 分页
    renderDecisionsPagination(data);
}

function renderDecisionsPagination(data) {
    const total = data.total || 0;
    const page = data.page || 1;
    const pageSize = data.page_size || 20;
    const totalPages = Math.ceil(total / pageSize);
    const container = document.getElementById('decisions-pagination');
    if (totalPages <= 1) {
        container.innerHTML = '';
        return;
    }
    let html = `<nav><ul class="pagination pagination-sm">`;
    html += `<li class="page-item ${page <= 1 ? 'disabled' : ''}"><a class="page-link" href="#" onclick="loadDecisionsHistory(${page - 1}); return false;">&laquo;</a></li>`;

    // 带省略号的页码渲染（与 skills.js / routetrace.js 一致）
    const delta = 2;  // 当前页前后各显示 2 页
    const rangeStart = Math.max(1, page - delta);
    const rangeEnd = Math.min(totalPages, page + delta);
    if (rangeStart > 1) {
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadDecisionsHistory(1); return false;">1</a></li>`;
        if (rangeStart > 2) html += `<li class="page-item disabled"><span class="page-link">…</span></li>`;
    }
    for (let i = rangeStart; i <= rangeEnd; i++) {
        html += `<li class="page-item ${i === page ? 'active' : ''}"><a class="page-link" href="#" onclick="loadDecisionsHistory(${i}); return false;">${i}</a></li>`;
    }
    if (rangeEnd < totalPages) {
        if (rangeEnd < totalPages - 1) html += `<li class="page-item disabled"><span class="page-link">…</span></li>`;
        html += `<li class="page-item"><a class="page-link" href="#" onclick="loadDecisionsHistory(${totalPages}); return false;">${totalPages}</a></li>`;
    }

    html += `<li class="page-item ${page >= totalPages ? 'disabled' : ''}"><a class="page-link" href="#" onclick="loadDecisionsHistory(${page + 1}); return false;">&raquo;</a></li>`;
    html += '</ul></nav>';
    container.innerHTML = html;
}

// 跨函数复用的图表实例（全局声明，避免隐式全局依赖 sloppy-mode）
let toolCallsChart = null;

function renderToolCallsChart(canvas, tools) {
    if (toolCallsChart) {
        toolCallsChart.destroy();
    }
    const ct = chartTheme();
    
    const ctx = canvas.getContext('2d');
    const labels = tools.map(t => t.display_name || t.tool_name);
    const calls = tools.map(t => t.total_calls);
    const successRates = tools.map(t => t.success_rate);
    
    // 根据成功率生成颜色
    const barColors = successRates.map(rate => {
        if (rate >= 90) return 'rgba(22, 163, 74, 0.7)';
        if (rate >= 70) return 'rgba(245, 158, 11, 0.7)';
        return 'rgba(220, 38, 38, 0.7)';
    });
    
    toolCallsChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: '调用次数',
                data: calls,
                backgroundColor: barColors,
                borderColor: barColors.map(c => c.replace('0.7', '1')),
                borderWidth: 1,
                borderRadius: 4,
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            layout: {
                padding: { top: 4, bottom: 4, left: 4, right: 8 }
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: ct.tooltipBg,
                    titleColor: ct.tooltipText,
                    bodyColor: ct.tooltipText,
                    borderColor: ct.tooltipBorder,
                    borderWidth: 1,
                    callbacks: {
                        afterLabel: function(context) {
                            const idx = context.dataIndex;
                            return `成功率: ${successRates[idx].toFixed(1)}%`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    beginAtZero: true,
                    ticks: { precision: 0, font: { size: 10 }, color: ct.tick },
                    grid: { color: ct.grid }
                },
                y: {
                    ticks: { font: { size: 10 }, color: ct.tick },
                    grid: { color: ct.grid }
                }
            }
        }
    });
}

// 配置导出
async function exportConfig() {
    try {
        const response = await fetch(api._withPlatform('/api/config/export'), { headers: api.getAuthHeaders() });
        const data = await response.json();
        
        const blob = new Blob([data.config], { type: 'text/yaml;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `config_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.yaml`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        
        showToast('配置导出成功');
    } catch (e) {
        showToast(`导出失败: ${e.message}`, 'error');
    }
}

// 配置导入
async function importConfig(input) {
    const file = input.files[0];
    if (!file) return;
    
    const statusDiv = document.getElementById('config-status');
    if (statusDiv) {
        statusDiv.innerHTML = `<span style="color: var(--text-secondary);">${iconize("⏳")} 上传中...</span>`;
    }
    
    try {
        const formData = new FormData();
        formData.append('file', file);
        
        const response = await fetch(api._withPlatform('/api/config/import'), {
            method: 'POST',
            headers: api.getAuthHeaders(),
            body: formData,
        });
        
        const result = await response.json();
        
        if (!response.ok) {
            throw new Error(result.detail || '导入失败');
        }
        
        if (statusDiv) {
            statusDiv.innerHTML = `<span style="color: #16a34a;">${iconize("✅")} ${escapeHtml(result.message)}</span>`;
        }
        showToast('配置导入成功');

        // 仅当当前停留在仪表盘时才刷新其数据；切回仪表盘时 switchPage 会自动 loadDashboard
        setTimeout(() => {
            if (currentPage === 'dashboard') loadDashboardData();
        }, 1000);
    } catch (e) {
        if (statusDiv) {
            statusDiv.innerHTML = `<span style="color: #dc2626;">${iconize("❌")} ${escapeHtml(e.message)}</span>`;
        }
        showToast(`导入失败: ${e.message}`, 'error');
    } finally {
        input.value = '';
    }
}


async function exportDecisionsCSV() {
    try {
        await api.exportDecisions({limit: 10000});
    } catch (e) {
        showToast('导出失败：' + (e && e.message ? e.message : e), 'error');
    }
}
