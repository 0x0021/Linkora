// ============ pages/skills.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动
// 平台筛选已统一由顶部全局 platform-switcher 接管（api.js _withPlatform 自动追加 ?platform=）

// ============ 安装技能平台选择器：跟随全局平台 + 隔离 ============
// 安装技能时的平台下拉框自动选中当前全局平台，并隐藏其他平台专属选项，
// 确保钉钉专属不会通过安装器暴露给飞书/企微（反之亦然）。
function _syncSkillPlatformSelector() {
    const sel = document.getElementById('skill-platform-select');
    if (!sel) return;
    const globalPlat = (window.store && typeof window.store.getPlatform === 'function')
        ? window.store.getPlatform() : 'dingtalk';

    // 重建选项：通用 + 当前平台专属（隐藏其他平台）
    const opts = [
        { value: '', label: '通用' },
        { value: globalPlat, label: _platformLabel(globalPlat) + '专属' },
    ];
    sel.innerHTML = opts.map(o =>
        `<option value="${o.value}">${o.label}</option>`
    ).join('');
    // 默认选中当前平台专属
    sel.value = globalPlat;
}

// 订阅全局平台变化，实时同步选择器
if (window.store && typeof window.store.subscribe === 'function') {
    window.store.subscribe('platform', () => _syncSkillPlatformSelector());
}

// ============ SkillHub 技能市场（榜单式，参考 skillhub.cn/skills） ============
// 注：_marketState / _installedSkillNames / _MARKET_TAB_NAMES / _MARKET_CAT_LABELS
//     已在文件顶部（init 之前）声明，避免在模块同步初始化时触发 TDZ。

function _fmtNum(n) {
    n = Number(n) || 0;
    if (n >= 100000000) return (n / 100000000).toFixed(1).replace(/\.0$/, '') + '亿';
    if (n >= 10000) return (n / 10000).toFixed(1).replace(/\.0$/, '') + '万';
    return String(n);
}
function _platformLabel(p) {
    const map = { dingtalk: '钉钉', feishu: '飞书', wecom: '企微', all: '通用' };
    return map[p] || p;
}

// ============ 平台专有/公开切换 ============

function togglePlatformDropdown(e, name) {
    e.stopPropagation();
    const dd = document.getElementById('pdd-' + name);
    if (!dd) return;
    // 关闭所有其他弹层
    document.querySelectorAll('.platform-dropdown').forEach(el => { if (el !== dd) el.style.display = 'none'; });
    dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
}
window.togglePlatformDropdown = togglePlatformDropdown;

async function setSkillPlatform(name, platform) {
    // 先关闭弹层
    document.querySelectorAll('.platform-dropdown').forEach(el => el.style.display = 'none');
    try {
        const resp = await api.fetch('/api/skills/' + encodeURIComponent(name), 'PUT', { platforms: [platform] });
        if (!resp || resp.error) {
            alert('设置失败：' + (resp?.detail || resp?.error || '未知错误'));
            return;
        }
        loadSkillsPage();
    } catch (e) {
        alert('设置失败：' + (e.message || String(e)));
    }
}
window.setSkillPlatform = setSkillPlatform;

// ============ 技能管理：子页面切换（SkillHub 技能市场 / 已有技能管理） ============
// 两个子页面的 DOM 在 loadSkillsPage 时都已渲染好，这里只切换可见性 + 高亮态，
// 保证切换瞬时、无重新拉取。返回市场子页时重算标签滑动指示条（隐藏态下
// getBoundingClientRect 为 0，需重新测量）。
function switchSkillSubPage(sub) {
    const valid = sub === 'market' || sub === 'installed' ? sub : 'market';
    document.querySelectorAll('.skill-subnav-btn').forEach(b => {
        const on = b.dataset.sub === valid;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const market = document.getElementById('subpage-skills-market');
    const installed = document.getElementById('subpage-skills-installed');
    if (market) market.style.display = valid === 'market' ? '' : 'none';
    if (installed) installed.style.display = valid === 'installed' ? '' : 'none';
    if (valid === 'market') {
        // 市场子页重新可见后，重算 slider 指示条位置
        if (typeof _moveMarketIndicator === 'function') {
            requestAnimationFrame(_moveMarketIndicator);
        }
    }
}
window.switchSkillSubPage = switchSkillSubPage;

async function setSkillPublic(name) {
    try {
        const resp = await api.fetch('/api/skills/' + encodeURIComponent(name), 'PUT', { platforms: [] });
        if (!resp || resp.error) {
            alert('设置失败：' + (resp?.detail || resp?.error || '未知错误'));
            return;
        }
        loadSkillsPage();
    } catch (e) {
        alert('设置失败：' + (e.message || String(e)));
    }
}
window.setSkillPublic = setSkillPublic;

// 点击空白关闭所有平台下拉
document.addEventListener('click', () => {
    document.querySelectorAll('.platform-dropdown').forEach(el => el.style.display = 'none');
});

function _catLabel(cat) {
    return _MARKET_CAT_LABELS[cat] || cat || '';
}

function _updateInstalledNames() {
    _installedSkillNames.clear();
    const container = document.getElementById('skills-content');
    if (!container) return;
    const cards = container.querySelectorAll('.skill-card');
    cards.forEach(card => {
        const nameEl = card.querySelector('.skill-name');
        if (nameEl) _installedSkillNames.add(nameEl.textContent.trim());
    });
}

function _renderMarketplaceCard(s) {
    const installed = _installedSkillNames.has(s.slug) || _installedSkillNames.has(s.name);
    const slug = s.slug || s.name;
    const icon = s.iconUrl
        ? `<img src="${escapeHtml(s.iconUrl)}" class="mk-card-icon" alt="" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
           <div class="mk-card-icon mk-card-icon-fallback" style="display:none;"><i class="fa-solid fa-cube"></i></div>`
        : `<div class="mk-card-icon mk-card-icon-fallback"><i class="fa-solid fa-cube"></i></div>`;
    const verified = s.verified
        ? `<span class="mk-verified" title="官方认证"><i class="fa-solid fa-circle-check"></i></span>` : '';
    const apiTag = s.requires_api_key ? `<span class="mk-tag mk-tag-warn">需 API Key</span>` : '';
    const catTag = s.category ? `<span class="mk-tag">${escapeHtml(_catLabel(s.category))}</span>` : '';
    const subTags = (s.subCategories || []).slice(0, 2)
        .map(t => `<span class="mk-tag mk-tag-soft">${escapeHtml(t)}</span>`).join('');

    const btn = installed
        ? `<button class="btn btn-sm mk-installed" disabled><i class="fa-solid fa-check"></i> 已安装</button>`
        : `<button class="btn btn-sm btn-primary" onclick="installFromMarketplace('${escapeHtml(slug)}')"><i class="fa-solid fa-download"></i> 安装</button>`;

    return `
    <div class="mk-card" data-slug="${escapeHtml(slug)}">
        <div class="mk-card-top">
            ${icon}
            <div class="mk-card-headtext">
                <div class="mk-card-name" title="${escapeHtml(s.name)}">${escapeHtml(s.name)} ${verified}</div>
                <div class="mk-card-author">@${escapeHtml(s.author || 'unknown')}</div>
            </div>
            ${btn}
        </div>
        <p class="mk-card-desc">${escapeHtml(s.description || '')}</p>
        <div class="mk-card-tags">${catTag}${apiTag}${subTags}</div>
        <div class="mk-card-stats">
            <span class="mk-stat" title="下载量"><i class="fa-solid fa-download"></i> ${_fmtNum(s.downloads)}</span>
            <span class="mk-stat" title="收藏量"><i class="fa-solid fa-star"></i> ${_fmtNum(s.stars)}</span>
            <span class="mk-stat" title="安装量"><i class="fa-solid fa-cloud-arrow-down"></i> ${_fmtNum(s.installs)}</span>
            ${s.version ? `<span class="mk-stat mk-stat-ver">v${escapeHtml(s.version)}</span>` : ''}
        </div>
    </div>`;
}

function _moveMarketIndicator() {
    const tabs = document.getElementById('marketplace-tabs');
    const ind = document.getElementById('market-tab-indicator');
    if (!tabs || !ind) return;
    const active = tabs.querySelector('.market-tab.active');
    if (!active) return;
    ind.style.width = active.offsetWidth + 'px';
    ind.style.transform = `translateX(${active.offsetLeft}px)`;
}

// ---- 市场分页：每屏约 3 行，列数按容器宽度动态计算（须与 .marketplace-grid 的 minmax/gap 一致）----
const _MARKET_ROWS_PER_PAGE = 3;
const _MARKET_CARD_MIN = 258;   // 须与 theme.css 中 .marketplace-grid 的 minmax 宽度一致
const _MARKET_GRID_GAP = 14;    // 须与 theme.css 中 .marketplace-grid 的 gap 一致

function _getMarketCols() {
    const area = document.getElementById('skill-marketplace-area');
    const w = area ? (area.clientWidth || area.getBoundingClientRect().width) : 0;
    if (!w) return 1;
    return Math.max(1, Math.floor((w + _MARKET_GRID_GAP) / (_MARKET_CARD_MIN + _MARKET_GRID_GAP)));
}

function _getMarketPageSize() {
    return _getMarketCols() * _MARKET_ROWS_PER_PAGE;
}

function _renderMarketPager(totalPages) {
    if (totalPages <= 1) return '';
    const cur = _marketState.page;
    const pagerBtn = (html, target, opts = {}) => {
        const disabled = opts.disabled ? ' disabled' : '';
        const cls = opts.active ? ' active' : '';
        return `<button class="mk-pager-btn${cls}"${disabled} onclick="marketGotoPage(${target})">${html}</button>`;
    };
    const start = Math.max(1, cur - 2);
    const end = Math.min(totalPages, cur + 2);
    let nums = '';
    if (start > 1) {
        nums += pagerBtn('1', 1, { active: cur === 1 });
        if (start > 2) nums += `<span class="mk-pager-ellipsis">…</span>`;
    }
    for (let i = start; i <= end; i++) {
        nums += pagerBtn(String(i), i, { active: i === cur });
    }
    if (end < totalPages) {
        if (end < totalPages - 1) nums += `<span class="mk-pager-ellipsis">…</span>`;
        nums += pagerBtn(String(totalPages), totalPages, { active: cur === totalPages });
    }
    return `<div class="marketplace-pager">` +
        `<button class="mk-pager-btn"${cur <= 1 ? ' disabled' : ''} onclick="marketChangePage(-1)" aria-label="上一页"><i class="fa-solid fa-chevron-left"></i></button>` +
        nums +
        `<button class="mk-pager-btn"${cur >= totalPages ? ' disabled' : ''} onclick="marketChangePage(1)" aria-label="下一页"><i class="fa-solid fa-chevron-right"></i></button>` +
        `<span class="mk-pager-info">第 ${cur} / ${totalPages} 页</span>` +
        `</div>`;
}

function marketChangePage(delta) {
    _marketState.page += delta;
    renderMarketplace();
}
function marketGotoPage(n) {
    _marketState.page = n;
    renderMarketplace();
}
window.marketChangePage = marketChangePage;
window.marketGotoPage = marketGotoPage;

function renderMarketplace() {
    const area = document.getElementById('skill-marketplace-area');
    const meta = document.getElementById('marketplace-meta');
    if (!area) return;
    if (!_marketState.sections && !_marketState.keyword.trim()) {
        area.innerHTML = `<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:28px;color:#f59e0b;"></i><p style="margin-top:8px;color:#999;">市场数据未加载</p></div>`;
        return;
    }

    const kw = _marketState.keyword.trim().toLowerCase();
    const isSearch = kw.length > 0;

    // 决定数据源：搜索模式用服务端 API 结果（完整列表），榜单模式用当前 tab 切片
    let list;
    if (isSearch) {
        if (_marketState.searchLoading && !_marketState.searchResults) {
            area.innerHTML = `<div class="marketplace-grid marketplace-skeleton">${
                Array.from({ length: 6 }).map(() => `<div class="mk-card mk-skeleton"></div>`).join('')
            }</div>`;
            if (meta) meta.innerHTML = `<span class="mk-meta-tab">搜索结果</span> · 搜索 “${escapeHtml(_marketState.keyword.trim())}”...`;
            return;
        }
        if (_marketState.searchError) {
            area.innerHTML = `<div class="alert alert-error">搜索失败: ${escapeHtml(_marketState.searchError)}</div>`;
            if (meta) meta.innerHTML = `<span class="mk-meta-tab">搜索结果</span> · 搜索 “${escapeHtml(_marketState.keyword.trim())}”`;
            return;
        }
        if (!_marketState.searchResults) {
            // 初始未触发搜索（debounce 还没到），提示等待
            area.innerHTML = `<div class="empty-state"><i class="fa-solid fa-spinner fa-spin" style="font-size:28px;color:#ccc;"></i><p style="margin-top:8px;color:#999;">正在搜索 “${escapeHtml(_marketState.keyword.trim())}”...</p></div>`;
            if (meta) meta.innerHTML = `<span class="mk-meta-tab">搜索结果</span> · 搜索 “${escapeHtml(_marketState.keyword.trim())}”`;
            return;
        }
        list = _marketState.searchResults.slice();
    } else {
        list = (_marketState.sections[_marketState.tab] || []).slice();
    }

    // 全局筛选：需要 / 无需 API Key
    // 注意：下方 API Key 筛选会重新赋值，必须用 let（const 重赋值在 Safari 下报 Attempted to assign to readonly property）
    let filtered;
    if (isSearch) {
        // 搜索结果由服务端负责关键词匹配，前端不再做 includes 过滤
        filtered = list;
    } else {
        filtered = kw
            ? list.filter(s =>
                (s.name || '').toLowerCase().includes(kw) ||
                (s.description || '').toLowerCase().includes(kw) ||
                (s.author || '').toLowerCase().includes(kw) ||
                (s.category || '').toLowerCase().includes(kw) ||
                (s.subCategories || []).join(' ').toLowerCase().includes(kw))
            : list;
    }

    if (_marketState.apiKey === 'required') filtered = filtered.filter(s => !!s.requires_api_key);
    else if (_marketState.apiKey === 'none') filtered = filtered.filter(s => !s.requires_api_key);

    if (meta) {
        const apiLabel = _marketState.apiKey === 'required' ? '需要 API Key'
            : _marketState.apiKey === 'none' ? '无需 API Key' : '';
        const tabLabel = isSearch ? '搜索结果' : (_MARKET_TAB_NAMES[_marketState.tab] || '');
        meta.innerHTML = `<span class="mk-meta-tab">${tabLabel}</span>` +
            ` · 共 <b>${filtered.length}</b> 个技能` +
            (apiLabel ? ` · ${apiLabel}` : '') +
            (kw ? `（搜索 “${escapeHtml(_marketState.keyword.trim())}”）` : '');
    }

    if (filtered.length === 0) {
        const emptyHint = isSearch
            ? `<p style="margin-top:8px;color:#999;">未找到匹配 “${escapeHtml(_marketState.keyword.trim())}” 的技能，试试换个关键词或清空搜索浏览榜单</p>`
            : `<p style="margin-top:8px;color:#999;">未找到匹配的技能</p>`;
        area.innerHTML = `<div class="empty-state"><i class="fa-solid fa-search" style="font-size:28px;color:#ccc;"></i>${emptyHint}</div>`;
        return;
    }

    // 分页：每屏约 3 行（列数按容器宽度动态计算）
    const pageSize = _getMarketPageSize();
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (_marketState.page > totalPages) _marketState.page = totalPages;
    if (_marketState.page < 1) _marketState.page = 1;
    const start = (_marketState.page - 1) * pageSize;
    const pageItems = filtered.slice(start, start + pageSize);

    _updateInstalledNames();
    area.innerHTML = `<div class="marketplace-grid">${pageItems.map(s => _renderMarketplaceCard(s)).join('')}</div>` +
        _renderMarketPager(totalPages);
    requestAnimationFrame(_moveMarketIndicator);
}

function switchMarketTab(tab) {
    _marketState.tab = tab;
    _marketState.page = 1;
    // 切 tab 时清空搜索状态（回到榜单视图；用户需切回关键词重新搜）
    if (_marketState.keyword.trim()) {
        _marketState.keyword = '';
        const input = document.getElementById('skill-market-search');
        if (input) input.value = '';
        _marketState.searchResults = null;
        _marketState.searchLoading = false;
        _marketState.searchError = null;
        if (_marketSearchTimer) { clearTimeout(_marketSearchTimer); _marketSearchTimer = null; }
    }
    const tabs = document.getElementById('marketplace-tabs');
    if (tabs) {
        tabs.querySelectorAll('.market-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
    }
    renderMarketplace();
    _moveMarketIndicator();
}

function onMarketSearchInput() {
    const input = document.getElementById('skill-market-search');
    _marketState.keyword = input ? input.value : '';
    _marketState.page = 1;

    const kw = _marketState.keyword.trim();

    // 清空搜索 → 重置为榜单 tab 的本地过滤视图
    if (!kw) {
        _marketState.searchResults = null;
        _marketState.searchLoading = false;
        _marketState.searchError = null;
        if (_marketSearchTimer) { clearTimeout(_marketSearchTimer); _marketSearchTimer = null; }
        renderMarketplace();
        return;
    }

    // 有关键词 → 走服务端 API 搜索（榜单只有热门 ~30-50 条，搜非热门 slug 找不到）
    if (_marketSearchTimer) clearTimeout(_marketSearchTimer);
    _marketSearchTimer = setTimeout(() => _runMarketSearch(kw), 300);
}

async function _runMarketSearch(kw) {
    // 用 setTimeout 句柄判断，避免极短间隔的竞态
    if (_marketSearchTimer === null) return;
    _marketState.searchLoading = true;
    _marketState.searchError = null;
    renderMarketplace();
    try {
        const data = await api.fetch('/api/skills/marketplace/search?keyword=' + encodeURIComponent(kw));
        if (!data || data.error === 'unauthorized') throw new Error('认证失效，请重新登录');
        _marketState.searchResults = (data && data.skills) ? data.skills : [];
    } catch (e) {
        _marketState.searchResults = [];
        _marketState.searchError = String(e.message || e);
    } finally {
        _marketState.searchLoading = false;
        _marketState.page = 1;
        renderMarketplace();
    }
}

async function loadMarketplace(force = false) {
    const area = document.getElementById('skill-marketplace-area');
    if (_marketState.loaded && !force) {
        renderMarketplace();
        return;
    }
    if (_marketState.loading) return;
    _marketState.loading = true;

    if (area) {
        area.innerHTML = `<div class="marketplace-grid marketplace-skeleton">${
            Array.from({ length: 8 }).map(() => `<div class="mk-card mk-skeleton"></div>`).join('')
        }</div>`;
    }
    try {
        const data = await api.fetch('/api/skills/marketplace/rankings' + (force ? '?force=true' : ''));
        if (!data || data.error === 'unauthorized') throw new Error('认证失效，请重新登录');
        _marketState.sections = data.sections || {};
        _marketState.loaded = true;
        _marketState.page = 1;
        renderMarketplace();
    } catch (e) {
        if (area) {
            area.innerHTML = `<div class="alert alert-error">市场加载失败: ${escapeHtml(String(e.message || e))}` +
                `<br><small style="color:#999;">请确认 skillhub CLI 已正确安装且可访问 api.skillhub.cn。</small></div>`;
        }
    } finally {
        _marketState.loading = false;
    }
}
window.loadMarketplace = loadMarketplace;
window.switchMarketTab = switchMarketTab;
window.onMarketSearchInput = onMarketSearchInput;

// 全局筛选：需要 / 无需 API Key（跨标签与搜索保持生效，仅在重渲染时回到第 1 页）
function setMarketApiFilter(v) {
    if (!['all', 'required', 'none'].includes(v)) v = 'all';
    _marketState.apiKey = v;
    _marketState.page = 1;
    const wrap = document.getElementById('marketplace-apifilter');
    if (wrap) wrap.querySelectorAll('.mk-filter-btn').forEach(b => b.classList.toggle('active', b.dataset.api === v));
    renderMarketplace();
}
window.setMarketApiFilter = setMarketApiFilter;

// 窗口尺寸变化（旋转/缩放）时重算列数→每页条数，回到第 1 页重新渲染
let _marketResizeTimer = null;
window.addEventListener('resize', () => {
    if (!_marketState.sections) return;
    if (currentPage !== 'skills') return;
    clearTimeout(_marketResizeTimer);
    _marketResizeTimer = setTimeout(() => {
        _marketState.page = 1;
        renderMarketplace();
    }, 200);
});

async function installFromMarketplace(slug) {
    if (!slug) return;
    const area = document.getElementById('skill-marketplace-area');
    let card = null;
    if (area) {
        area.querySelectorAll('.mk-card').forEach(c => { if (c.dataset.slug === slug) card = c; });
        const btn = card ? card.querySelector('button') : null;
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 安装中...'; }
    }
    try {
        await api.post('/api/skills/marketplace/install', { slug });
        showToast(`技能 ${slug} 安装成功`, 'success');
        _installedSkillNames.add(slug);
        loadSkillsPage();      // 刷新已安装列表
        renderMarketplace();  // 刷新市场卡片状态（本地即时更新，不重新请求）
    } catch (e) {
        showToast(`安装失败: ${e.message || e}`, 'error');
        renderMarketplace();  // 还原按钮
    }
}
window.installFromMarketplace = installFromMarketplace;

// 窗口尺寸变化时重新定位滑动指示器
window.addEventListener('resize', () => {
    if (document.getElementById('market-tab-indicator')) _moveMarketIndicator();
});

// 兼容旧调用点
function loadMarketplacePopular() { loadMarketplace(); }

async function loadSkillsPage() {
    const container = document.getElementById('skills-content');
    if (!container) return;

    // 同步安装技能平台选择器到当前全局平台
    _syncSkillPlatformSelector();

    // 加载态
    container.innerHTML = `
        <div class="empty-state">
            <i class="fa-solid fa-spinner fa-spin" style="font-size:32px;color:#ccc;"></i>
            <p style="margin-top:8px;color:#999;">加载技能列表...</p>
        </div>`;

    try {
        // 平台筛选由 api._withPlatform() 自动追加 ?platform=（来自全局 store）
        const data = await api.fetch('/api/skills');

        // 防御：api.fetch 可能返回 null（网络异常）或 {error:'unauthorized'}
        if (!data || data.error === 'unauthorized' || data.status === 401) {
            container.innerHTML = `
                <div class="empty-state">
                    <i class="fa-solid fa-triangle-exclamation" style="font-size:32px;color:#f59e0b;"></i>
                    <p style="margin-top:8px;color:#999;">认证失效或未登录</p>
                    <button class="btn btn-sm btn-primary" onclick="showLoginOverlay()" style="margin-top:8px;">
                        <i class="fa-solid fa-rotate"></i> 重新登录
                    </button>
                </div>`;
            return;
        }

        const skills = data.skills || [];
        _installedSkillNames.clear();

        if (skills.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <i class="fa-solid fa-puzzle-piece" style="font-size:48px;color:#ccc;"></i>
                    <p style="margin-top:12px;color:#999;">暂未安装任何技能</p>
                    <p style="color:#bbb;font-size:13px;">使用下方安装表单添加技能，或从 SkillHub 市场一键安装</p>
                </div>`;
        } else {
            const badges = { disposition: '处置意向', action: '动作意向' };
            const rows = skills.map((s, i) => {
                const kwTags = (s.intent_keywords || []).slice(0, 8).map(k =>
                    `<span class="pill pill-intent" style="background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;">${escapeHtml(k)}</span>`
                ).join(' ') || '<span style="color:#ccc;">-</span>';

                const tools = (s.allowed_tools || []).length > 0
                    ? `<span class="skill-tools-wrap">${(s.allowed_tools || []).map(t => `<code style="font-size:11px;background:#fafafa;padding:2px 6px;border-radius:3px;">${escapeHtml(t)}</code>`).join('')}</span>`
                    : '<span style="color:#ccc;">无限制</span>';

                const pct = Math.round((s.weight || 0) * 100);
                const disabledDim = s.enabled === false ? 'opacity:0.55;filter:grayscale(0.6);' : '';

                // 配置按钮：仅当技能存在 config.yaml 时显示（Task 2）
                const configBtn = s.has_config
                    ? `<button class="btn btn-sm btn-outline" onclick="openSkillConfig('${escapeHtml(s.name)}')">配置</button>`
                    : '';

                // 关键词数组序列化为 HTML 属性安全的字符串（转义 & 和 "，避免提前闭合 onclick）
                const kwJson = JSON.stringify(s.intent_keywords || [])
                    .replace(/&/g, '&amp;')
                    .replace(/"/g, '&quot;');

                // 平台标签 + 公开/专有切换
                const platforms = s.platforms || [];
                const platformToggle = platforms.length > 0
                    ? `<span class="platform-badge platform-badge-${escapeHtml(platforms[0])}">${escapeHtml(_platformLabel(platforms[0]))}</span>
                       <button class="btn btn-xs platform-action-btn" onclick="setSkillPublic('${escapeHtml(s.name)}')" title="设为通用技能，所有平台可见">设为公开</button>`
                    : `<span class="platform-badge platform-badge-common">通用</span>
                       <div class="platform-toggle-wrap">
                         <button class="btn btn-xs platform-action-btn" onclick="togglePlatformDropdown(event, '${escapeHtml(s.name)}')">设为专有 ▾</button>
                         <div class="platform-dropdown" id="pdd-${escapeHtml(s.name)}" style="display:none;">
                           <div class="platform-dropdown-item" onclick="setSkillPlatform('${escapeHtml(s.name)}', 'dingtalk')"><i class="fa-solid fa-message" style="margin-right:4px;color:#1677ff;"></i>钉钉专属</div>
                           <div class="platform-dropdown-item" onclick="setSkillPlatform('${escapeHtml(s.name)}', 'feishu')"><i class="fa-solid fa-feather" style="margin-right:4px;color:#3370ff;"></i>飞书专属</div>
                           <div class="platform-dropdown-item" onclick="setSkillPlatform('${escapeHtml(s.name)}', 'wecom')"><i class="fa-solid fa-building" style="margin-right:4px;color:#8b5cf6;"></i>企微专属</div>
                         </div>
                       </div>`;

                return `
                <div class="skill-card ${s.error ? 'skill-error' : ''}" style="border:1px solid #e8e8e8;border-radius:8px;padding:16px;margin-bottom:12px;overflow:hidden;${disabledDim}${s.error ? 'opacity:0.6;' : ''}">
                    <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:10px;">
                        <div>
                            <strong class="skill-name" style="font-size:16px;">${escapeHtml(s.name)}</strong>
                            ${platformToggle}
                            ${s.error ? '<span class="pill" style="background:#fff2f0;color:#ef4444;margin-left:8px;">加载失败</span>' : ''}
                        </div>
                        <div style="display:flex;align-items:center;gap:6px;">
                            <label title="${s.enabled !== false ? '已启用，点击禁用' : '已禁用，点击启用'}" style="display:flex;align-items:center;cursor:pointer;">
                                <input type="checkbox" ${s.enabled !== false ? 'checked' : ''} onchange="toggleSkillEnabled('${escapeHtml(s.name)}', this.checked)" style="display:none;">
                                <span style="display:inline-block;width:40px;height:22px;background:${s.enabled !== false ? '#2563eb' : '#cbd5e1'};border-radius:11px;position:relative;transition:background 0.2s;">
                                    <span style="display:inline-block;width:18px;height:18px;background:#fff;border-radius:50%;position:absolute;top:2px;left:${s.enabled !== false ? '20px' : '2px'};transition:left 0.2s;box-shadow:0 1px 3px rgba(0,0,0,0.2);"></span>
                                </span>
                            </label>
                            <button class="btn btn-sm btn-outline" onclick="openSkillIntent('${escapeHtml(s.name)}', ${kwJson})" title="手动维护意图关键词"><i class="fa-solid fa-tags"></i> 意图词</button>
                            <button class="btn btn-sm btn-outline" onclick="openSkillAiIntent('${escapeHtml(s.name)}')" title="AI 分析 SKILL.md 并生成意图词，展示生成过程"><i class="fa-solid fa-wand-magic-sparkles"></i> AI生成意图词</button>
                            ${configBtn}
                            <button class="btn btn-sm btn-outline" onclick="uninstallSkill('${escapeHtml(s.name)}')" style="color:#ef4444;border-color:#ef4444;">卸载</button>
                        </div>
                    </div>
                    <p style="color:#666;margin:0 0 10px 0;font-size:13px;">${escapeHtml(s.description)}</p>
                    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:12px;">
                        <span style="color:#999;white-space:nowrap;">权重：</span>
                        <input type="range" min="0" max="100" value="${pct}" oninput="updateSkillWeight('${escapeHtml(s.name)}', this.value/100, this)" 
                            style="flex:1;max-width:120px;height:6px;accent-color:#2563eb;cursor:pointer;">
                        <span class="skill-weight-val" style="min-width:36px;text-align:right;font-weight:600;color:#16a34a;">${pct}%</span>
                    </div>
                    <div style="display:grid;grid-template-columns:80px minmax(0,1fr);gap:6px 8px;font-size:12px;">
                        <span style="color:#999;">意图词：</span><span style="min-width:0;overflow-wrap:break-word;word-break:break-all;">${kwTags}</span>
                        <span style="color:#999;">工具：</span><span style="min-width:0;overflow-wrap:break-word;">${tools}</span>
                        <span style="color:#999;">路径：</span><code class="skill-path-code" style="font-size:11px;">${escapeHtml(s.source_path)}</code>
                    </div>
                </div>`;
            }).join('');
            container.innerHTML = rows;
        }
    } catch (e) {
        console.error('[skills] loadSkillsPage error:', e);
        container.innerHTML = `
            <div class="alert alert-error" style="margin:12px;">
                <strong>加载技能列表失败</strong>
                <p style="margin:4px 0 0 0;color:#666;font-size:12px;">${escapeHtml(e.message || String(e))}</p>
                <button class="btn btn-sm btn-outline-secondary" onclick="loadSkillsPage()" style="margin-top:8px;">
                    <i class="fa-solid fa-rotate"></i> 重试
                </button>
            </div>`;
    }
}

async function installSkill() {
    const input = document.getElementById('skill-repo-input');
    const repo = input?.value.trim();
    if (!repo) return;

    const platSelect = document.getElementById('skill-platform-select');
    const platform = platSelect ? platSelect.value : '';

    const status = document.getElementById('skill-install-status');
    const btn = document.getElementById('skill-install-btn');

    if (status) status.innerHTML = '<span style="color:#2563eb;">安装中...</span>';
    if (btn) btn.disabled = true;

    try {
        const res = await api.post('/api/skills/install', { repo, platform });
        if (status) status.innerHTML = `<span style="color:#16a34a;">${escapeHtml(res.message)} (已加载 ${res.loaded_count} 个技能)</span>`;
        if (input) input.value = '';
        loadSkillsPage();
    } catch (e) {
        if (status) status.innerHTML = `<span style="color:#ef4444;">安装失败: ${escapeHtml(e.message || e)}</span>`;
    } finally {
        if (btn) btn.disabled = false;
    }
}
window.installSkill = installSkill;

// 技能权重滑块回调（防抖：停止拖动 300ms 后写 API）
let _weightTimers = {};
function updateSkillWeight(name, val, el) {
    if (el) {
        const span = el.parentNode.querySelector('.skill-weight-val');
        if (span) span.textContent = Math.round(val * 100) + '%';
    }
    const key = 'w_' + name;
    clearTimeout(_weightTimers[key]);
    _weightTimers[key] = setTimeout(async () => {
        try {
            await api.fetch(`/api/skills/${encodeURIComponent(name)}`, 'PUT', { weight: val });
        } catch (e) {
            console.error('更新权重失败:', e);
            if (el) el.value = Math.round(el.dataset?.oldWeight || 50);
        }
    }, 300);
}
window.updateSkillWeight = updateSkillWeight;

// 技能开关回调
async function toggleSkillEnabled(name, enabled) {
    try {
        await api.fetch(`/api/skills/${encodeURIComponent(name)}`, 'PUT', { enabled });
        loadSkillsPage();
    } catch (e) {
        alert('切换失败: ' + (e.message || e));
        loadSkillsPage();
    }
}
window.toggleSkillEnabled = toggleSkillEnabled;

async function uninstallSkill(name) {
    if (!confirm(`确定要卸载技能 "${name}" 吗？`)) return;

    try {
        await api.del(`/api/skills/${encodeURIComponent(name)}`);
        loadSkillsPage();
    } catch (e) {
        alert(`卸载失败: ${e.message || e}`);
    }
}
window.uninstallSkill = uninstallSkill;

// ============ 定期检测更新 + 立即更新 ============

let _skillsAutoTimer = null;

// 自动检测更新：每 30s 刷新技能列表以反映后端热加载的变动；编辑弹窗打开时不刷新，避免打断
async function _skillsAutoTick() {
    if (currentPage !== 'skills') return;
    if (document.querySelector('.modal.active')) return;
    try {
        await loadSkillsPage();
        const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
        const st = document.getElementById('skill-update-status');
        if (st) st.innerHTML = `<i class="fa-solid fa-check-circle" style="color:#16a34a;"></i> 已是最新 · 上次检测 ${t}`;
    } catch (_) { /* 忽略轮询错误 */ }
}

function startSkillsAutoCheck() {
    stopSkillsAutoCheck();
    _skillsAutoTimer = setInterval(_skillsAutoTick, 30000);
    _refreshSkillWatcherStatus();
}

function stopSkillsAutoCheck() {
    if (_skillsAutoTimer) { clearInterval(_skillsAutoTimer); _skillsAutoTimer = null; }
}
window.startSkillsAutoCheck = startSkillsAutoCheck;
window.stopSkillsAutoCheck = stopSkillsAutoCheck;

// 拉取热加载 watcher 状态，展示「自动检测」是否开启
async function _refreshSkillWatcherStatus() {
    const st = document.getElementById('skill-update-status');
    if (!st) return;
    try {
        const info = await api.fetch('/api/skills/watcher');
        const on = !!(info && info.running);
        const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
        const dot = on ? '#16a34a' : '#f59e0b';
        const label = on ? `自动检测开启（每 ${info.poll_interval || '?'}s）` : '自动检测未开启';
        st.innerHTML = `<i class="fa-solid fa-circle" style="color:${dot};"></i> ${label} · 上次检测 ${t}`;
    } catch (_) {
        st.innerHTML = `<i class="fa-solid fa-circle-xmark" style="color:#ef4444;"></i> 检测状态获取失败`;
    }
}
window._refreshSkillWatcherStatus = _refreshSkillWatcherStatus;

async function reloadSkills() {
    const btn = document.getElementById('skill-reload-btn');
    const st = document.getElementById('skill-update-status');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 更新中…'; }
    if (st) st.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> 正在检测更新…';
    try {
        const res = await api.post('/api/skills/reload', {});
        showToast(`已更新 ${res.loaded_count} 个技能`);
        await loadSkillsPage();
        const t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
        if (st) st.innerHTML = `<i class="fa-solid fa-check-circle" style="color:#16a34a;"></i> 已更新 ${res.loaded_count} 个技能 · ${t}`;
    } catch (e) {
        if (st) st.innerHTML = `<i class="fa-solid fa-circle-xmark" style="color:#ef4444;"></i> 更新失败`;
        showToast(`更新失败: ${e.message || e}`, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-rotate"></i> 立即更新'; }
    }
}
window.reloadSkills = reloadSkills;


// ============ 技能配置编辑 ============

async function openSkillConfig(name) {
    if (!name) return;
    document.getElementById('skill-config-name').value = name;
    document.getElementById('skill-config-title').textContent = `技能配置 — ${name}`;
    document.getElementById('skill-config-editor').value = '# 加载中...';
    document.getElementById('skill-config-modal').classList.add('active');

    try {
        const data = await api.fetch(`/api/skills/${encodeURIComponent(name)}/config`);
        if (data.has_config && data.raw_yaml) {
            document.getElementById('skill-config-editor').value = data.raw_yaml;
        } else {
            document.getElementById('skill-config-editor').value = '# 该技能暂无 config.yaml\n# 输入 YAML 格式的配置参数后保存即可创建\n';
        }
    } catch (e) {
        document.getElementById('skill-config-editor').value = `# 加载失败: ${e.message || e}\n# 可手动输入 YAML 参数后保存创建\n`;
    }
}
window.openSkillConfig = openSkillConfig;

function closeSkillConfigModal() {
    document.getElementById('skill-config-modal').classList.remove('active');
}
window.closeSkillConfigModal = closeSkillConfigModal;

async function saveSkillConfig() {
    const name = document.getElementById('skill-config-name').value;
    const rawYaml = document.getElementById('skill-config-editor').value;
    const btn = document.getElementById('skill-config-save-btn');

    if (!name || !rawYaml.trim()) {
        showToast('配置内容不能为空', 'error');
        return;
    }

    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 保存中...';

    try {
        await api.fetch(`/api/skills/${encodeURIComponent(name)}/config`, 'PUT', { raw_yaml: rawYaml });
        showToast(`技能 ${name} 配置已保存并重载`, 'success');
        closeSkillConfigModal();
        loadSkillsPage();
    } catch (e) {
        showToast(`保存失败: ${e.message || e}`, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '保存并重载';
    }
}
window.saveSkillConfig = saveSkillConfig;


// ============ 技能意图词手动维护 ============

// 打开意图词编辑弹窗，预填当前关键词
function openSkillIntent(name, keywords) {
    if (!name) return;
    document.getElementById('skill-intent-name').value = name;
    document.getElementById('skill-intent-title').textContent = `意图关键词 — ${name}`;

    // 渲染已有关键词为可删除的 chip
    const wrap = document.getElementById('skill-intent-chips');
    const list = Array.isArray(keywords) ? keywords : [];
    renderSkillIntentChips(list);
    document.getElementById('skill-intent-input').value = '';
    document.getElementById('skill-intent-modal').classList.add('active');
    setTimeout(() => document.getElementById('skill-intent-input').focus(), 50);
}
window.openSkillIntent = openSkillIntent;

function renderSkillIntentChips(list) {
    const wrap = document.getElementById('skill-intent-chips');
    if (!wrap) return;
    if (!list.length) {
        wrap.innerHTML = '<span style="color:#bbb;font-size:12px;">（暂无关键词，下方输入后回车添加）</span>';
        return;
    }
        wrap.innerHTML = list.map(k =>
        `<span class="pill" style="background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;display:inline-flex;align-items:center;gap:4px;">
            ${escapeHtml(k)}
            <button type="button" onclick="removeSkillIntentKeyword(this)" data-kw="${escapeHtml(k)}" style="border:none;background:none;color:#2563eb;cursor:pointer;padding:0;line-height:1;font-size:13px;">&times;</button>
        </span>`
    ).join(' ');
}

// 读取当前 chips 中的关键词数组
function getSkillIntentList() {
    const wrap = document.getElementById('skill-intent-chips');
    if (!wrap) return [];
    return Array.from(wrap.querySelectorAll('button[data-kw]')).map(b => b.getAttribute('data-kw'));
}

function addSkillIntentKeyword() {
    const input = document.getElementById('skill-intent-input');
    const raw = (input.value || '').trim();
    if (!raw) return;
    // 支持逗号/空格/换行分隔批量输入
    const parts = raw.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
    const list = getSkillIntentList();
    const seen = new Set(list);
    let added = 0;
    for (const p of parts) {
        if (!seen.has(p)) {
            seen.add(p);
            list.push(p);
            added++;
        }
    }
    if (added) renderSkillIntentChips(list);
    input.value = '';
    input.focus();
}
window.addSkillIntentKeyword = addSkillIntentKeyword;

function removeSkillIntentKeyword(btn) {
    const kw = btn.getAttribute('data-kw');
    const list = getSkillIntentList().filter(k => k !== kw);
    renderSkillIntentChips(list);
}
window.removeSkillIntentKeyword = removeSkillIntentKeyword;

function closeSkillIntentModal() {
    document.getElementById('skill-intent-modal').classList.remove('active');
}
window.closeSkillIntentModal = closeSkillIntentModal;

async function saveSkillIntent() {
    const name = document.getElementById('skill-intent-name').value;
    const keywords = getSkillIntentList();
    const btn = document.getElementById('skill-intent-save-btn');
    if (!name) return;

    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 保存中...';
    try {
        await api.fetch(`/api/skills/${encodeURIComponent(name)}`, 'PUT', { intent_keywords: keywords });
        showToast(`技能 ${name} 意图关键词已保存并重载`, 'success');
        closeSkillIntentModal();
        loadSkillsPage();
    } catch (e) {
        showToast(`保存失败: ${e.message || e}`, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '保存并重载';
    }
}
window.saveSkillIntent = saveSkillIntent;


// ============ AI 生成意图词（带过程可视化） ============

let _aiIntentName = '';

async function openSkillAiIntent(name) {
    if (!name) return;
    _aiIntentName = name;
    const modal = document.getElementById('skill-ai-intent-modal');
    document.getElementById('skill-ai-intent-title').textContent = `AI 生成意图词 — ${name}`;
    const body = document.getElementById('skill-ai-intent-body');
    body.innerHTML = `
        <div class="ai-intent-loading">
            <i class="fa-solid fa-spinner fa-spin"></i>
            <span>AI 正在分析技能 <b>${escapeHtml(name)}</b> 的 SKILL.md…</span>
        </div>`;
    const rerun = document.getElementById('skill-ai-intent-rerun');
    if (rerun) rerun.style.display = 'none';
    modal.classList.add('active');
    await runSkillAiIntent(false);
}
window.openSkillAiIntent = openSkillAiIntent;

// 执行生成（force=true 时覆盖已有意图词）
async function runSkillAiIntent(force) {
    const name = _aiIntentName;
    if (!name) return;
    const body = document.getElementById('skill-ai-intent-body');
    const rerun = document.getElementById('skill-ai-intent-rerun');
    if (rerun) rerun.style.display = 'none';
    // loading 态叠加（保留上一次结果时仅显示遮罩）
    body.classList.add('ai-intent-busy');
    try {
        const data = await api.post('/api/skills/generate-intent-trace', { name, force: !!force });
        if (!data || data.error || data.detail) {
            const msg = (data && (data.detail || data.error)) || '未知错误';
            body.classList.remove('ai-intent-busy');
            body.innerHTML = `<div class="alert alert-error">生成失败：${escapeHtml(String(msg))}</div>`;
            return;
        }
        renderSkillAiIntentTrace(data);
        if (rerun) rerun.style.display = '';
    } catch (e) {
        body.classList.remove('ai-intent-busy');
        body.innerHTML = `<div class="alert alert-error">生成异常：${escapeHtml(e.message || String(e))}</div>`;
    } finally {
        body.classList.remove('ai-intent-busy');
    }
}
window.runSkillAiIntent = runSkillAiIntent;

// 当前 AI 意图词 trace 数据（供追加/覆盖编辑器使用）
let _aiIntentTraceData = null;

function renderSkillAiIntentTrace(data) {
    const body = document.getElementById('skill-ai-intent-body');
    const trace = data.trace || {};
    const result = data.result || trace.result || null;
    const msgs = trace.messages || [];
    const systemMsg = msgs.find(m => m.role === 'system');
    const userMsg = msgs.find(m => m.role === 'user');
    const raw = trace.raw_response || '';
    const skipped = trace.skipped;
    const error = trace.error;
    const written = data.written;
    // 缓存供编辑器使用
    _aiIntentTraceData = { name: _aiIntentName, result, raw, systemMsg, userMsg };

    const kwChips = (arr) => (arr && arr.length)
        ? arr.map(k => `<span class="pill" style="background:#eff6ff;color:#2563eb;border:1px solid #bfdbfe;">${escapeHtml(k)}</span>`).join(' ')
        : '<span style="color:#ccc;">-</span>';
    const catChips = (arr) => (arr && arr.length)
        ? arr.map(c => `<span class="pill" style="background:#ecfeff;color:#06b6d4;border:1px solid #a5f3fc;">${escapeHtml(c)}</span>`).join(' ')
        : '<span style="color:#ccc;">（未匹配到领域类别）</span>';

    let statusHtml;
    if (skipped) {
        statusHtml = `<span class="ai-step-badge warn"><i class="fa-solid fa-circle-info"></i> 跳过：技能已存在意图词（未启用覆盖）</span>`;
    } else if (error) {
        statusHtml = `<span class="ai-step-badge err"><i class="fa-solid fa-circle-xmark"></i> 未生成：${escapeHtml(error)}</span>`;
    } else if (written) {
        statusHtml = `<span class="ai-step-badge ok"><i class="fa-solid fa-circle-check"></i> 已写入 SKILL.md 并刷新语义缓存</span>`;
    } else {
        statusHtml = `<span class="ai-step-badge warn"><i class="fa-solid fa-circle-info"></i> 已生成但未写回</span>`;
    }

    // 追加/覆盖操作按钮（仅在成功有结果时显示）
    const hasResult = result && !skipped && !error;
    const actionBtns = hasResult ? `
            <div style="margin-top:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <button class="btn btn-sm" onclick="showAiPromptEditor('append')" style="background:#e6f4ff;color:#0958d9;border:1px solid #91caff;">
                    <i class="fa-solid fa-plus"></i> 追加提示词
                </button>
                <button class="btn btn-sm" onclick="showAiPromptEditor('overwrite')" style="background:#fff7e6;color:#d46b08;border:1px solid #ffd591;">
                    <i class="fa-solid fa-pen-to-square"></i> 覆盖提示词
                </button>
                <span style="font-size:12px;color:#999;margin-left:4px;">对当前技能的 system_prompt 进行编辑操作</span>
            </div>
            <div id="ai-prompt-editor-wrap" style="display:none;"></div>` : '';

    body.innerHTML = `
        <div class="ai-intent-steps">
            <div class="ai-step">
                <div class="ai-step-head"><span class="ai-step-no">1</span> 发送给 AI 的提示词</div>
                ${systemMsg ? `<div class="ai-step-sub">System Prompt</div><pre class="ai-pre">${escapeHtml(systemMsg.content)}</pre>` : ''}
                ${userMsg ? `<div class="ai-step-sub">User Message（技能元信息 + 可选领域类别清单）</div><pre class="ai-pre">${escapeHtml(userMsg.content)}</pre>` : '<div class="ai-step-sub">（无 user 消息）</div>'}
            </div>
            <div class="ai-step">
                <div class="ai-step-head"><span class="ai-step-no">2</span> AI 原始返回</div>
                ${raw ? `<pre class="ai-pre ai-pre-raw">${escapeHtml(raw)}</pre>` : '<div class="ai-step-sub">（空）</div>'}
            </div>
            <div class="ai-step">
                <div class="ai-step-head"><span class="ai-step-no">3</span> 解析结果</div>
                <div class="ai-step-sub">匹配的领域类别（intent_categories）</div>
                <div style="margin:4px 0 10px;">${catChips(result ? result.intent_categories : [])}</div>
                <div class="ai-step-sub">统一意图词（domain 展开 + 自由触发词合并，intent_keywords）</div>
                <div style="margin:4px 0;">${kwChips(result ? result.intent_keywords : [])}</div>
            </div>
            <div class="ai-step">
                <div class="ai-step-head"><span class="ai-step-no">4</span> 写回状态</div>
                <div style="margin-top:6px;">${statusHtml}</div>
                ${actionBtns}
            </div>
        </div>`;
}
window.renderSkillAiIntentTrace = renderSkillAiIntentTrace;

// ── 提示词编辑器：追加 / 覆盖 ──

/**
 * 显示提示词编辑器面板。
 * mode='append'  → 预填当前 system_prompt，用户在末尾追加内容后提交合并；
 * mode='overwrite'→ 预填 AI 分析得出的建议 prompt，提交后整体替换。
 */
async function showAiPromptEditor(mode) {
    const wrap = document.getElementById('ai-prompt-editor-wrap');
    if (!wrap || !_aiIntentTraceData) return;

    const skillName = _aiIntentTraceData.name;

    // 先拉取当前技能的 system_prompt
    let currentPrompt = '';
    try {
        const skillData = await api.fetch(`/api/skills/${encodeURIComponent(skillName)}`);
        currentPrompt = (skillData && skillData.system_prompt) || '';
    } catch (_) {
        // 拉取失败时用空字符串
    }

    // 根据模式决定初始值与标签
    let initialValue, labelTitle, labelHint, confirmText, confirmIcon, btnStyle;
    if (mode === 'append') {
        initialValue = '';
        labelTitle = '<i class="fa-solid fa-plus"></i> 追加提示词内容';
        labelHint = `以下内容将<strong>追加</strong>到当前 system_prompt 末尾（当前长度 ${currentPrompt.length} 字符）。留空则不操作。`;
        confirmText = '确认追加';
        confirmIcon = 'fa-plus';
        btnStyle = 'btn-primary';
    } else {
        // overwrite: 用 AI 原始返回中提取的推荐内容作为初始值（或用解析后的关键词作为建议 prompt）
        const suggested = buildSuggestedPrompt(_aiIntentTraceData);
        initialValue = suggested;
        labelTitle = '<i class="fa-solid fa-pen-to-square"></i> 覆盖提示词内容';
        labelHint = `以下内容将<strong>完全替换</strong>当前 system_prompt（原 ${currentPrompt.length} 字符将被覆盖）。请审阅后确认。`;
        confirmText = '确认覆盖';
        confirmIcon = 'fa-pen-to-square';
        btnStyle = 'btn-warning'; // 用 warning 色提醒这是破坏性操作
    }

    wrap.style.display = '';
    wrap.innerHTML = `
        <div class="ai-prompt-editor">
            <div class="ai-prompt-editor-header">
                <span>${labelTitle}</span>
                <button class="btn btn-sm btn-outline" onclick="hideAiPromptEditor()" style="padding:2px 8px;font-size:11px;"><i class="fa-solid fa-xmark"></i></button>
            </div>
            <div style="padding:6px 12px;font-size:12px;color:var(--text-tertiary);">${labelHint}</div>
            <textarea id="ai-prompt-editor-text" placeholder="在此输入提示词内容...">${escapeHtml(initialValue)}</textarea>
            <div class="ai-prompt-editor-footer">
                <button class="btn btn-sm btn-outline" onclick="hideAiPromptEditor()">取消</button>
                <button class="btn btn-sm ${btnStyle}" id="ai-prompt-confirm-btn" onclick="confirmAiPromptEditor('${mode}')">
                    <i class="fa-solid ${confirmIcon}"></i> ${confirmText}
                </button>
            </div>
        </div>`;

    // 滚动到编辑器位置
    wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
window.showAiPromptEditor = showAiPromptEditor;

function hideAiPromptEditor() {
    const wrap = document.getElementById('ai-prompt-editor-wrap');
    if (wrap) { wrap.style.display = 'none'; wrap.innerHTML = ''; }
}
window.hideAiPromptEditor = hideAiPromptEditor;

/** 根据trace数据构建一个推荐的prompt文本 */
function buildSuggestedPrompt(traceData) {
    const r = traceData.result || {};
    const cats = (r.intent_categories || []).join(', ');
    const kws = (r.intent_keywords || []).join(', ');
    return [
        `# 技能指令（AI 生成建议）`,
        ``,
        `## 匹配领域`,
        cats || '（无）',
        ``,
        `## 触发关键词`,
        kws || '（无）',
        ``,
        `## 说明`,
        `以上内容由 AI 根据 SKILL.md 分析生成。请根据实际需求修改后再确认覆盖。`,
    ].join('\n');
}

/** 执行追加或覆盖操作 */
async function confirmAiPromptEditor(mode) {
    const name = _aiIntentTraceData ? _aiIntentTraceData.name : '';
    if (!name) return;

    const textarea = document.getElementById('ai-prompt-editor-text');
    const btn = document.getElementById('ai-prompt-confirm-btn');
    const newText = (textarea && textarea.value.trim()) || '';

    if (mode === 'overwrite' && !newText) {
        showToast('覆盖内容不能为空', 'error'); return;
    }

    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 提交中...'; }

    try {
        if (mode === 'append') {
            // 追加模式：先拿现有 prompt，拼接新内容
            if (!newText) { showToast('追加内容为空，已取消', 'info'); hideAiPromptEditor(); return; }
            const skillData = await api.fetch(`/api/skills/${encodeURIComponent(name)}`);
            const existing = (skillData && skillData.system_prompt) || '';
            const combined = existing ? (existing.trimEnd() + '\n\n' + newText) : newText;
            await api.fetch(`/api/skills/${encodeURIComponent(name)}`, 'PUT', { system_prompt: combined });
            showToast(`已追加提示词（原 ${existing.length} → 现 ${combined.length} 字符）`, 'success');
        } else {
            // 覆盖模式
            await api.fetch(`/api/skills/${encodeURIComponent(name)}`, 'PUT', { system_prompt: newText });
            showToast(`已覆盖提示词（${newText.length} 字符）`, 'success');
        }
        hideAiPromptEditor();
        loadSkillsPage(); // 刷新列表以反映变化
    } catch (e) {
        showToast(`操作失败: ${e.message || e}`, 'error');
    } finally {
        if (btn) { btn.disabled = false;
            btn.innerHTML = mode === 'append'
                ? '<i class="fa-solid fa-plus"></i> 确认追加'
                : '<i class="fa-solid fa-pen-to-square"></i> 确认覆盖';
        }
    }
}
window.confirmAiPromptEditor = confirmAiPromptEditor;

function closeSkillAiIntentModal() {
    document.getElementById('skill-ai-intent-modal').classList.remove('active');
}
window.closeSkillAiIntentModal = closeSkillAiIntentModal;



