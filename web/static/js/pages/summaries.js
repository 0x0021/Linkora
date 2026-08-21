// ============ pages/summaries.js ============
// 「对话摘要」页：实时展示近期 per-conversation 对话摘要明细 +
// 每日聚合摘要文本（与钉钉 17:30 主动推送同源）。
// 数据源：GET /api/summaries?window=today|yesterday|7days（conversation_summaries 表，多平台隔离）。

let summariesPolling = null;
let currentWindow = '7days';
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

// 各会话摘要明细列表（按 updated_at 倒序）
function renderSummaryList(items) {
    if (!items || !items.length) {
        return `<div class="metrics-empty">${WINDOW_LABELS[currentWindow] || '今日'}暂无对话摘要。对话摘要调度器生成摘要后将在此实时显示。</div>`;
    }
    const lis = items.map(function (it) {
        const name = escapeHtml(it.chat_name || it.chat_id || '未知对话');
        const time = it.updated_at ? formatTsLocal(it.updated_at) : '';
        const cnt = it.covered_count != null ? it.covered_count : 0;
        return `
        <div class="summary-item">
            <div class="summary-item-head">
                <span class="summary-name"><i class="fa-solid fa-comments"></i> ${name}</span>
                <span class="summary-meta">覆盖 ${cnt} 条 · ${time}</span>
            </div>
            <div class="summary-text">${simpleMarkdown(it.summary || '')}</div>
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

        // 分析卡片数据
        const totalCovers = (r.items || []).reduce((s, it) => s + (it.covered_count || 0), 0);
        const platforms = new Set(r.items?.map(it => it.platform).filter(Boolean));
        const latestTime = r.items?.length ? Math.max(...r.items.map(it => it.updated_at ? new Date(it.updated_at).getTime() : 0)) : 0;
        const lastUpdate = latestTime ? formatTsLocal(new Date(latestTime).toISOString()) : '—';

        const analyticsHtml = `
            <div class="summaries-analytics">
                <div class="analytics-card">
                    <div class="analytics-icon"><i class="fa-solid fa-clipboard-list"></i></div>
                    <div class="analytics-value">${r.count || 0}</div>
                    <div class="analytics-label">摘要条数</div>
                </div>
                <div class="analytics-card">
                    <div class="analytics-icon"><i class="fa-solid fa-comment-dots"></i></div>
                    <div class="analytics-value">${totalCovers}</div>
                    <div class="analytics-label">覆盖消息</div>
                </div>
                <div class="analytics-card">
                    <div class="analytics-icon"><i class="fa-solid fa-comments"></i></div>
                    <div class="analytics-value">${platforms.size || 0}</div>
                    <div class="analytics-label">活跃会话</div>
                </div>
                <div class="analytics-card">
                    <div class="analytics-icon"><i class="fa-solid fa-clock"></i></div>
                    <div class="analytics-value">${lastUpdate}</div>
                    <div class="analytics-label">最新摘要</div>
                </div>
            </div>`;

        body.innerHTML = `
            <div class="summaries-window-bar">
                <button class="summaries-window-btn${currentWindow === 'today' ? ' active' : ''}" data-window="today" onclick="setWindow('today')">今日</button>
                <button class="summaries-window-btn${currentWindow === 'yesterday' ? ' active' : ''}" data-window="yesterday" onclick="setWindow('yesterday')">昨日</button>
                <button class="summaries-window-btn${currentWindow === '7days' ? ' active' : ''}" data-window="7days" onclick="setWindow('7days')">近七天</button>
            </div>
            ${analyticsHtml}
            ${digestHtml}
            <div class="panel">
                <div class="panel-header"><h3><i class="fa-solid fa-list"></i> 各会话摘要（${WINDOW_LABELS[r.window] || '摘要'} · ${r.count || 0}）</h3></div>
                <div class="panel-body">${renderSummaryList(r.items)}</div>
            </div>
            <div class="summaries-foot">最后更新：${formatTsLocal(r.generated_at)}${r.platform ? ' · 平台 ' + escapeHtml(r.platform) : ''}</div>
        `;
    } catch (e) {
        // 瞬时网络错误：保留上一次内容，仅提示
        if (body.innerHTML.indexOf('summaries-error') === -1) {
            body.innerHTML = `<div class="summaries-error"><i class="fa-solid fa-triangle-exclamation"></i> 摘要刷新失败，将自动重试…</div>`;
        }
    }
}

window.loadSummariesPage = loadSummariesPage;
window.startSummariesPolling = startSummariesPolling;
window.stopSummariesPolling = stopSummariesPolling;
window.setWindow = setWindow;
