// ============ pages/summaries.js v4 ============
// 修复 undefined 显示问题：确保所有字段有默认值
// 「对话摘要」页 - 结构化卡片 + 顶部工具栏
// 数据源：GET /api/summaries?window=today|yesterday|7days

let summariesPolling = null;
let currentWindow = 'today';
const WINDOW_LABELS = { today: '今日', yesterday: '昨日', '7days': '近七天' };
const WINDOW_HINTS = {
    today: '今日 00:00 至今',
    yesterday: '昨日全天',
    '7days': '近 7 天'
};
const PLATFORM_ICON = {
    dingtalk: 'qq',
    wecom: 'wechat',
    feishu: 'slack'
};
const PLATFORM_LABEL = {
    dingtalk: '钉钉',
    wecom: '企微',
    feishu: '飞书'
};

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

function avatarLetter(name) {
    if (!name) return '?';
    const c = name.trim().charAt(0);
    return /[\u4e00-\u9fa5]/.test(c) ? c : c.toUpperCase();
}

function avatarGradient(name) {
    const palettes = [
        ['#22D3EE', '#6366F1'],
        ['#34D399', '#10B981'],
        ['#F472B6', '#DB2777'],
        ['#FBBF24', '#F97316'],
        ['#A78BFA', '#7C3AED'],
        ['#60A5FA', '#2563EB'],
    ];
    let h = 0;
    for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) % palettes.length;
    return palettes[h];
}

// 从摘要文本里轻量提取主题/关键词标签
function extractTopics(summary) {
    if (!summary) return [];
    const text = summary.replace(/^【对话摘要】\s*/, '');
    const topics = [];
    const patterns = [
        /围绕\s*([^，。；\.]{2,12})\s*(展开|讨论|沟通|问题)/,
        /关于\s*([^，。；\.]{2,12})\s*(问题|事项|安排|情况)/,
        /([^，。；\.]{2,10})(问题|事项|安排|需求|功能|系统)/,
        /(采购|销售|HR|人事|考勤|外勤|SRM|钉钉|企业通|企微|飞书)/,
    ];
    for (const re of patterns) {
        const m = text.match(re);
        if (m) {
            const t = m[1] + (m[2] || '');
            if (t && !topics.includes(t) && topics.length < 3) topics.push(t);
        }
    }
    return topics.slice(0, 3);
}

function extractTodo(summary) {
    if (!summary) return '';
    const text = summary.replace(/^【对话摘要】\s*/, '');
    const m = text.match(/(?:建议|需要|应该|应当|最好|可以|请|需)\s*([^。；\n]{2,30})/);
    return m ? m[0].slice(0, 28) : '';
}

// 今日汇总：解析 digest 并与 items 元数据关联，渲染为结构化卡片网格
function renderDigestCards(digest, items) {
    if (!digest) return '';
    const metaByName = new Map();
    for (const it of items || []) {
        const key = it.chat_name || it.chat_id;
        if (key) metaByName.set(key, it);
    }

    const lines = digest.split('\n');
    let header = '';
    let countBadge = 0;
    const entries = [];
    let inHeader = true;
    for (const raw of lines) {
        const line = raw.trim();
        if (!line) continue;
        if (inHeader) {
            header = line;
            const cm = line.match(/共\s*(\d+)\s*段/);
            if (cm) countBadge = parseInt(cm[1], 10);
            inHeader = false;
            continue;
        }
        // 匹配 digest 格式：• **名称**：内容
        const m = line.match(/^•\s*\*\*(.+?)\*\*\s*：\s*(.+)$/);
        if (m) {
            entries.push({ name: m[1], content: m[2] });
        }
    }
    if (!entries.length) return renderDigestBlocks(digest);

    const cards = entries.map(e => {
        const meta = metaByName.get(e.name) || {};
        const platform = meta.platform || '';
        const platformLabel = PLATFORM_LABEL[platform] || '';
        const cnt = meta.covered_count != null ? meta.covered_count : 0;
        const time = meta.updated_at ? formatTsLocal(meta.updated_at, false) : '—';
        const name = e.name || '未知对话';
        const topics = extractTopics(e.content);
        const todo = extractTodo(e.content);
        const [c1, c2] = avatarGradient(name);
        const nameInitial = avatarLetter(name);
        const summary = e.content.replace(/^【对话摘要】\s*/, '').trim();

        return `
        <div class="digest-card">
            <div class="digest-card-head">
                <div class="digest-avatar" style="background:linear-gradient(135deg, ${c1}, ${c2})">${nameInitial}</div>
                <div class="digest-card-meta">
                    <div class="digest-card-title">
                        <span class="digest-name">${escapeHtml(name)}</span>
                        ${platformLabel ? `<span class="digest-tag digest-tag-platform"><i class="fa-brands fa-${PLATFORM_ICON[platform] || 'comment'}"></i>${platformLabel}</span>` : ''}
                        ${cnt ? `<span class="digest-tag digest-tag-count"><i class="fa-solid fa-message"></i>${cnt} 条</span>` : ''}
                    </div>
                    <div class="digest-topics">
                        ${topics.map(t => `<span class="digest-topic">${escapeHtml(t)}</span>`).join('')}
                        ${todo ? `<span class="digest-topic digest-topic-todo"><i class="fa-solid fa-check"></i>${escapeHtml(todo)}</span>` : ''}
                    </div>
                </div>
            </div>
            <div class="digest-card-body">${simpleMarkdown(escapeHtml(summary))}</div>
            ${time ? `<div class="digest-card-foot"><i class="fa-regular fa-clock"></i>${time}</div>` : ''}
        </div>`;
    }).join('');

    return { header, countBadge, cards };
}

// 各会话摘要明细列表
function renderSummaryList(items) {
    if (!items || !items.length) {
        return `<div class="metrics-empty">
            <i class="fa-regular fa-folder-open"></i>
            ${WINDOW_LABELS[currentWindow] || '今日'}暂无对话摘要<br>
            <span style="font-size:0.8rem;opacity:0.7">对话摘要调度器生成摘要后将在此实时显示</span>
        </div>`;
    }
    return `<div class="summary-list-compact">${items.map((it, idx) => {
        const name = escapeHtml(it.chat_name || it.chat_id || '未知对话');
        const time = it.updated_at ? formatTsLocal(it.updated_at, false) : '—';
        const cnt = it.covered_count != null ? it.covered_count : 0;
        const platform = it.platform || '';
        const platformLabel = PLATFORM_LABEL[platform] || '';
        const summaryText = (it.summary || '').replace(/^【对话摘要】\s*/, '').trim().slice(0, 80);
        return `
        <div class="summary-item-compact">
            <div class="summary-item-compact-head">
                <span class="summary-name">${name}</span>
                ${platformLabel ? `<span class="summary-tag summary-tag-platform">${platformLabel}</span>` : ''}
                ${cnt ? `<span class="summary-tag summary-tag-count">${cnt} 条</span>` : ''}
                <span class="summary-time">${time}</span>
            </div>
            <div class="summary-item-compact-body">${escapeHtml(summaryText)}${summaryText.length >= 80 ? '…' : ''}</div>
        </div>`;
    }).join('')}</div>`;
}

async function loadSummariesPage() {
    const body = document.getElementById('summaries-body');
    if (!body) return;
    try {
        const r = await api.fetch(`/api/summaries?limit=30&window=${currentWindow}`);
        if (!r || r.error) {
            if (body.innerHTML.indexOf('summaries-error') === -1) {
                body.innerHTML = `<div class="summaries-error">
                    <i class="fa-solid fa-triangle-exclamation"></i>
                    摘要加载失败（${r && r.error ? escapeHtml(r.error) : '未知错误'}）
                </div>`;
            }
            return;
        }

        const digest = r.digest || '';
        const digestResult = digest ? renderDigestCards(digest, r.items || []) : null;
        const digestHtml = digestResult
            ? `<div class="digest-grid">${digestResult.cards}</div>`
            : '';
        const digestCount = digestResult ? digestResult.countBadge : 0;

        // 统计卡片数据
        const totalCovers = (r.items || []).reduce((s, it) => s + (it.covered_count || 0), 0);
        const platforms = new Set((r.items || []).map(it => it.platform).filter(Boolean));
        const latestTime = (r.items || []).length > 0 ? Math.max(...(r.items || []).map(it => it.updated_at ? new Date(it.updated_at).getTime() : 0), 0) : 0;
        const lastUpdate = latestTime ? formatTsLocal(new Date(latestTime).toISOString(), false) : '—';
        const windowHint = WINDOW_HINTS[currentWindow] || '';
        const platformHint = platforms.size > 1
            ? `跨 ${platforms.size} 平台`
            : platforms.size === 1
                ? PLATFORM_LABEL[[...platforms][0]] || [...platforms][0]
                : '暂无数据';

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
                        <div class="stat-hint">摘要涉及原始消息数</div>
                    </div>
                </div>
                <div class="stat-card stat-card-purple">
                    <div class="stat-icon"><i class="fa-solid fa-comments"></i></div>
                    <div class="stat-info">
                        <div class="stat-value">${platforms.size || 0}</div>
                        <div class="stat-label">活跃会话</div>
                        <div class="stat-hint">${platformHint}</div>
                    </div>
                </div>
                <div class="stat-card stat-card-orange">
                    <div class="stat-icon"><i class="fa-solid fa-clock"></i></div>
                    <div class="stat-info">
                        <div class="stat-value">${lastUpdate}</div>
                        <div class="stat-label">最新摘要</div>
                        <div class="stat-hint">实时同步 · 每30秒刷新</div>
                    </div>
                </div>
            </div>`;

        body.innerHTML = `
            <div class="summaries-toolbar">
                <div class="summaries-window-bar">
                    <button class="summaries-window-btn${currentWindow === 'today' ? ' active' : ''}" data-window="today" onclick="setWindow('today')">
                        <i class="fa-solid fa-sun"></i>今日
                    </button>
                    <button class="summaries-window-btn${currentWindow === 'yesterday' ? ' active' : ''}" data-window="yesterday" onclick="setWindow('yesterday')">
                        <i class="fa-solid fa-moon"></i>昨日
                    </button>
                    <button class="summaries-window-btn${currentWindow === '7days' ? ' active' : ''}" data-window="7days" onclick="setWindow('7days')">
                        <i class="fa-solid fa-calendar-week"></i>近七天
                    </button>
                </div>
                ${digestResult ? `
                <div class="summaries-digest-badge">
                    <i class="fa-solid fa-clipboard-list"></i>
                    <span>${WINDOW_LABELS[r.window] || r.window || '摘要'}汇总</span>
                    <span class="digest-badge-count">${digestCount}</span>
                </div>` : ''}
            </div>
            ${analyticsHtml}
            ${digestHtml}
            <div class="summary-list-panel">
                <div class="panel-header">
                    <h3><i class="fa-solid fa-list-ul"></i>各会话摘要</h3>
                    <span class="panel-tag">${WINDOW_LABELS[r.window] || r.window || '摘要'} · ${r.count || 0} 条</span>
                </div>
                <div class="panel-body">${renderSummaryList(r.items)}</div>
            </div>
            <div class="summaries-foot">
                <i class="fa-regular fa-clock"></i>
                最后更新：${formatTsLocal(r.generated_at || new Date().toISOString(), false)}
                ${r.platform ? `<span>· 平台 ${escapeHtml(r.platform)}</span>` : ''}
            </div>
        `;
    } catch (e) {
        if (body.innerHTML.indexOf('summaries-error') === -1) {
            body.innerHTML = `<div class="summaries-error">
                <i class="fa-solid fa-triangle-exclamation"></i>
                摘要刷新失败，将自动重试…
            </div>`;
        }
    }
}

window.loadSummariesPage = loadSummariesPage;
window.startSummariesPolling = startSummariesPolling;
window.stopSummariesPolling = stopSummariesPolling;
window.setWindow = setWindow;
