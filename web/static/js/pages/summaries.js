// ============ pages/summaries.js ============
// 「对话摘要」页：实时展示近期 per-conversation 对话摘要明细 +
// 每日聚合摘要文本（与钉钉 17:30 主动推送同源）。
// 数据源：GET /api/summaries?window=today|yesterday|7days（conversation_summaries 表，多平台隔离）。

let summariesPolling = null;
let currentWindow = 'today';
const WINDOW_LABELS = { today: '今日', yesterday: '昨日', '7days': '近七天' };

function startSummariesPolling() {
    if (summariesPolling) return;
    summariesPolling = setInterval(loadSummariesPage, 30000);
    loadSummariesPage();
}

function stopSummariesPolling() {
    if (summariesPolling) {
        clearInterval(summariesPolling);
        summariesPolling = null;
    }
}

function setWindow(w) {
    currentWindow = w;
    document.querySelectorAll('.summaries-window-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.window === w);
    });
    loadSummariesPage();
}

// 各会话摘要明细列表（按 updated_at 倒序）——精致化卡片
function renderSummaryList(items) {
    if (!items || !items.length) {
        return `<div class="metrics-empty"><i class="fa-regular fa-folder-open"></i> ${WINDOW_LABELS[currentWindow] || '今日'}暂无对话摘要。<br>对话摘要调度器生成摘要后将在此实时显示。</div>`;
    }
    const lis = items.map(function (it, idx) {
        const name = escapeHtml(it.chat_name || it.chat_id || '未知对话');
        const time = it.updated_at ? formatTsLocal(it.updated_at) : '';
        const cnt = it.covered_count != null ? it.covered_count : 0;
        const platform = it.platform || '';
        const platformLabel = ({dingtalk: '钉钉', wecom: '企微', feishu: '飞书'})[platform] || platform;
        const summaryHtml = simpleMarkdown(it.summary || '');
        return `
        <div class="summary-item">
            <div class="summary-item-side">
                <div class="summary-avatar"><i class="fa-solid fa-comments"></i></div>
                <div class="summary-idx">${idx + 1}</div>
            </div>
            <div class="summary-item-main">
                <div class="summary-item-head">
                    <span class="summary-name">${name}</span>
                    ${platformLabel ? `<span class="summary-tag summary-tag-platform">${platformLabel}</span>` : ''}
                    <span class="summary-tag summary-tag-count"><i class="fa-solid fa-message"></i> ${cnt} 条</span>
                </div>
                <div class="summary-text">${summaryHtml}</div>
                <div class="summary-meta">
                    <span class="summary-meta-item"><i class="fa-regular fa-clock"></i> ${time}</span>
                </div>
            </div>
        </div>`;
    }).join('');
    return `<div class="summary-list">${lis}</div>`;
}

async function loadSummariesPage() {
    const body = document.getElementById('summaries-body');
    if (!body) return;
    try {
        const r = await api.fetch(`/api/summaries?limit=30&window=${currentWindow}`);
        if (!r || r.error) {
            if (body.innerHTML.indexOf('summaries-error') === -1) {
                body.innerHTML = `<div class="summaries-error"><i class="fa-solid fa-triangle-exclamation"></i> 摘要加载失败（${r && r.error ? r.error : '未知错误'}），将自动重试…</div>`;
            }
            return;
        }
        const digest = r.digest || '';
        const digestHtml = digest
            ? `<div class="panel summary-digest-panel">
                   <div class="panel-header"><h3><i class="fa-solid fa-clipboard-list"></i> ${WINDOW_LABELS[r.window] || '摘要'}汇总</h3></div>
                   <div class="panel-body summary-digest">${simpleMarkdown(digest)}</div>
               </div>`
            : '';

        // 分析卡片：4 张 hero 风格，每张不同渐变色 + 副信息
        const totalCovers = (r.items || []).reduce((s, it) => s + (it.covered_count || 0), 0);
        const platforms = new Set(r.items?.map(it => it.platform).filter(Boolean));
        const latestTime = r.items?.length ? Math.max(...r.items.map(it => it.updated_at ? new Date(it.updated_at).getTime() : 0)) : 0;
        const lastUpdate = latestTime ? formatTsLocal(new Date(latestTime).toISOString()) : '—';
        const windowHint = {today: '今日 00:00 至今', yesterday: '昨日全天', '7days': '近 7 天'}[currentWindow] || '';

        const analyticsHtml = `
            <div class="summaries-stats">
                <div class="stat-card stat-card-blue">
                    <div class="stat-icon"><i class="fa-solid fa-clipboard-list"></i></div>
                    <div class="stat-info">
                        <div class="stat-value">${r.count || 0}</div>
                        <div class="stat-label">摘要条数</div>
                        <div class="stat-hint">${windowHint}</div>
                    </div>
                </div>
                <div class="stat-card stat-card-green">
                    <div class="stat-icon"><i class="fa-solid fa-comment-dots"></i></div>
                    <div class="stat-info">
                        <div class="stat-value">${totalCovers}</div>
                        <div class="stat-label">覆盖消息</div>
                        <div class="stat-hint">摘要涉及原始消息</div>
                    </div>
                </div>
                <div class="stat-card stat-card-purple">
                    <div class="stat-icon"><i class="fa-solid fa-comments"></i></div>
                    <div class="stat-info">
                        <div class="stat-value">${platforms.size || 0}</div>
                        <div class="stat-label">活跃会话</div>
                        <div class="stat-hint">${platforms.size > 1 ? '跨 ' + platforms.size + ' 平台' : (platforms.size === 1 ? '单平台' : '暂无数据')}</div>
                    </div>
                </div>
                <div class="stat-card stat-card-orange">
                    <div class="stat-icon"><i class="fa-solid fa-clock"></i></div>
                    <div class="stat-info">
                        <div class="stat-value">${lastUpdate}</div>
                        <div class="stat-label">最新摘要</div>
                        <div class="stat-hint">实时同步 · 30s</div>
                    </div>
                </div>
            </div>`;

        body.innerHTML = `
            <div class="summaries-window-bar">
                <button class="summaries-window-btn${currentWindow === 'today' ? ' active' : ''}" data-window="today" onclick="setWindow('today')">
                    <i class="fa-solid fa-sun"></i> 今日
                </button>
                <button class="summaries-window-btn${currentWindow === 'yesterday' ? ' active' : ''}" data-window="yesterday" onclick="setWindow('yesterday')">
                    <i class="fa-solid fa-moon"></i> 昨日
                </button>
                <button class="summaries-window-btn${currentWindow === '7days' ? ' active' : ''}" data-window="7days" onclick="setWindow('7days')">
                    <i class="fa-solid fa-calendar-week"></i> 近七天
                </button>
            </div>
            ${analyticsHtml}
            ${digestHtml}
            <div class="panel summary-list-panel">
                <div class="panel-header"><h3><i class="fa-solid fa-list-ul"></i> 各会话摘要</h3><span class="panel-tag">${WINDOW_LABELS[r.window] || '摘要'} · ${r.count || 0} 条</span></div>
                <div class="panel-body">${renderSummaryList(r.items)}</div>
            </div>
            <div class="summaries-foot"><i class="fa-regular fa-clock"></i> 最后更新：${formatTsLocal(r.generated_at)}${r.platform ? ' · 平台 ' + escapeHtml(r.platform) : ''}</div>
        `;
    } catch (e) {
        if (body.innerHTML.indexOf('summaries-error') === -1) {
            body.innerHTML = `<div class="summaries-error"><i class="fa-solid fa-triangle-exclamation"></i> 摘要刷新失败，将自动重试…</div>`;
        }
    }
}

window.loadSummariesPage = loadSummariesPage;
window.startSummariesPolling = startSummariesPolling;
window.stopSummariesPolling = stopSummariesPolling;
window.setWindow = setWindow;
