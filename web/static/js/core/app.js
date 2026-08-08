// v2026-07-12-2343 — cache bust (fix api.js syntax)

// 使用 api.js 中已创建的全局实例（避免重复创建）
const api = window.api || new ApiClient();
let currentPage = 'dashboard';
let selectedKeywordIds = new Set();


// ============ 图片签名 token（替代 Basic Auth，兼容 <img src>） ============
// 后端 /api/image/ 已不再信任 Basic Auth（<img> 浏览器不自动带头），
// 改为校验 ?it= 签名 token。前端在已认证状态下领取并缓存，拼到图片 URL。
let _imgTok = { v: '', exp: 0 };
async function refreshImageToken() {
    try {
        const r = await api.getImageToken();
        if (r && r.token) {
            _imgTok = { v: r.token, exp: Date.now() + Math.max(0, (r.ttl || 300) * 1000 - 10000) };
        }
    } catch (e) { /* 保留旧 token，下次轮询再试 */ }
}
function imgTokUrl(url) {
    // 图片鉴权改由后端下发的 HttpOnly Cookie(img_token) 携带；URL 不再含 token/platform，
    // 地址保持稳定 → 浏览器可长期缓存，杜绝每轮 token 轮换引发的整屏图片重复下载。
    // refreshImageToken 仅周期性刷新该 Cookie（见下方 setInterval），不再改写任何 URL/DOM。
    return url;
}
// 启动即拉取，并每 4 分钟刷新（token TTL 5 分钟）。登出后不再请求，避免持续 401。
refreshImageToken();
setInterval(() => { if (api.isAuthenticated()) refreshImageToken(); }, 4 * 60 * 1000);

// Chart.js 实例缓存
let _messageTrendChart = null;
let _msgTypeChart = null;


// ============ 图表主题适配（暗色/浅色自动切换） ============
function chartTheme() {
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    return dark ? {
        grid: 'rgba(148, 163, 184, 0.14)',
        tick: 'rgba(226, 232, 240, 0.70)',
        legendText: 'rgba(226, 232, 240, 0.88)',
        tooltipBg: 'rgba(15, 23, 42, 0.95)',
        tooltipText: '#e2e8f0',
        tooltipBorder: 'rgba(148, 163, 184, 0.25)',
    } : {
        grid: 'rgba(100, 116, 139, 0.16)',
        tick: 'rgba(71, 85, 105, 0.90)',
        legendText: 'rgba(51, 65, 85, 0.90)',
        tooltipBg: 'rgba(255, 255, 255, 0.97)',
        tooltipText: '#1e293b',
        tooltipBorder: 'rgba(148, 163, 184, 0.30)',
    };
}

// 刷新所有已渲染图表的主题配色（主题切换时调用，无动画避免闪烁）
function refreshChartsTheme() {
    const ct = chartTheme();
    const charts = [_messageTrendChart, _msgTypeChart];
    try { if (typeof toolCallsChart !== 'undefined' && toolCallsChart) charts.push(toolCallsChart); } catch (e) {}
    charts.filter(Boolean).forEach(ch => {
        if (ch.options && ch.options.scales) {
            ['x', 'y'].forEach(ax => {
                const s = ch.options.scales[ax];
                if (!s) return;
                if (s.grid) s.grid.color = ct.grid;
                if (s.ticks) s.ticks.color = ct.tick;
            });
        }
        if (ch.options && ch.options.plugins) {
            const p = ch.options.plugins;
            if (p.legend && p.legend.labels) p.legend.labels.color = ct.legendText;
            if (p.tooltip) {
                p.tooltip.backgroundColor = ct.tooltipBg;
                p.tooltip.titleColor = ct.tooltipText;
                p.tooltip.bodyColor = ct.tooltipText;
                p.tooltip.borderColor = ct.tooltipBorder;
                p.tooltip.borderWidth = 1;
            }
        }
        ch.update('none');
    });
}

// 监听主题变化（由 theme.js 派发），同步刷新图表配色
window.addEventListener('dt-theme-change', refreshChartsTheme);

function formatTime(timestamp) {
    if (!timestamp) return '-';
    return new Date(timestamp).toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast show ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    setTimeout(() => { toast.className = 'toast'; toast.setAttribute('role', 'status'); }, 3000);
}
// 暴露到 window，供按页拆分的模块（如 persona.js）复用，避免 fallback 到 console
window.showToast = showToast;

// ── 统一翻页渲染器 ──
// 用法：renderPager('容器id', { total, page, pageSize }, (page) => { ...加载第page页... })
// 渲染与决策追踪一致的 Bootstrap .pagination（带省略号页码），三处列表（死信/草稿/文档）共用。
// total=0 或单页时返回空字符串（不渲染翻页条）。page 从 1 开始。
function renderPager(containerId, { total = 0, page = 1, pageSize = 20 } = {}, onPage) {
    const container = document.getElementById(containerId);
    if (!container) return '';
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (totalPages <= 1) {
        container.innerHTML = '';
        return '';
    }
    const go = (p) => `onclick="renderPager_go('${containerId}', ${p}, ${pageSize});"`;
    const delta = 2;
    const rangeStart = Math.max(1, page - delta);
    const rangeEnd = Math.min(totalPages, page + delta);

    let html = '<nav aria-label="分页"><ul class="pagination pagination-sm">';
    html += `<li class="page-item ${page <= 1 ? 'disabled' : ''}"><a class="page-link" href="#" ${page > 1 ? go(page - 1) : ''}>&laquo;</a></li>`;
    if (rangeStart > 1) {
        html += `<li class="page-item"><a class="page-link" href="#" ${go(1)}>1</a></li>`;
        if (rangeStart > 2) html += '<li class="page-item disabled"><span class="page-link">…</span></li>';
    }
    for (let i = rangeStart; i <= rangeEnd; i++) {
        html += `<li class="page-item ${i === page ? 'active' : ''}"><a class="page-link" href="#" ${go(i)}>${i}</a></li>`;
    }
    if (rangeEnd < totalPages) {
        if (rangeEnd < totalPages - 1) html += '<li class="page-item disabled"><span class="page-link">…</span></li>';
        html += `<li class="page-item"><a class="page-link" href="#" ${go(totalPages)}>${totalPages}</a></li>`;
    }
    html += `<li class="page-item ${page >= totalPages ? 'disabled' : ''}"><a class="page-link" href="#" ${page < totalPages ? go(page + 1) : ''}>&raquo;</a></li>`;
    html += '</ul></nav>';
    container.innerHTML = html;
    // 把 onPage 回调挂到全局，供翻页点击调用（闭包无法内联 onclick）
    window.__pagerCb = window.__pagerCb || {};
    window.__pagerCb[containerId] = onPage;
    return html;
}

// 翻页点击处理器：读取该容器注册的回调并跳页
function renderPager_go(containerId, page, pageSize) {
    const cb = window.__pagerCb && window.__pagerCb[containerId];
    if (typeof cb === 'function') cb(page, pageSize);
    return false;
}

// 将 UTC ISO 字符串（来自决策追踪器 datetime.now(timezone.utc).isoformat()）
// 转换为浏览器本地时区的可读时间。后端存 UTC，前端按访问者本地时区显示。
// 格式：YYYY-MM-DD HH:MM:SS（不带秒 / 带秒可切换）
// 空值、无法解析时原样返回 +08:00 假设，保证 UI 不显示 NaN。
function formatTsLocal(isoUtc, withSeconds = true) {
    if (!isoUtc) return '';
    try {
        // 后端可能写 "2026-07-12T04:30:00+00:00" 或 "2026-07-12T04:30:00Z"
        const d = new Date(isoUtc);
        if (isNaN(d.getTime())) return isoUtc;
        const pad = (n) => String(n).padStart(2, '0');
        const y = d.getFullYear();
        const m = pad(d.getMonth() + 1);
        const day = pad(d.getDate());
        const hh = pad(d.getHours());
        const mm = pad(d.getMinutes());
        const ss = pad(d.getSeconds());
        return withSeconds ? `${y}-${m}-${day} ${hh}:${mm}:${ss}` : `${y}-${m}-${day} ${hh}:${mm}`;
    } catch (e) {
        return isoUtc;
    }
}

function simpleMarkdown(md) {
    if (!md) return '';
    let html = escapeHtml(md);
    html = html.replace(/^### (.*$)/gim, '<h3>$1</h3>');
    html = html.replace(/^## (.*$)/gim, '<h2>$1</h2>');
    html = html.replace(/^# (.*$)/gim, '<h1>$1</h1>');
    html = html.replace(/\*\*([\s\S]*?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');
    html = html.replace(/`(.*?)`/g, '<code>$1</code>');
    html = html.replace(/\n/g, '<br>');
    return html;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

/* 关闭移动端侧边栏 */
function closeSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebar) sidebar.classList.remove('open');
    if (backdrop) backdrop.classList.remove('show');
    document.body.style.overflow = '';
}

function switchPage(page) {
    if (currentPage === page) return;
    // 关闭头像下拉菜单
    const userDd = document.getElementById('web-user-info');
    if (userDd) userDd.classList.remove('open');
    // 切换页面时关闭移动端侧边栏
    closeSidebar();
    // 先更新 DOM，再更新状态变量，消除 currentPage 与 DOM 的不一致窗口
    document.querySelectorAll('.nav-item').forEach(item => {
        const on = item.dataset.page === page;
        item.classList.toggle('active', on);
        // 可访问性：用 aria-current 标记当前导航项，便于屏幕阅读器识别（WCAG 4.1.2）
        if (on) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
    });
    document.querySelectorAll('.page').forEach(p => {
        p.classList.toggle('active', p.id === `page-${page}`);
    });
    const titles = {
        dashboard: '仪表盘',
        keywords: '关键词规则',
        rag: 'RAG 知识库',
        messages: '消息记录',
        intent: '意图 & 路由',
        skills: '技能管理',
        tools: '工具链路',
        deadletters: '死信队列',
        drafts: '草稿审阅',
        config: '系统配置',
        persona: '主人风格画像',
        metrics: '指标监控',
        'cost-quality': '成本 / 质量',
        logs: '运行日志',
        simulate: '模拟测试',
    };
    document.getElementById('page-title').textContent = titles[page] || page;
    currentPage = page;
    try { sessionStorage.setItem('marvis_last_page', page); } catch (_) {}
    stopIntentPolling(); // 离开任意页时停掉意图页轮询
    stopRouteTracePolling(); // 离开任意页时停掉路由追踪轮询
    stopMetricsPolling(); // 离开任意页时停掉指标监控轮询
    // 离开 RAG 页时强制隐藏关键词测试面板（已迁移到关键词页面）

    if (page === 'dashboard') {
        loadDashboard();
        startDashboardLivePolling();
        startEmbeddingStatusPolling();
    } else {
        stopDashboardLivePolling();
        stopEmbeddingStatusPolling();
        // 销毁 Chart.js 实例，防止切页后内存泄漏和 resize 事件空转
        if (_messageTrendChart) { _messageTrendChart.destroy(); _messageTrendChart = null; }
        if (_msgTypeChart) { _msgTypeChart.destroy(); _msgTypeChart = null; }
    }
    // 运行日志页：独立轮询（与仪表盘 realtime-log 面板互不干扰）
    if (page === 'logs') {
        if (typeof loadLogsPage === 'function') loadLogsPage();
        if (typeof startLogsPolling === 'function') startLogsPolling();
    } else {
        if (typeof stopLogsPolling === 'function') stopLogsPolling();
    }
    if (page === 'keywords') loadKeywords();
    if (page === 'rag') loadRagPage();
    if (page === 'messages') { loadMessages(); loadDepartments(); startMessageRefresh(); window.loadMessagesAnalytics && loadMessagesAnalytics(); }
    else { stopMessageRefresh(); }
    if (page === 'intent') loadIntentPage();
    if (page === 'skills') { loadSkillsPage(); loadMarketplace(); startSkillsAutoCheck(); }
    else { stopSkillsAutoCheck(); }
    if (page === 'tools') loadToolsPage();
    if (page === 'config') loadConfigPage();
    if (page === 'deadletters') loadDeadLettersPage();
    if (page === 'drafts') loadDraftsPage();
    if (page === 'persona') { window.loadPersonaPage && window.loadPersonaPage(); }
    if (page === 'metrics') { loadMetricsPage(); startMetricsPolling(); window.loadMetricsReliability && window.loadMetricsReliability(); }
    if (page === 'cost-quality') { loadCostQualityPage(); startCostQualityPolling(); }
    else { stopCostQualityPolling(); }
    if (page === 'simulate') { window.loadSimulatePage && window.loadSimulatePage(); }
}

// ============ 多平台隔离：平台切换器 ============
const _PLATFORM_ADAPTER_COLORS = {
    dingtalk: '#1677ff',
    feishu: '#18C08F',
    wecom: '#2ba245',
};
const _platformNames = {};

async function initPlatformSwitcher() {
    const container = document.getElementById('platform-switcher');
    if (!container) return;

    let platforms = [];
    try {
        const r = await api.fetch('/api/platforms');
        platforms = (r && r.platforms) || [];
    } catch (e) {
        platforms = [];
    }
    if (!platforms.length) {
        platforms = [{ id: 'dingtalk', display_name: '钉钉', enabled: true, adapter_type: 'dingtalk' }];
    }

    // 校验本地存储的平台是否仍有效，无效则回退 dingtalk
    const current = window.store.getPlatform();
    if (!platforms.some(p => p.id === current)) {
        window.store.setPlatform('dingtalk');
    }

    Object.keys(_platformNames).forEach(k => delete _platformNames[k]);
    container.innerHTML = '';
    platforms.forEach(p => {
        _platformNames[p.id] = p.display_name || p.id;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'platform-btn'
            + (p.id === window.store.getPlatform() ? ' active' : '')
            + (p.enabled ? '' : ' disabled');
        btn.dataset.platform = p.id;
        const color = _PLATFORM_ADAPTER_COLORS[p.adapter_type] || 'var(--brand-primary)';
        btn.innerHTML = `<span class="pf-dot" style="background:${color}"></span>`
            + `<span class="pf-label">${escapeHtml(p.display_name || p.id)}</span>`
            + (p.enabled ? '' : `<span class="pf-badge" title="${escapeHtml(p.disabled_reason || '该平台未启用，请联系管理员配置')}">禁用</span>`);
        btn.addEventListener('click', () => {
            if (!p.enabled) {
                showToast(`${p.display_name || p.id} 未启用`, 'info');
                return;
            }
            switchPlatform(p.id);
        });
        container.appendChild(btn);
    });

    // 订阅平台变化，同步按钮高亮（切换时实时更新）
    window.store.subscribe('platform', (pid) => {
        container.querySelectorAll('.platform-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.platform === pid);
        });
    });
}

function switchPlatform(pid) {
    const prev = window.store.getPlatform();
    if (prev === pid) return;
    window.store.setPlatform(pid);
    // 清空前端数据缓存 + 请求缓存，强制按新平台隔离重渲染
    window.store.clearData();
    api.clearCache();
    showToast('已切换到 ' + (_platformNames[pid] || pid), 'success');
    reloadCurrentPage();
}

function reloadCurrentPage() {
    const page = currentPage || 'dashboard';
    // 临时重置 currentPage 绕过 switchPage 同页短路，确保重载数据 + 启停轮询
    currentPage = '';
    switchPage(page);
}


// ============ Init ============
window.switchPage = switchPage;

window.loadKeywords = loadKeywords;
window.toggleAllKwSelect = toggleAllKwSelect;
window.toggleKwSelect = toggleKwSelect;
window.showKeywordModal = showKeywordModal;
window.closeKeywordModal = closeKeywordModal;
window.saveKeyword = saveKeyword;
window.editKeyword = editKeyword;
window.deleteKeyword = deleteKeyword;
window.batchEnable = batchEnable;
window.batchDisable = batchDisable;
window.batchDelete = batchDelete;
window.showImportModal = showImportModal;
window.closeImportModal = closeImportModal;
window.handleImportFile = handleImportFile;
window.clearImportPreview = clearImportPreview;
window.confirmImport = confirmImport;
window.exportKeywords = exportKeywords;
window.testMatch = testMatch;
window.clearKwTest = clearKwTest;

window.switchRagTab = switchRagTab;
window.loadKbDocs = loadKbDocs;
window.viewKbDoc = viewKbDoc;
window.reindexKbDoc = reindexKbDoc;
window.deleteKbDoc = deleteKbDoc;
window.showKbModal = showKbModal;
window.closeKbModal = closeKbModal;
window.saveKbDocument = saveKbDocument;
window.showBatchUploadModal = showBatchUploadModal;
window.closeBatchUploadModal = closeBatchUploadModal;
window.handleDragOver = handleDragOver;
window.handleDragLeave = handleDragLeave;
window.handleDrop = handleDrop;
window.handleBatchFileSelect = handleBatchFileSelect;
window.removeBatchFile = removeBatchFile;
window.startBatchUpload = startBatchUpload;
window.showImportUrlModal = showImportUrlModal;
window.closeImportUrlModal = closeImportUrlModal;
window.startImportUrl = startImportUrl;
window.viewKbDocChunks = viewKbDocChunks;
window.sendRagChat = sendRagChat;
window.handleChatKeydown = handleChatKeydown;
window.saveChunkConfig = saveChunkConfig;
window.testRagSearch = testRagSearch;
window.loadMemoryList = loadMemoryList;
window.editMemory = editMemory;
window.deleteMemoryConfirm = deleteMemoryConfirm;
window.clearRagSearch = clearRagSearch;

window.closeDocViewModal = closeDocViewModal;

window.loadMessages = loadMessages;
window.saveConfig = saveConfig;
window.restoreDefaultConfig = restoreDefaultConfig;
window.rechunkAllDocs = rechunkAllDocs;
window.exportConfig = exportConfig;
window.closeDocEditModal = closeDocEditModal;
window.saveDocEdit = saveDocEdit;
window.editKbDoc = editKbDoc;


// ============ Initialization ============
async function init() {
    // Bind navigation clicks
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const page = item.dataset.page;
            if (page) switchPage(page);
            // 移动端：点击导航项后自动关闭侧边栏
            closeSidebar();
        });
    });

    // 移动端首次加载强制收起侧边栏（防御性）
    if (window.innerWidth <= 768) {
        closeSidebar();
    }
    // 双重兜底：延迟 100ms 再判断一次，防止 DOM/样式/CSS 加载时序导致侧边栏残影
    setTimeout(() => {
        if (window.innerWidth <= 768) closeSidebar();
    }, 100);

    // 移动端汉堡菜单：切换侧边栏
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (sidebarToggle && sidebar && backdrop) {
        sidebarToggle.addEventListener('click', () => {
            const isOpen = sidebar.classList.toggle('open');
            backdrop.classList.toggle('show', isOpen);
            if (isOpen) {
                document.body.style.overflow = 'hidden';
            } else {
                document.body.style.overflow = '';
            }
        });
        backdrop.addEventListener('click', () => {
            closeSidebar();
        });
        // 侧边栏内的 nav-item 点击：通过冒泡委托统一处理
        sidebar.addEventListener('click', (e) => {
            if (e.target.closest('.nav-item')) {
                closeSidebar();
            }
        });
    }

    // ── 头像下拉菜单切换 ──
    const userInfoEl = document.getElementById('web-user-info');
    if (userInfoEl) {
        userInfoEl.addEventListener('click', (e) => {
            e.stopPropagation();
            userInfoEl.classList.toggle('open');
        });
    }
    // 点击外部关闭下拉菜单
    document.addEventListener('click', (e) => {
        const el = document.getElementById('web-user-info');
        if (el && !el.contains(e.target)) {
            el.classList.remove('open');
        }
    });

    // Bind any [data-page] link (e.g. in-page quick jump to a sidebar page)
    document.querySelectorAll('a[data-page], .link-to-config[data-page]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const page = item.dataset.page;
            if (page) switchPage(page);
        });
    });

    // Load initial data
    if (!api.isAuthenticated()) {
        showLoginOverlay();
    } else {
        // 刷新后恢复上次浏览的页面（首次访问默认仪表盘）
        // 注意：currentPage 初始为 'dashboard'，switchPage('dashboard') 会因页面未改变而短路返回，
        // 导致 loadDashboard/lodaDashboardData 不会被触发。因此对仪表盘场景需直接调用 loadDashboard。
        // 允许的页面白名单（与 switchPage 的 titles 保持一致）
        const ALLOWED_PAGES = ['dashboard', 'keywords', 'rag', 'messages', 'intent', 'skills', 'tools', 'config', 'deadletters', 'drafts', 'persona', 'metrics', 'cost-quality', 'logs', 'simulate'];
        const saved = (() => { try { return sessionStorage.getItem('marvis_last_page'); } catch (_) { return null; } })();
        if (saved && ALLOWED_PAGES.includes(saved)) {
            if (saved === 'dashboard') {
                loadDashboard();
            } else {
                switchPage(saved);
            }
        } else {
            loadDashboard();
        }
        // 仅仪表盘直接 loadDashboard() 绕过了 switchPage，需手动启停轮询
        if (!saved || saved === 'dashboard') {
            startDashboardLivePolling();
            startEmbeddingStatusPolling();
        }
    }

    // 实时日志面板控件绑定
    const logLevelSel = document.getElementById('log-level-select');
    if (logLevelSel) {
        logLevelSel.addEventListener('change', () => {
            lastLogId = 0;  // 切换级别时重置增量游标，重新拉取全量
            const s = document.getElementById('realtime-log-stream');
            if (s) s.innerHTML = '<div class="rt-log-empty"><span class="empty-icon">📜</span><span>等待日志输出…</span></div>';
            pollRealtimeLogs();
        });
    }
    const logAuto = document.getElementById('log-autoscroll');
    if (logAuto) {
        logAutoScroll = logAuto.checked;
        logAuto.addEventListener('change', (e) => { logAutoScroll = e.target.checked; });
    }
    const logClear = document.getElementById('log-clear-btn');
    if (logClear) {
        logClear.addEventListener('click', () => {
            const s = document.getElementById('realtime-log-stream');
            if (s) s.innerHTML = '<div class="rt-log-empty"><span class="empty-icon">📜</span><span>已清空，等待新日志…</span></div>';
        });
    }

    // Auto-refresh dashboard every 30s (silent, no skeleton flash)
    setInterval(() => {
        if (api.isAuthenticated() && currentPage === 'dashboard') {
            loadDashboardData(false);
        }
    }, 30000);

    // Update sidebar status
    const statusIcon = document.getElementById('sidebar-status-icon');
    const statusText = document.getElementById('sidebar-status-text');
    if (statusIcon && statusText) {
        statusIcon.className = 'sidebar-status-icon ok';
        statusText.className = 'sidebar-status-text ok';
        statusText.textContent = '运行正常';
    }

    // 多平台隔离：渲染平台切换器（/api/platforms 免认证，登录前即可渲染）
    try { await initPlatformSwitcher(); } catch (e) { console.error('[platform] 切换器初始化失败:', e); }

    // Expose key functions to global scope for debugging/testing and inline handlers
    window.switchPage = switchPage;
    window.switchPlatform = switchPlatform;
    window.initPlatformSwitcher = initPlatformSwitcher;
    window.loadSkillsPage = loadSkillsPage;
    window.loadDeadLettersPage = loadDeadLettersPage;
    window.loadDraftsPage = loadDraftsPage;
    // drafts.js 是 type=module（defer 执行），在 app.js 同步求值时 loadDraftsPage 尚不可用。
    // 把 debounced 包装放在 init()（DOMContentLoaded 后）创建，确保函数已暴露到全局。
    window.debouncedLoadDraftsPage = debounce(loadDraftsPage, 300);

    window.loadDashboard = loadDashboard;
    window.loadDashboardData = loadDashboardData;
    window.loadMessages = loadMessages;
    window.syncHistory = syncHistory;
    window.selectMessageConversation = selectMessageConversation;
}

// ---- 全局状态：必须声明在 init() 之前，避免同步执行 init 时触发 TDZ ----
// 本脚本以经典 <script>（非 module）加载并置于各 pages/*.js 之后。若文档已解析
// （readyState !== 'loading'），init() 会在脚本求值阶段同步运行；而下方的
// let _marketState 等声明位于文件更靠后，提前到这里可确保 init →
// switchPage('skills') → loadMarketplace 访问时它们已初始化。
let _installedSkillNames = new Set();
let _marketState = {
    loaded: false,
    loading: false,
    sections: null,   // {all, featured, trending, hot, newest, stars}
    tab: 'all',
    keyword: '',
    page: 1,         // 当前分页（1-based）
    apiKey: 'all',   // 全局筛选：all / required(需要 API Key) / none(无需 API Key)
    searchResults: null,  // 搜索结果（API 返回的完整列表，非榜单切片）；null=未搜索或正在搜索
    searchLoading: false, // 正在调搜索接口
    searchError: null,    // 搜索失败原因
};
let _marketSearchTimer = null;  // 搜索 debounce 句柄
const _MARKET_TAB_NAMES = {
    all: '全部', featured: '推荐精选', trending: '近期飙升',
    hot: '下载量', stars: '收藏量', newest: '最近上新',
};
const _MARKET_CAT_LABELS = {
    'ai-agent': 'AI 智能体', 'knowledge-management': '知识管理', 'office': '办公效率',
    'content-creation': '内容创作', 'ai-agent-framework': 'Agent 框架', 'data-analysis': '数据分析',
    'dev-programming': '开发编程', 'design-media': '设计多媒体', 'business-operations': '商业运营',
    'life-services': '生活服务', 'it-ops-security': 'IT 运维与安全', 'finance': '金融',
    'education': '教育', 'productivity': '效率工具', 'writing': '写作',
};

// Run initialization when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}


// ============ Web Auth ============
function showLoginOverlay() {
    // 保存当前页面，登录后恢复（避免登录后跳到仪表盘）
    window._preLoginPage = currentPage;
    // 停止所有轮询，防止 401 风暴
    stopDashboardLivePolling();
    document.getElementById('login-overlay').style.display = 'flex';
    document.getElementById('login-error').textContent = '';
    document.getElementById('login-username').value = '';
    document.getElementById('login-password').value = '';
    setTimeout(() => document.getElementById('login-username').focus(), 100);
}

function hideLoginOverlay() {
    document.getElementById('login-overlay').style.display = 'none';
}

async function doLogin() {
    if (window.__loginInProgress) return;  // 防连按回车/重复提交
    const username = document.getElementById('login-username').value.trim();
    const password = document.getElementById('login-password').value;
    const errorEl = document.getElementById('login-error');

    if (!username || !password) {
        errorEl.textContent = '请输入用户名和密码';
        return;
    }

    errorEl.textContent = '';
    window.__loginInProgress = true;
    try {
    api.setAuth(username, password);

    // 验证凭证：调用一个受保护的 API
    const data = await api.getStatus();
    if (data && data.status === 'running') {
        hideLoginOverlay();
        updateWebUserInfo(username);
        showToast('登录成功', 'success');
        await refreshImageToken();  // 预取图片 token，避免首屏图片 401 裂图
        try { await initPlatformSwitcher(); } catch (e) {}  // 登录后刷新平台列表

        // 恢复到登录前所在页面（非首次登录时则不跳到仪表盘）
        const prevPage = window._preLoginPage;
        delete window._preLoginPage;
        if (prevPage && prevPage !== 'dashboard') {
            // 临时重置 currentPage 以绕过 switchPage 的同页短路检查
            currentPage = '';
            switchPage(prevPage);
        } else {
            loadDashboard();
            startDashboardLivePolling();
            startEmbeddingStatusPolling();
        }
    } else if (data && data.error === 'unauthorized') {
        api.clearAuth();
        errorEl.textContent = '用户名或密码错误';
    } else {
        api.clearAuth();
        errorEl.textContent = '登录失败，请检查网络或服务状态';
    }
    } catch (e) {
        api.clearAuth();
        errorEl.textContent = '登录失败，请检查网络或服务状态';
    } finally {
        window.__loginInProgress = false;
    }
}

function doLogout() {
    api.clearAuth();
    updateWebUserInfo(null);
    showLoginOverlay();
    showToast('已退出登录', 'info');
}

function updateWebUserInfo(username) {
    const infoEl = document.getElementById('web-user-info');
    if (username) {
        document.getElementById('web-user-name').textContent = username;
        infoEl.style.display = 'flex';
    } else {
        infoEl.style.display = 'none';
    }
}

async function checkWebAuth() {
    if (api.isAuthenticated()) {
        const data = await api.getStatus();
        if (data && data.status === 'running') {
            const realName = (data.user || {}).name || '管理员';
            updateWebUserInfo(realName);
            return;
        }
        api.clearAuth();
    }

    try {
        const res = await fetch('/api/status', { credentials: 'omit' });
        if (res.status === 401) {
            showLoginOverlay();
        } else {
            updateWebUserInfo(null);
        }
    } catch (e) {
        console.error('Auth check failed:', e);
        showLoginOverlay();
    }
}

// 监听 401 事件
window.addEventListener('web-auth-required', () => {
    showLoginOverlay();
});

// 页面加载时检查认证
document.addEventListener('DOMContentLoaded', () => {
    checkWebAuth();
});

// 暴露钉钉文档导入弹窗函数到全局（因为 app.js 是 type="module"）
window.showDingtalkImportModal = showDingtalkImportModal;
window.closeDingtalkImportModal = closeDingtalkImportModal;
window.refreshDingtalkImportList = refreshDingtalkImportList;
window.selectDingtalkImportDoc = selectDingtalkImportDoc;
window.searchDingtalkImport = searchDingtalkImport;
window.syncAndSelectDingtalkDoc = syncAndSelectDingtalkDoc;
window.confirmDingtalkImport = confirmDingtalkImport;
window.doLogin = doLogin;
window.doLogout = doLogout;
window.showLoginOverlay = showLoginOverlay;

// ===== 图片灯箱：当前页弹出查看大图（替代 window.open 跳转新窗口）=====
function openImageLightbox(src) {
    const modal = document.getElementById('image-lightbox');
    const img = document.getElementById('image-lightbox-img');
    img.src = src;
    document.getElementById('image-lightbox-open').href = src;
    document.getElementById('image-lightbox-download').href = src;
    modal.classList.add('active');
    document.addEventListener('keydown', _lightboxEscHandler);
}

function closeImageLightbox() {
    const modal = document.getElementById('image-lightbox');
    modal.classList.remove('active');
    document.getElementById('image-lightbox-img').src = '';
    document.removeEventListener('keydown', _lightboxEscHandler);
}

function _lightboxEscHandler(e) {
    if (e.key === 'Escape') closeImageLightbox();
}

window.openImageLightbox = openImageLightbox;
window.closeImageLightbox = closeImageLightbox;

// ===== 工具：防抖（搜索框复用，减少请求与免费 LLM 限流风险）=====
function debounce(fn, wait = 300) {
    let t;
    return function (...args) {
        clearTimeout(t);
        t = setTimeout(() => fn.apply(this, args), wait);
    };
}
window.debouncedLoadKeywords = debounce(loadKeywords, 300);
window.debouncedLoadKbDocs = debounce(loadKbDocs, 300);
window.debouncedLoadMessages = debounce(loadMessages, 300);
window.debouncedRefreshDingtalkImportList = debounce(refreshDingtalkImportList, 300);
window.debouncedFilterThread = debounce(filterThread, 300);
// 死信/草稿搜索框：过滤本就是客户端（_dlqFilterItems/_draftFilterItems），
// 但 oninput 直接触发整页服务端重载 → 中文 IME 下 8–10 次/键击 = 8–10 次全量请求。
// 套 debounce(300) 把请求收敛到输入停顿后一次（过滤仍由 load 内客户端逻辑完成）。
window.debouncedLoadDeadLettersPage = debounce(loadDeadLettersPage, 300);

// ===== 全局模态框行为：Esc 关闭 + 背景点击关闭 + 焦点管理 + 无障碍 =====
(function () {
    const modals = Array.from(document.querySelectorAll('.modal:not(.image-lightbox)'));
    let lastTrigger = null;

    function topModal() {
        const actives = modals.filter(m => m.classList.contains('active'));
        return actives[actives.length - 1] || null;
    }
    function closeModal(m) {
        if (!m) return;
        const btn = m.querySelector('[onclick^="close"]');
        if (btn) btn.click();
        else m.classList.remove('active');
        if (lastTrigger && document.contains(lastTrigger)) {
            try { lastTrigger.focus(); } catch (e) {}
        }
        lastTrigger = null;
    }

    modals.forEach(m => {
        m.setAttribute('role', 'dialog');
        m.setAttribute('aria-modal', 'true');
        const closeBtn = m.querySelector('[onclick^="close"]');
        if (closeBtn && !closeBtn.getAttribute('aria-label')) {
            closeBtn.setAttribute('aria-label', '关闭');
        }
        // 点击遮罩本身（非内容）关闭
        m.addEventListener('click', e => { if (e.target === m) closeModal(m); });
    });

    // Esc 关闭最上层模态框
    document.addEventListener('keydown', e => {
        if (e.key === 'Tab') {
            const m = topModal();
            if (m) {
                const f = m.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])');
                if (f.length === 0) { e.preventDefault(); return; }
                const first = f[0];
                const last = f[f.length - 1];
                if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
                else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
            }
        }
        if (e.key === 'Escape') {
            const m = topModal();
            if (m) { e.preventDefault(); closeModal(m); }
        }
        // Ctrl+S: 保存配置（仅 config 页）
        if ((e.ctrlKey || e.metaKey) && e.key === 's') {
            if (currentPage === 'config') {
                e.preventDefault();
                if (typeof saveConfig === 'function') saveConfig();
            }
        }
        // Ctrl+Enter: 发送模拟测试（仅 simulate 页）
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            if (currentPage === 'simulate') {
                e.preventDefault();
                if (typeof sendSimulatedMessage === 'function') sendSimulatedMessage();
            }
        }
    });

    // 打开时聚焦首个输入控件，关闭时归还焦点给触发元素（覆盖 X / Esc / 背景所有路径）
    const obs = new MutationObserver(muts => {
        muts.forEach(mu => {
            if (mu.attributeName !== 'class') return;
            const m = mu.target;
            if (m.classList.contains('active')) {
                if (!lastTrigger) lastTrigger = document.activeElement;
                const f = m.querySelector('input, textarea, select, button:not(.modal-close)');
                if (f) setTimeout(() => { try { f.focus(); } catch (e) {} }, 30);
            } else {
                if (lastTrigger && document.contains(lastTrigger)) {
                    try { lastTrigger.focus(); } catch (e) {}
                }
                lastTrigger = null;
            }
        });
    });
    modals.forEach(m => obs.observe(m, { attributes: true, attributeFilter: ['class'] }));
})();


