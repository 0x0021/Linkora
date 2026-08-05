// ============ pages/dashboard.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ Dashboard ============

function renderMessageTrendChart(trend) {
    const ctx = document.getElementById('chart-message-trend');
    if (!ctx) return;
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
            <div class="ts-rank-item">
                <div class="ts-rank-num">${i + 1}</div>
                <div class="ts-rank-info">
                    <div class="skeleton skeleton-line" style="height:14px;width:70%"></div>
                    <div class="skeleton skeleton-line" style="height:8px;width:100%;margin-top:4px"></div>
                </div>
                <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">
                    <div class="skeleton skeleton-line" style="height:12px;width:30px"></div>
                    <div class="skeleton skeleton-line" style="height:10px;width:24px"></div>
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

function skStatusTiles(n = 6) {
    let h = '';
    for (let i = 0; i < n; i++) {
        h += `<div class="status-tile skeleton-tile">
            <div class="skeleton" style="width:30px;height:30px;border-radius:50%;flex-shrink:0"></div>
            <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:6px">
                <div class="skeleton skeleton-line" style="height:9px;width:55%"></div>
                <div class="skeleton skeleton-line" style="height:12px;width:75%"></div>
            </div>
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
     'ps-last-poll','ps-poll-count','ps-queue-depth','ps-last-error','ps-running',
     'drift-status'].forEach(id => set(id, relSk));
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
            const circuitBadge = trippedCount > 0
                ? `<div class="status-tile-value warn">${trippedCount} 个</div>`
                : `<div class="status-tile-value ok">正常</div>`;
            statusList.innerHTML = `
                <div class="status-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-circle-play"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">运行模式</div>
                        <div class="status-tile-value">${cfg.dry_run ? 'Dry Run' : '正常'}</div>
                    </div>
                </div>
                <div class="status-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-microchip"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">LLM 模型</div>
                        <div class="status-tile-value" title="${escapeHtml((cfg.llm_model || '-').slice(0, 100))}">${escapeHtml((cfg.llm_model || '-').length > 20 ? (cfg.llm_model || '-').slice(0,18)+'…' : (cfg.llm_model || '-'))}</div>
                    </div>
                </div>
                <div class="status-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-cubes"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">Embedding</div>
                        <div class="status-tile-value">${cfg.embedding_enabled ? (cfg.embedding_model || '已启用') : '未启用'}</div>
                    </div>
                </div>
                <div class="status-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-arrows-rotate"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">轮询间隔</div>
                        <div class="status-tile-value">${cfg.poll_interval != null ? cfg.poll_interval + 's' : '-'}</div>
                    </div>
                </div>
                <div class="status-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-toolbox"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">可用工具</div>
                        <div class="status-tile-value">${cfg.tools_count ?? '-'} 个</div>
                    </div>
                </div>
                <div class="status-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-shield-halved"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">熔断保护</div>
                        ${circuitBadge}
                    </div>
                </div>
                <div class="status-tile" id="drift-tile">
                    <div class="status-tile-icon"><i class="fa-solid fa-clipboard-check"></i></div>
                    <div class="status-tile-text">
                        <div class="status-tile-label">配置自检</div>
                        <div class="status-tile-value" id="drift-status"><span class="rel-skeleton skeleton" style="width:60px;display:inline-block;"></span></div>
                    </div>
                </div>
            `;
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
                const el = document.getElementById('drift-status');
                if (!el) return;
                if (!driftData || driftData.available === false) {
                    el.textContent = '—';
                    el.className = 'status-tile-value';
                } else if (driftData.missing_in_whitelist.length || driftData.stale_in_whitelist.length) {
                    el.textContent = '有漂移';
                    el.className = 'status-tile-value warn';
                    el.title = '缺少: ' + driftData.missing_in_whitelist.join(',')
                        + (driftData.stale_in_whitelist.length ? ' | 多余: ' + driftData.stale_in_whitelist.join(',') : '');
                } else {
                    el.textContent = '一致 (' + driftData.registered_count + ')';
                    el.className = 'status-tile-value ok';
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
var recentMessagesPolling = null;
var embStatusPolling = null;
var _pollSeq = 0;

async function loadRecentMessages() {
    const stream = document.getElementById('recent-messages-stream');
    if (!stream || currentPage !== 'dashboard') return;
    try {
        const data = await api.getMessages('', 10);
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

async function pollNewMessages() {
    const stream = document.getElementById('recent-messages-stream');
    if (!stream || !lastMessageId) return;
    const seq = ++_pollSeq;
    try {
        const data = await api.getMessages('', 20);
        if (seq !== _pollSeq) return; // 忽略过期请求，防止竞态覆盖
        const messages = data.messages || [];
        if (messages.length === 0 || messages[0].id === lastMessageId) return;

        const newMessages = messages.filter(m => m.id > lastMessageId);
        if (newMessages.length > 0) {
            lastMessageId = messages[0].id;
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
        }
    } catch (e) {
        console.error('Poll new messages failed:', e);
    }
}

function startRecentMessagesPolling() {
    if (recentMessagesPolling) clearInterval(recentMessagesPolling);
    recentMessagesPolling = setInterval(pollNewMessages, 5000);
}

function stopRecentMessagesPolling() {
    if (recentMessagesPolling) {
        clearInterval(recentMessagesPolling);
        recentMessagesPolling = null;
    }
}

// 最近决策追踪：5s 轻量轮询（卡片已从意图页移至仪表盘，复用全局 loadDecisions）
let _decisionPolling = null;
function startDecisionPolling() {
    if (_decisionPolling) clearInterval(_decisionPolling);
    _decisionPolling = setInterval(() => {
        if (currentPage !== 'dashboard') return;
        if (typeof loadDecisions === 'function') { loadDecisions().catch(() => {}); }
    }, 5000);
}
function stopDecisionPolling() {
    if (_decisionPolling) {
        clearInterval(_decisionPolling);
        _decisionPolling = null;
    }
}


// ============ 实时日志面板（独立轮询，只刷日志容器，不刷新框架） ============
let lastLogId = 0;
let realtimeLogPolling = null;
let logAutoScroll = true;

function renderLogLine(l) {
    const raw = String(l.level || 'INFO').toUpperCase();
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
    const stream = document.getElementById('realtime-log-stream');
    if (!stream) return;
    const levelSel = document.getElementById('log-level-select');
    const level = levelSel ? levelSel.value : 'info';
    try {
        // 仪表盘实时面板保留全局概览：显式 platform=all，避免被当前平台上下文隔离
        const data = await api.fetch(`/api/logs?level=${encodeURIComponent(level)}&since=${lastLogId}&limit=300&platform=all`);
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
    } catch (e) {
        // 静默失败：日志面板不应干扰其它功能
    }
}

function startRealtimeLogPolling() {
    if (realtimeLogPolling) clearInterval(realtimeLogPolling);
    realtimeLogPolling = setInterval(() => {
        // 仅在仪表盘页活跃时拉取，避免后台无谓请求
        if (currentPage !== 'dashboard') return;
        pollRealtimeLogs();
    }, 2000);
}

function stopRealtimeLogPolling() {
    if (realtimeLogPolling) {
        clearInterval(realtimeLogPolling);
        realtimeLogPolling = null;
    }
}


