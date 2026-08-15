// ============ pages/dashboard.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ Dashboard ============

// 渲染 hero 区 7 日 sparkline + 今日变化。
// 复用 message-stats API 的 trend 数组（已在下方 loadDashboardData 中请求），
// 不再额外发请求。trend: [{day:'YYYY-MM-DD', cnt:number}...]
function renderHeroSparkline(trend) {
    const wrap = document.getElementById('stat-messages-spark');
    const lineEl = wrap?.querySelector('.ov-spark-line');
    const areaEl = wrap?.querySelector('.ov-spark-area');
    const dotsEl = wrap?.querySelector('.ov-spark-dots');
    const axisEl = document.getElementById('stat-messages-spark-axis');
    const deltaEl = document.getElementById('stat-messages-today');
    if (!wrap || !lineEl || !areaEl) return;

    // 兼容 day / date 两种字段名
    const data = (trend || []).map(d => ({
        day: d.day || d.date || '',
        cnt: d.cnt || d.count || 0,
    }));
    if (data.length === 0) {
        if (axisEl) axisEl.textContent = '近 7 日 · 暂无数据';
        return;
    }
    // 只画最近 7 个点
    const pts = data.slice(-7);
    const max = Math.max(...pts.map(p => p.cnt), 1);
    const min = Math.min(...pts.map(p => p.cnt), 0);
    const range = Math.max(max - min, 1);
    const W = 200, H = 48, PAD = 4;
    const stepX = (W - PAD * 2) / Math.max(pts.length - 1, 1);
    const coords = pts.map((p, i) => {
        const x = PAD + i * stepX;
        const y = H - PAD - ((p.cnt - min) / range) * (H - PAD * 2);
        return { x, y, cnt: p.cnt, day: p.day };
    });

    // 折线 path
    const lineD = coords.map((c, i) => `${i === 0 ? 'M' : 'L'} ${c.x.toFixed(1)} ${c.y.toFixed(1)}`).join(' ');
    // 折线下方面积（path 闭合到 H）
    const first = coords[0], last = coords[coords.length - 1];
    const areaD = `${lineD} L ${last.x.toFixed(1)} ${H} L ${first.x.toFixed(1)} ${H} Z`;
    lineEl.setAttribute('d', lineD);
    areaEl.setAttribute('d', areaD);

    // 末端高亮 + 起点小点
    if (dotsEl) {
        const lastPt = coords[coords.length - 1];
        const firstPt = coords[0];
        dotsEl.innerHTML = `
            <circle cx="${firstPt.x.toFixed(1)}" cy="${firstPt.y.toFixed(1)}" r="1.8" fill="currentColor" opacity="0.45"/>
            <circle cx="${lastPt.x.toFixed(1)}" cy="${lastPt.y.toFixed(1)}" r="2.6" fill="currentColor" opacity="0.95"/>
            <circle cx="${lastPt.x.toFixed(1)}" cy="${lastPt.y.toFixed(1)}" r="5" fill="currentColor" opacity="0.18"/>
        `;
    }

    // 轴：起点日 - 终点日 + 峰值
    if (axisEl) {
        const peak = pts.reduce((m, p) => p.cnt > m.cnt ? p : m, pts[0]);
        axisEl.textContent = `${pts[0].day.slice(5)} → ${pts[pts.length - 1].day.slice(5)} · 峰 ${peak.cnt.toLocaleString()}`;
    }

    // 今日 delta：最后一日 vs 倒数第二日
    if (deltaEl && pts.length >= 2) {
        const today = pts[pts.length - 1].cnt;
        const prev = pts[pts.length - 2].cnt;
        const diff = today - prev;
        let cls = 'is-flat', icon = 'fa-minus', text = `今日 ${today.toLocaleString()}`;
        if (diff > 0) { cls = 'is-up'; icon = 'fa-arrow-up'; text = `今日 +${diff.toLocaleString()}（${today.toLocaleString()}）`; }
        else if (diff < 0) { cls = 'is-down'; icon = 'fa-arrow-down'; text = `今日 ${diff.toLocaleString()}（${today.toLocaleString()}）`; }
        else { text = `今日 ${today.toLocaleString()}（与昨日持平）`; }
        deltaEl.className = `ov-hero-delta ${cls}`;
        deltaEl.innerHTML = `<i class="fa-solid ${icon}"></i>${text}`;
    } else if (deltaEl && pts.length === 1) {
        deltaEl.className = 'ov-hero-delta is-flat';
        deltaEl.innerHTML = `<i class="fa-solid fa-minus"></i>今日 ${pts[0].cnt.toLocaleString()}`;
    }
    wrap.classList.remove('is-loading');
}

async function renderMessageTrendChart(trend) {
    const ctx = document.getElementById('chart-message-trend');
    if (!ctx) return;
    await window.loadChart();
    const ct = chartTheme();
    const skeleton = document.getElementById('chart-message-trend-skeleton');
    if (_messageTrendChart) {
        _messageTrendChart.destroy();
    }
    const labels = trend.map(d => d.day?.slice(5) || '');
    const data = trend.map(d => d.cnt || 0);
    _messageTrendChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: '消息数',
                data,
                borderColor: '#2563eb',
                backgroundColor: 'rgba(37, 99, 235, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 3,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, ticks: { stepSize: 1, color: ct.tick }, grid: { color: ct.grid } },
                x: { grid: { display: false, color: ct.grid }, ticks: { color: ct.tick } }
            },
            animation: { duration: 800, easing: 'easeOutQuart' }
        }
    });
    if (skeleton) skeleton.style.display = 'none';
    ctx.style.display = 'block';
}

// 消息类型 → 渐变配色（base 深 / light 浅，构成纵向渐变）
const MSG_TYPE_PALETTE = {
    '私信':   { base: '#2563eb', light: '#60a5fa' },
    '群消息': { base: '#16a34a', light: '#4ade80' },
    '系统通知': { base: '#f59e0b', light: '#fbbf24' },
    'AI摘要': { base: '#8b5cf6', light: '#c4b5fd' },
};
const MSG_TYPE_FB = [
    { base: '#06b6d4', light: '#22d3ee' },
    { base: '#ec4899', light: '#f9a8d4' },
    { base: '#dc2626', light: '#f87171' },
    { base: '#0891b2', light: '#67e8f9' },
];
function _msgTypeColor(label, idx) {
    return MSG_TYPE_PALETTE[label] || MSG_TYPE_FB[idx % MSG_TYPE_FB.length];
}

// 消息类型分布：纯 CSS 横向堆叠占比条 + 紧凑图例（替代旧 doughnut，压低卡片高度）
function renderMsgTypeChart(msgTypes) {
    const wrap = document.getElementById('msgtype-chart-wrap');
    const skeleton = document.getElementById('chart-msg-types-skeleton');
    if (!wrap) return;

    const items = [...msgTypes].sort((a, b) => (b.cnt || 0) - (a.cnt || 0));
    const total = items.reduce((s, d) => s + (d.cnt || 0), 0) || 1;

    // 头部总数提示
    const hint = document.getElementById('msgtype-total-hint');
    if (hint) hint.textContent = `共 ${total.toLocaleString('zh-CN')} 条`;

    // 顶部堆叠占比条（分段渐变 + 圆角胶囊）
    const segs = items.map((d, i) => {
        const pal = _msgTypeColor(d.msg_type, i);
        const w = (d.cnt || 0) / total * 100;
        const grad = `linear-gradient(135deg, ${pal.light}, ${pal.base})`;
        return `<span class="mt-seg" data-idx="${i}" style="width:${w}%;background:${grad}"></span>`;
    }).join('');

    // 紧凑图例（圆角色块 + 名称 + 占比条 + 计数）
    const rows = items.map((d, i) => {
        const pal = _msgTypeColor(d.msg_type, i);
        const w = (d.cnt || 0) / total * 100;
        const grad = `linear-gradient(135deg, ${pal.light}, ${pal.base})`;
        return `<div class="mt-row" data-idx="${i}" style="animation-delay:${(i * 0.06).toFixed(2)}s">
            <span class="mt-dot" style="background:${grad}"></span>
            <span class="mt-name">${escapeHtml(d.msg_type)}</span>
            <span class="mt-pct">${w.toFixed(1)}%</span>
            <div class="mt-bar"><i style="width:${w}%;background:${grad}"></i></div>
            <span class="mt-cnt">${(d.cnt || 0).toLocaleString('zh-CN')}</span>
        </div>`;
    }).join('');

    wrap.innerHTML = `<div class="mt-stack">${segs}</div><div class="mt-legend">${rows}</div>`;

    // hover 联动：图例行 ↔ 堆叠段高亮
    wrap.querySelectorAll('.mt-row').forEach(row => {
        const idx = row.getAttribute('data-idx');
        row.addEventListener('mouseenter', () => {
            wrap.querySelectorAll('.mt-seg').forEach(s =>
                s.classList.toggle('dim', s.getAttribute('data-idx') !== idx));
            const seg = wrap.querySelector(`.mt-seg[data-idx="${idx}"]`);
            if (seg) seg.classList.add('active');
        });
        row.addEventListener('mouseleave', () => {
            wrap.querySelectorAll('.mt-seg').forEach(s => s.classList.remove('dim', 'active'));
        });
    });

    if (skeleton) skeleton.style.display = 'none';
    wrap.style.display = 'flex';
}

// ============ 高频关键词 — 星环设计 (Cosmic Halo) ============
function renderWordCloud(words) {
    const container = document.getElementById('word-cloud-container');
    const skeleton = document.getElementById('word-cloud-skeleton');
    if (!container) {
        // 容器缺失属于页面状态问题，非错误，静默清理骨架
        if (skeleton) skeleton.style.display = 'none';
        return;
    }

    if (!words || words.length === 0) {
        container.className = 'word-cloud';
        container.innerHTML = '<div class="word-cloud-empty"><p>暂无足够数据</p></div>';
        if (skeleton) skeleton.style.display = 'none';
        container.style.display = 'block';
        return;
    }
    if (skeleton) skeleton.style.display = 'none';
    container.style.display = 'block';
    container.className = 'word-cloud';

    const topWords = words.slice(0, 18);
    const haloPalette = [
        { color: '#6366f1', light: '#a5b4fc', base: '#4f46e5' },   // indigo
        { color: '#8b5cf6', light: '#c4b5fd', base: '#6d28d9' },   // violet
        { color: '#3b82f6', light: '#93c5fd', base: '#1d4ed8' },   // blue
        { color: '#06b6d4', light: '#67e8f9', base: '#0e7490' },   // cyan
        { color: '#f59e0b', light: '#fcd34d', base: '#b45309' },   // amber
        { color: '#ef4444', light: '#fca5a5', base: '#b91c1c' },   // red
        { color: '#ec4899', light: '#f9a8d4', base: '#be185d' },   // pink
        { color: '#22c55e', light: '#86efac', base: '#15803d' },   // green
    ];
    const getSize = (i) => i < 3 ? 'lg' : i < 8 ? 'md' : 'sm';

    let html = '<div class="kw-halo">';
    html += '<div class="halo-core"></div>';
    html += '<div class="halo-field">';

    topWords.forEach((w, i) => {
        const { color, light, base } = haloPalette[i % haloPalette.length];
        const size = getSize(i);
        const enterDelay = (i * 0.05).toFixed(2);
        const floatDelay = (Math.random() * 3).toFixed(2);

        html += `<span class="halo-tag size-${size}"
            style="--hl-color:${color};--hl-light:${light};--hl-base:${base};
            --enter-delay:${enterDelay}s;--float-delay:${floatDelay}s;"
            title="${escapeHtml(w.word)} · ${w.count} 次">
            ${escapeHtml(w.word)}<span class="tag-count">${w.count}</span></span>`;
    });

    html += '</div></div>';
    container.innerHTML = html;

    // 点击标签 → 跳转搜索
    container.querySelectorAll('.halo-tag').forEach(tag => {
        tag.addEventListener('click', () => {
            const word = tag.childNodes[0].textContent.trim();
            searchByKeyword(word);
        });
    });
}

function searchByKeyword(keyword) {
    // 点击标签后跳转到消息页并搜索
    switchPage('messages');
    const searchInput = document.getElementById('msg-search');
    if (searchInput) {
        searchInput.value = keyword;
        searchInput.dispatchEvent(new Event('input'));
    }
}

function renderTopSenders(senders) {
    const container = document.getElementById('top-senders-list');
    if (!container) return;
    if (!senders || senders.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding: 24px;"><p>暂无数据</p></div>';
        return;
    }
    const topSenders = senders.slice(0, 5);
    container.innerHTML = topSenders.map((s, i) => `
        <div class="kw-top-item">
            <span class="kw-top-rank ${i === 0 ? 'top1' : i === 1 ? 'top2' : i === 2 ? 'top3' : ''}">${i + 1}</span>
            <span class="kw-top-pattern" title="${escapeHtml(s.sender_name || '未知')}">${escapeHtml(s.sender_name || '未知')}</span>
            <span class="kw-top-count">${s.cnt ?? 0}</span>
        </div>
    `).join('');
}



// ============ Dashboard ============

function animateValue(el, value, duration = 600) {
    if (!el) return;
    const start = parseInt(el.getAttribute('data-start') || '0');
    const diff = value - start;
    if (diff === 0) {
        el.textContent = value;
        return;
    }
    const startTime = performance.now();
    function step(currentTime) {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        const easeProgress = 1 - Math.pow(1 - progress, 3);
        const current = Math.round(start + diff * easeProgress);
        el.textContent = current;
        if (progress < 1) {
            requestAnimationFrame(step);
        } else {
            el.textContent = value;
        }
    }
    requestAnimationFrame(step);
}

function showStatCard(cardId, delay = 0) {
    const card = document.getElementById(cardId);
    if (card) {
        card.style.opacity = '0';
        card.style.transform = 'translateY(10px)';
        setTimeout(() => {
            card.style.transition = 'all 0.4s ease';
            card.style.opacity = '1';
            card.style.transform = 'translateY(0)';
        }, delay);
    }
}


// ============ 仪表盘骨架屏 ============
// 仅在「首次加载 / 手动切回仪表盘」时注入骨架，30s 后台静默刷新不闪骨架
function skKwRows(n = 5) {
    let h = '<div class="skeleton-card" style="padding:0.75rem">';
    for (let i = 0; i < n; i++) {
        h += '<div class="skeleton-kw-row"><div class="skeleton skeleton-rank"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line short"></div></div>';
    }
    return h + '</div>';
}

function skToolRanking(n = 20) {
    let h = '';
    for (let i = 0; i < n; i++) {
        h += `
            <div class="ts-rank-card" aria-label="loading">
                <div class="ts-rank-card-header">
                    <span class="ts-rank-card-badge"></span>
                    <span class="ts-rank-card-name"><span class="skeleton skeleton-line" style="width:70%;height:12px;display:inline-block"></span></span>
                </div>
                <div class="ts-rank-card-footer">
                    <span class="ts-rank-card-calls"><span class="skeleton skeleton-line" style="width:24px;height:10px;display:inline-block"></span></span>
                    <span class="ts-rank-card-rate"><span class="skeleton skeleton-line" style="width:18px;height:10px;display:inline-block"></span></span>
                </div>
            </div>`;
    }
    return h;
}

function fmtDashTime(ts) {
    if (!ts) return '-';
    // 兼容后端 "2026-07-12T04:30:00+00:00" / "2026-07-12T04:30:00Z" / 纯 ISO
    const d = new Date(ts);
    if (isNaN(d.getTime())) return String(ts).slice(0, 16);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function skLogRows(n = 6) {
    let h = '<div class="skeleton-log-list">';
    for (let i = 0; i < n; i++) {
        h += '<div class="skeleton-log-row"><div class="skeleton skeleton-line short"></div><div class="skeleton skeleton-line short"></div><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line short"></div></div>';
    }
    return h + '</div>';
}

function skStatusTiles(n = 7) {
    // 胶囊骨架：宽度错落，避免机械感
    const widths = [78, 110, 124, 70, 86, 96, 104];
    let h = '';
    for (let i = 0; i < n; i++) {
        h += `<div class="ov-chip is-sk">
            <div class="skeleton" style="width:21px;height:21px;border-radius:50%;flex-shrink:0"></div>
            <div class="skeleton skeleton-line" style="height:10px;width:${widths[i % widths.length]}px"></div>
        </div>`;
    }
    return h;
}

function injectDashboardSkeletons() {
    const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
    // 顶部 5 个小卡片
    ['stat-messages', 'stat-keywords', 'stat-kb-docs', 'stat-ddocs', 'stat-memories'].forEach(id => {
        set(id, '<span class="stat-skeleton skeleton skeleton-line tall"></span>');
    });
    // 决策追踪 TOP10 / 活跃发送者 TOP5
    set('decisions-top-list', skKwRows(10));
    set('top-senders-list', skKwRows(5));
    // 系统状态网格
    set('status-list', skStatusTiles(6));
    // 最近消息
    set('recent-messages-stream', skLogRows(6));
    // 工具调用统计：排行列表（3列网格）+ 4 个汇总值
    set('tool-stats-container', skToolRanking(20));
    // 调度可靠性（背压 + 防抖 inline-bar 值）
    const relSk = '<span class="rel-skeleton skeleton"></span>';
    ['bp-max-dispatch','bp-max-concurrent','bp-last-dispatched','bp-last-deferred',
     'db-pending','db-delay-count','db-extra-sec','db-fired-with',
     'ps-last-poll','ps-poll-count','ps-queue-depth','ps-last-error','ps-running'].forEach(id => set(id, relSk));
    // 「工具」chip 的 value 骨架（自检回包前会闪一下）
    const toolsChipVal = document.querySelector('#tools-tile .ov-chip-value');
    if (toolsChipVal) toolsChipVal.innerHTML = '<span class="rel-skeleton skeleton" style="width:38px;height:11px;display:inline-block;vertical-align:middle;"></span>';
    // 高频关键词 / 图表：重新显出骨架，渲染后由各自 render 隐藏
    const wcSk = document.getElementById('word-cloud-skeleton');
    if (wcSk) wcSk.style.display = 'block';
    const wcC = document.getElementById('word-cloud-container');
    if (wcC) wcC.style.display = 'none';
    const tSk = document.getElementById('chart-message-trend-skeleton');
    if (tSk) tSk.style.display = 'flex';
    const tC = document.getElementById('chart-message-trend');
    if (tC) tC.style.display = 'none';
    const mtSk = document.getElementById('chart-msg-types-skeleton');
    if (mtSk) mtSk.style.display = 'flex';
    const mtC = document.getElementById('chart-msg-types');
    if (mtC) mtC.style.display = 'none';
}

async function loadDashboardData(showSkeleton = true, retryCount = 0) {
    if (showSkeleton) injectDashboardSkeletons();
    const t0 = performance.now();
    try {
        const data = await api.getStatus();
        if (!data) {
            if (retryCount < 3) {
                setTimeout(() => loadDashboardData(false, retryCount + 1), 1500);
            } else {
                const container = document.querySelector('.content-area');
                if (container) {
                    container.innerHTML = `
                        <div class="empty-state" style="margin-top:60px;">
                            <div class="empty-icon" style="font-size:4rem;">⚠️</div>
                            <p style="font-size:1.1rem;margin-top:1rem;">数据加载失败</p>
                            <p class="text-sm text-gray-500">请检查服务是否正常运行，或点击下方按钮重试</p>
                            <button class="btn btn-primary" onclick="loadDashboard()" style="margin-top:1rem;">
                                <i class="fa-solid fa-arrows-rotate"></i> 重新加载
                            </button>
                        </div>
                    `;
                }
            }
            return;
        }
        if (data.error === 'unauthorized') {
            return;
        }
        const stats = data.stats || {};
        // 本地接口过快时骨架会一闪而过（getStatus 常 <50ms），
        // 强制骨架至少可见 MIN_SKELETON_MS，保证加载态可被感知。
        if (showSkeleton) {
            const elapsed = performance.now() - t0;
            const MIN_SKELETON_MS = 400;
            if (elapsed < MIN_SKELETON_MS) {
                await new Promise(r => setTimeout(r, MIN_SKELETON_MS - elapsed));
            }
        }

        // Update stat cards with animation
        // 钉钉文档统计（stat-ddocs）仅在钉钉平台显示
        const currentPlatform = window.store?.getPlatform ? window.store.getPlatform() : 'dingtalk';
        const statItems = [
            { id: 'stat-messages', value: stats.messages ?? 0, card: 'stat-card-messages' },
            { id: 'stat-keywords', value: stats.keyword_rules ?? 0, card: 'stat-card-keywords' },
            { id: 'stat-kb-docs', value: stats.kb_documents ?? 0, card: 'stat-card-kb-docs' },
            { id: 'stat-ddocs', value: stats.dingtalk_docs ?? 0, card: 'stat-card-ddocs', platform: 'dingtalk' },
            { id: 'stat-memories', value: stats.memories ?? 0, card: 'stat-card-memories' },
        ];

        statItems.forEach((item, index) => {
            const el = document.getElementById(item.id);
            const cardEl = document.getElementById(item.card);
            // 平台专属卡片：仅在对应平台显示
            if (item.platform && item.platform !== currentPlatform) {
                if (cardEl) cardEl.style.display = 'none';
                return;
            }
            if (cardEl) cardEl.style.display = '';
            if (el) {
                el.innerHTML = '';
                el.setAttribute('data-start', '0');
                animateValue(el, item.value, 800);
                showStatCard(item.card, index * 80);
            }
        });

        // Update system status as premium grid
        const cfg = data.config || {};
        const circuit = data.circuit || {};
        const statusList = document.getElementById('status-list');
        if (statusList) {
            const trippedCount = circuit.tripped_count || 0;
            const llmModel = cfg.llm_model || '-';
            const embValue = cfg.embedding_enabled ? (cfg.embedding_model || '已启用') : '未启用';
            const toolsCount = cfg.tools_count ?? null;
            // 数据驱动的胶囊流：每个胶囊带 hint 用大白话解释含义，
            // mouseover 看 tooltip 即可，不需要点进去。重复含义项合并去重。
            const dryRunHint = cfg.dry_run
                ? 'Dry Run：收消息但不调用 LLM、不会回复/写操作，用于观察链路'
                : '正常模式：消息会正常进入处理链路';
            const embHint = cfg.embedding_enabled
                ? `语义检索已启用，模型 ${cfg.embedding_model || '已加载'}`
                : '语义检索未启用，只能做关键词检索';
            const rerankValue = cfg.rerank_enabled ? (cfg.rerank_model || '已启用') : '未启用';
            const rerankHint = cfg.rerank_enabled
                ? `重排模型已启用：${cfg.rerank_model || '已加载'}，RAG 检索结果会经交叉编码器二次排序`
                : '重排模型未启用，RAG 仅使用向量+关键词混合排序';
            const tripHint = trippedCount > 0
                ? `熔断器跳闸：${trippedCount} 个工具被临时禁用（失败次数过多），冷却后自动恢复`
                : '熔断器无跳闸，全部工具可用';
            const pollHint = cfg.poll_interval != null
                ? `${cfg.poll_interval} 秒轮询一次新消息，越短越实时但越耗资源`
                : '轮询间隔未配置';
            const llmHint = `当前对话生成模型：${llmModel}`;
            const toolsHint = (toolsCount != null)
                ? `内置 ${toolsCount} 个技能/工具，对话时可被模型调用（读文档、发审批、查日程…）`
                : '工具数未上报';
            const chips = [
                { ic: 'fa-circle-play', label: '运行模式', value: cfg.dry_run ? 'Dry Run' : '正常', tone: cfg.dry_run ? 'warn' : 'ok', hint: dryRunHint },
                { ic: 'fa-microchip', label: 'LLM', value: llmModel, title: llmModel, hint: llmHint },
                { ic: 'fa-cubes', label: 'Embedding', value: embValue, title: embValue, tone: cfg.embedding_enabled ? '' : 'warn', hint: embHint },
                { ic: 'fa-sort-amount-down', label: '重排', value: rerankValue, title: rerankValue, tone: cfg.rerank_enabled ? '' : 'muted', hint: rerankHint },
                { ic: 'fa-arrows-rotate', label: '轮询', value: cfg.poll_interval != null ? cfg.poll_interval + 's' : '-', hint: pollHint },
                { ic: 'fa-shield-halved', label: '熔断', value: trippedCount > 0 ? trippedCount + ' 个' : '正常', tone: trippedCount > 0 ? 'warn' : 'ok', hint: tripHint },
                // 「工具」与「配置自检」合并：注册数与白名单一致性是同一件事的两面，
                // 分开显示让用户看到一个 38 出现两次的混乱。统一一个胶囊表达，drift 状态用 tone 区分。
                { ic: 'fa-toolbox', label: '工具', value: (toolsCount != null) ? toolsCount + ' 个' : '-', hint: toolsHint, dataId: 'tools-tile' },
            ];
            statusList.innerHTML = chips.map(c => `
                <div class="ov-chip${c.tone ? ' is-' + c.tone : ''}"${c.dataId ? ` id="${c.dataId}"` : ''}${c.hint ? ` title="${escapeHtml(c.hint)}"` : ''}>
                    <span class="ov-chip-ico"><i class="fa-solid ${c.ic}"></i></span>
                    <span class="ov-chip-label">${c.label}</span>
                    <span class="ov-chip-value${c.tone ? ' ' + c.tone : ''}"${c.title ? ` title="${escapeHtml(String(c.title))}"` : ''}>${escapeHtml(String(c.value))}</span>
                </div>`).join('');
        }

        // Update user name
        const userName = (data.user || {}).name || 'N/A';
        setText('user-name', userName);

        // Update sidebar status
        const statusIcon = document.getElementById('sidebar-status-icon');
        const statusText = document.getElementById('sidebar-status-text');
        if (statusIcon && statusText) {
            statusIcon.className = 'sidebar-status-icon ok';
            statusText.className = 'sidebar-status-text ok';
            statusText.textContent = '运行正常';
        }
    } catch (e) {
        console.error('Failed to load dashboard status:', e);
    }

    // 并行发起无依赖的子请求（原先被人为 setTimeout 串行化）
    await Promise.all([
        (async () => {
            try {
                const statsData = await api.getMessageStats(7);
                if (currentPage !== 'dashboard') return;
                renderMessageTrendChart(statsData.trend || []);
                renderHeroSparkline(statsData.trend || []);
                renderMsgTypeChart(statsData.msg_types || []);
                renderTopSenders(statsData.top_senders || []);
                renderWordCloud(statsData.top_words || []);
                // D1: 用 message-stats 趋势累加值覆盖统计卡消息数，保证与趋势图数据口径一致
                const trendTotal = (statsData.trend || []).reduce((s, d) => s + (d.cnt || 0), 0);
                const statMsgEl = document.getElementById('stat-messages');
                if (statMsgEl && trendTotal > 0) {
                    statMsgEl.textContent = trendTotal.toLocaleString();
                }
            } catch (e) {
                console.error('Failed to load message stats:', e);
                // 清理骨架 + 显示占位，避免骨架永久卡住
                ['word-cloud-skeleton', 'chart-message-trend-skeleton', 'chart-msg-types-skeleton'].forEach(id => {
                    const el = document.getElementById(id);
                    if (el) el.style.display = 'none';
                });
                const wcC = document.getElementById('word-cloud-container');
                if (wcC) { wcC.style.display = 'block'; wcC.innerHTML = '<div class="word-cloud-empty"><p>数据加载失败</p></div>'; }
                const trC = document.getElementById('chart-message-trend');
                if (trC) trC.style.display = 'none';
                const mtC = document.getElementById('msgtype-chart-wrap');
                if (mtC) mtC.style.display = 'none';
            }
        })(),
        loadRecentMessages(),
        (async () => {
            try {
                const decData = await api.fetch('/api/decisions?n=2');
                if (currentPage !== 'dashboard') return;
                // 取最新的 2 条并倒序（最新在上）：API 返回时间正序，故 slice(-2).reverse()
                const decisions = (decData && decData.decisions || []).slice(-2).reverse();
                const decContainer = document.getElementById('decisions-top-list');
                if (!decContainer) return;
                if (decisions.length === 0) {
                    decContainer.innerHTML = `<div class="empty-state"><div class="empty-icon">${iconize("📊")}</div><p>暂无决策记录</p></div>`;
                    return;
                }
                renderDecisionFeed('decisions-top-list', decisions, { max: 2, emptyText: '暂无决策记录' });
            } catch (e) {
                console.error('Failed to load decisions top:', e);
            }
        })(),
        (async () => {
            try {
                const driftData = await api.fetch('/api/config-drift');
                if (currentPage !== 'dashboard') return;
                // 「工具」chip 既是数量展示也是自检状态：根据 /api/config-drift
                // 结果调整 tone，并更新 title 把漂移细节写进 tooltip。
                const el = document.getElementById('drift-status');  // 兼容旧引用
                const valueEl = document.querySelector('#tools-tile .ov-chip-value');
                const chip = document.getElementById('tools-tile');
                const setTone = (tone) => {
                    if (valueEl) valueEl.className = 'ov-chip-value' + (tone ? ' ' + tone : '');
                    if (chip) chip.className = 'ov-chip' + (tone ? ' is-' + tone : '');
                    if (el) el.className = 'ov-chip-value' + (tone ? ' ' + tone : '');
                };
                if (!driftData || driftData.available === false
                    || !Array.isArray(driftData.missing_in_whitelist)
                    || !Array.isArray(driftData.stale_in_whitelist)) {
                    // 自检不可用，不动 tone（保留 default）
                    if (valueEl) valueEl.title = '配置自检暂不可用';
                } else if (driftData.missing_in_whitelist.length || driftData.stale_in_whitelist.length) {
                    setTone('warn');
                    const detail = '缺少 ' + driftData.missing_in_whitelist.length + ' / 多余 ' + driftData.stale_in_whitelist.length
                        + '\n缺：' + driftData.missing_in_whitelist.join(', ')
                        + (driftData.stale_in_whitelist.length ? '\n多余：' + driftData.stale_in_whitelist.join(', ') : '');
                    if (valueEl) valueEl.title = detail;
                } else {
                    setTone('ok');
                    if (valueEl) valueEl.title = `${driftData.registered_count} 个工具全部就位，没有漂移`;
                }
            } catch (e) {
                console.error('Drift check failed:', e);
            }
        })(),
        (typeof loadDecisions === 'function' ? loadDecisions().catch(() => {}) : Promise.resolve()),
    ]);
    // 立即拉取一次实时日志以清除骨架屏（后续由定时器持续轮询）
    pollRealtimeLogs();
    // 向量模型加载状态（含下载进度）
    loadEmbeddingStatus();
    // 路由质量 KPI 概览（订阅 store 切片，跨页共享，不重复请求）
    loadRoutingQualityOverview();
}


function loadDashboard() {
    if (currentPage !== 'dashboard') {
        switchPage('dashboard');
    }
    loadDashboardData(true);
}

// ============ 路由质量 KPI 概览 ============
// 通过 store 订阅 routingQuality.aggregate 切片，与 routetrace 页共享数据，
// 无需重复请求 /api/routing-quality/aggregate。
// 若切片为空，则触发 RoutingQualityService.loadAggregate() 懒加载。
let _rqOverviewUnsub = null;
function loadRoutingQualityOverview() {
    // 确保聚合卡片容器存在（动态注入 stats-grid）
    const grid = document.getElementById('stats-grid');
    if (!grid) return;

    let cardEl = document.getElementById('stat-card-rq');
    if (!cardEl) {
        cardEl = document.createElement('div');
        cardEl.className = 'stat-card';
        cardEl.id = 'stat-card-rq';
        cardEl.innerHTML = `
            <div class="stat-icon"><i class="fa-solid fa-route"></i></div>
            <div class="stat-info">
                <div class="stat-value" id="stat-rq-overview"><span class="stat-skeleton skeleton skeleton-line tall"></span></div>
                <div class="stat-label">路由质量</div>
            </div>`;
        grid.appendChild(cardEl);
    }

    function renderOverview(agg) {
        const el = document.getElementById('stat-rq-overview');
        if (!el || currentPage !== 'dashboard') return;
        if (!agg || agg.available === false) {
            el.innerHTML = '<span style="color:var(--text-tertiary);font-size:0.9rem">暂无数据</span>';
            return;
        }
        const total = agg.total_records ?? 0;
        const health = total ? ((1 - (agg.empty_rate ?? 0)) * 100).toFixed(0) + '%' : '—';
        const avgMs = agg.avg_total_ms != null
            ? (agg.avg_total_ms >= 1000 ? (agg.avg_total_ms / 1000).toFixed(1) + 's' : Math.round(agg.avg_total_ms) + 'ms')
            : '—';
        el.innerHTML = total.toLocaleString('zh-CN');
        el.title = '记录数: ' + total + ' | 健康率: ' + health + ' | 平均延迟: ' + avgMs;
    }

    // 先尝试读已有切片
    const cached = window.store.slice('routingQuality', 'aggregate');
    if (cached && cached.available !== false) {
        renderOverview(cached);
    } else {
        const el = document.getElementById('stat-rq-overview');
        if (el) el.innerHTML = '<span class="stat-skeleton skeleton skeleton-line tall"></span>';
    }

    // 订阅切片变更
    if (!_rqOverviewUnsub) {
        _rqOverviewUnsub = window.store.subscribeSlice('routingQuality', 'aggregate', function (agg) {
            renderOverview(agg);
        });
    }

    // 切片为空时懒加载（仅触发一次）
    if (!cached || cached.available === false) {
        if (typeof RoutingQualityService !== 'undefined') {
            RoutingQualityService.loadAggregate().catch(function () {});
        }
    }
}

// var（非 let）确保跨脚本共享变量被提升为 window 属性，避免脚本加载顺序变化时的 TDZ ReferenceError
var lastMessageId = null;
var embStatusPolling = null;

async function loadRecentMessages() {
    const stream = document.getElementById('recent-messages-stream');
    if (!stream || currentPage !== 'dashboard') return;
    try {
        const data = await api.getMessages('', 5);
        const messages = data.messages || [];
        if (messages.length === 0) {
            stream.innerHTML = '<div class="log-item" style="justify-content:center;color:var(--text-tertiary)">暂无消息</div>';
            return;
        }

        lastMessageId = messages[0].id;
        stream.innerHTML = messages.map(m => renderLogItem(m)).join('');
    } catch (e) {
        stream.innerHTML = `<div class="log-item" style="justify-content:center;color:var(--brand-danger)">加载失败: ${escapeHtml(e.message)}</div>`;
    }
}

function renderLogItem(m) {
    const content = m.content || '';
    const contentPreview = content.length > 60 ? content.slice(0, 60) + '...' : content;
    const isBot = !!(m.is_bot);
    const isSelf = m.role === 'assistant';
    let aitag = '';
    if (isBot) aitag = '<i class="fa-solid fa-robot" style="color:var(--brand-primary);margin-right:2px"></i> ';
    else if (isSelf) aitag = '<i class="fa-solid fa-user" style="margin-right:2px"></i> ';
    return `
        <div class="log-item">
            <span class="log-time">${escapeHtml(fmtDashTime(m.timestamp))}</span>
            <span class="log-sender" title="${escapeHtml(m.sender_name || '-')}">${aitag}${escapeHtml(m.sender_name || '-')}</span>
            <span class="log-receiver" title="${escapeHtml(m.receiver_name || m.chat_name || '-')}">${escapeHtml(m.receiver_name || m.chat_name || '-')}</span>
            <span class="log-item-text" data-full="${escapeHtml(content)}">${escapeHtml(contentPreview)}</span>
            <span class="log-type">${escapeHtml(m.msg_type || 'text')}</span>
        </div>
    `;
}

// ============ F-H6：单通道实时轮询（合并消息/决策/日志三路为 1 个 setInterval） ============
// 原 recentMessages(5s) / decisions(5s) / realtimeLogs(2s) 三路独立轮询 ≈ 54 req/min，
// 现合并为单个 /api/dashboard/stream-data（带增量游标），5s 一次 ≈ 12 req/min。
let dashboardLivePolling = null;
async function fetchDashboardStream() {
    if (currentPage !== 'dashboard') return;
    try {
        const levelSel = document.getElementById('log-level-select');
        const logLevel = levelSel ? levelSel.value : 'info';
        const platform = (typeof window.store !== 'undefined' && window.store && window.store.getPlatform)
            ? window.store.getPlatform() : '';
        const params = new URLSearchParams({
            last_message_id: String(lastMessageId || 0),
            last_log_id: String(lastLogId || 0),
            log_level: logLevel,
            decisions_n: '2',
            decisions_platform: platform,
            platform: 'all',
        });
        const data = await api.fetch('/api/dashboard/stream-data?' + params.toString());
        // 日志：复用 applyRealtimeLogs
        if (data && data.logs) applyRealtimeLogs(data.logs);
        // 决策：直接渲染到 dashboard feed（n=2 紧凑，与 loadDashboardData 初始渲染一致）
        // 注意：HTML 模板中元素 ID 是 decisions-top-list，不是 decisions-feed
        const decContainer = document.getElementById('decisions-top-list');
        if (data && data.decisions && decContainer) {
            renderDecisionFeed('decisions-top-list', data.decisions.decisions || [], { max: 2, emptyText: '暂无决策记录，发一条消息试试' });
        }
        // 消息：服务端已按游标过滤，直接渲染增量
        if (data && data.messages && data.messages.length) {
            lastMessageId = data.max_message_id || lastMessageId;
            applyNewMessages(data.messages);
        }
    } catch (e) {
        // 静默失败：实时面板不应干扰其它功能
    }
}
function startDashboardLivePolling() {
    if (dashboardLivePolling) clearInterval(dashboardLivePolling);
    dashboardLivePolling = setInterval(fetchDashboardStream, 5000);
}
function stopDashboardLivePolling() {
    if (dashboardLivePolling) {
        clearInterval(dashboardLivePolling);
        dashboardLivePolling = null;
    }
}
window.startDashboardLivePolling = startDashboardLivePolling;
window.stopDashboardLivePolling = stopDashboardLivePolling;

// 将一批「新增消息」渲染进实时消息流（F-H6：抽出以便被单通道 stream 复用）
function applyNewMessages(newMessages) {
    const stream = document.getElementById('recent-messages-stream');
    if (!stream || !newMessages || newMessages.length === 0) return;
    const newItemsHtml = newMessages.map(m => renderLogItem(m)).join('');
    stream.insertAdjacentHTML('afterbegin', newItemsHtml);
    const newItems = stream.querySelectorAll('.log-item');
    newItems.forEach((item, index) => {
        if (index < newMessages.length) {
            item.classList.add('new-item');
            setTimeout(() => item.classList.remove('new-item'), 500);
        }
    });
    if (stream.scrollHeight > 220) {
        stream.scrollTop = 0;
    }
    while (stream.childElementCount > 150) {
        stream.removeChild(stream.lastElementChild);
    }
}


// ============ 实时日志面板（独立轮询，只刷日志容器，不刷新框架） ============
let lastLogId = 0;
let logAutoScroll = true;

function renderLogLine(l) {
    const raw = escapeHtml(String(l.level || 'INFO').toUpperCase());
    const cls = 'rt-level-' + raw.toLowerCase();
    const ts = escapeHtml((l.ts || '').slice(-8));        // 仅显示 HH:MM:SS
    const fullTs = escapeHtml(l.ts || '');
    let logger = escapeHtml(l.logger || '-').replace(/^src\./, '');  // 去掉 src. 前缀
    const msg = escapeHtml(l.message || '');
    return `
        <div class="rt-log-line ${cls}" title="${fullTs} · [${raw}] · ${logger}">
            <span class="rt-log-ts">${ts}</span>
            <span class="rt-log-level">${raw}</span>
            <span class="rt-log-logger">${logger}:</span>
            <span class="rt-log-msg">${msg}</span>
        </div>`;
}

async function pollRealtimeLogs() {
    const levelSel = document.getElementById('log-level-select');
    const level = levelSel ? levelSel.value : 'info';
    try {
        // 仪表盘实时面板保留全局概览：显式 platform=all，避免被当前平台上下文隔离
        const data = await api.fetch(`/api/logs?level=${encodeURIComponent(level)}&since=${lastLogId}&limit=300&platform=all`);
        applyRealtimeLogs(data);
    } catch (e) {
        // 静默失败：日志面板不应干扰其它功能
    }
}

// 将日志增量 payload 渲染进实时日志面板（F-H6：抽出以便被单通道 stream 复用）
function applyRealtimeLogs(data) {
    const stream = document.getElementById('realtime-log-stream');
    if (!stream) return;
    const logs = (data && data.logs) || [];
    // 缓冲区重置（后端重启 / wrap）检测：返回的最大 id 小于本地游标时，
    // 清空游标让下次拉全量，避免实时日志冻结在旧记录上。
    if (data && typeof data.max_id === 'number' && data.max_id > 0 && data.max_id < lastLogId) {
        lastLogId = 0;
    }
    // 清除初始连接骨架
    const skel = stream.querySelector('.rt-log-skeleton');
    if (skel) skel.remove();
    // 更新计数徽章（用缓冲区总量，非本次增量条数）
    const cnt = document.getElementById('log-count');
    if (cnt && data) {
        cnt.textContent = (data.buffer_total != null ? data.buffer_total : (data.total || 0)) + ' 条';
    }
    if (!logs.length) {
        // 增量无新日志：保留已有历史，仅在容器真正为空时才显示占位，
        // 避免空闲轮询把历史覆盖成“等待日志输出…”（原 bug）。
        if (stream.childElementCount === 0 && !stream.querySelector('.rt-log-empty')) {
            stream.innerHTML = '<div class="rt-log-empty"></div>';
        }
        return;
    }
    // 清除空状态占位
    const empty = stream.querySelector('.rt-log-empty');
    if (empty) stream.innerHTML = '';
    const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 48;
    const before = stream.childElementCount;
    stream.insertAdjacentHTML('beforeend', logs.map(renderLogLine).join(''));
    // 仅对新插入行施加入场动画
    const kids = stream.children;
    for (let i = before; i < kids.length; i++) {
        kids[i].classList.add('rt-new');
    }
    // 限制 DOM 行数（保留最近 150 条，足够看上下文）
    while (stream.childElementCount > 150) {
        stream.removeChild(stream.firstChild);
    }
    if (logAutoScroll && atBottom) {
        stream.scrollTop = stream.scrollHeight;
    }
    lastLogId = logs[logs.length - 1].id;
}


