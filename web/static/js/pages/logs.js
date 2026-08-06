// 运行日志页（/logs）：独立的实时日志视图。
// 复用 dashboard 实时日志面板的 /api/logs 端点与 .rt-log-* 渲染样式，
// 但拥有独立的轮询循环与过滤能力（关键词搜索 / 仅显示审计 [audit]）。
// 与仪表盘 realtime-log 面板互不干扰：各自仅在本页激活时拉取。

let _logsLastId = 0;
let _logsPolling = null;
let _logsAutoScroll = true;

function renderLogsLine(l) {
    const raw = escapeHtml(String(l.level || 'INFO').toUpperCase());
    const cls = 'rt-level-' + raw.toLowerCase();
    const ts = escapeHtml((l.ts || '').slice(-8));        // 仅显示 HH:MM:SS
    const fullTs = escapeHtml(l.ts || '');
    let logger = escapeHtml(l.logger || '-').replace(/^src\./, '');  // 去掉 src. 前缀
    const msg = escapeHtml(l.message || '');
    const isAudit = msg.includes('[audit]');
    const extra = isAudit ? ' rt-audit' : '';
    return `
        <div class="rt-log-line ${cls}${extra}" title="${fullTs} · [${raw}] · ${logger}">
            <span class="rt-log-ts">${ts}</span>
            <span class="rt-log-level">${raw}</span>
            <span class="rt-log-logger">${logger}:</span>
            <span class="rt-log-msg">${msg}</span>
        </div>`;
}

function _logsApplyFilters(logs) {
    const qEl = document.getElementById('logs-search');
    const q = (qEl && qEl.value || '').trim().toLowerCase();
    const auditEl = document.getElementById('logs-audit-only');
    const auditOnly = !!(auditEl && auditEl.checked);
    return logs.filter(l => {
        const text = (l.message || '') + ' ' + (l.logger || '');
        if (auditOnly && !(l.message || '').includes('[audit]')) return false;
        if (q && !text.toLowerCase().includes(q)) return false;
        return true;
    });
}

function getLogsPlatform() {
    const sel = document.getElementById('logs-platform');
    return sel ? sel.value : 'all';
}

async function pollLogsPage() {
    const stream = document.getElementById('logs-stream');
    if (!stream) return;
    const levelSel = document.getElementById('logs-level-select');
    const level = levelSel ? levelSel.value : 'info';
    const platform = getLogsPlatform();
    try {
        // 显式带 platform=，使 api._withPlatform 不再追加全局平台（避免双重参数/覆盖本页选择）
        const data = await api.fetch(`/api/logs?level=${encodeURIComponent(level)}&since=${_logsLastId}&limit=300&platform=${encodeURIComponent(platform)}`);
        const logs = (data && data.logs) || [];
        // 缓冲区重置（后端重启 / wrap）检测：返回的最大 id 小于本地游标时，
        // 清空游标让下次拉全量，避免实时日志冻结在旧记录上。
        if (data && typeof data.max_id === 'number' && data.max_id > 0 && data.max_id < _logsLastId) {
            _logsLastId = 0;
        }
        // 清除初始连接骨架
        const skel = stream.querySelector('.rt-log-skeleton');
        if (skel) skel.remove();
        // 更新计数徽章（用缓冲区总量，非本次增量条数）
        const cnt = document.getElementById('logs-count');
        if (cnt && data) {
            cnt.textContent = (data.buffer_total != null ? data.buffer_total : (data.total || 0)) + ' 条';
        }
        if (!logs.length) {
            // 增量无新日志：保留已有历史，仅在容器真正为空时才显示占位
            if (stream.childElementCount === 0 && !stream.querySelector('.rt-log-empty')) {
                stream.innerHTML = '<div class="rt-log-empty"></div>';
            }
            return;
        }
        // 清除空状态占位
        const empty = stream.querySelector('.rt-log-empty');
        if (empty) stream.innerHTML = '';
        const filtered = _logsApplyFilters(logs);
        const atBottom = stream.scrollHeight - stream.scrollTop - stream.clientHeight < 48;
        const before = stream.childElementCount;
        if (filtered.length) {
            stream.insertAdjacentHTML('beforeend', filtered.map(renderLogsLine).join(''));
            const kids = stream.children;
            for (let i = before; i < kids.length; i++) {
                kids[i].classList.add('rt-new');
            }
        }
        // 限制 DOM 行数（保留最近 300 条，足够看上下文）
        while (stream.childElementCount > 300) {
            stream.removeChild(stream.firstChild);
        }
        if (_logsAutoScroll && atBottom) {
            stream.scrollTop = stream.scrollHeight;
        }
        if (logs.length) _logsLastId = logs[logs.length - 1].id;
    } catch (e) {
        // 静默失败：日志面板不应干扰其它功能
    }
}

function startLogsPolling() {
    if (_logsPolling) clearInterval(_logsPolling);
    _logsPolling = setInterval(() => {
        // 仅在日志页活跃时拉取，避免后台无谓请求
        if (currentPage !== 'logs') return;
        pollLogsPage();
    }, 2000);
}

function stopLogsPolling() {
    if (_logsPolling) {
        clearInterval(_logsPolling);
        _logsPolling = null;
    }
}

async function _populateLogsPlatformSelect() {
    const sel = document.getElementById('logs-platform');
    if (!sel) return;
    // 默认选中当前全局平台（通常为 dingtalk），其余平台追加为选项
    const cur = (window.store && typeof window.store.getPlatform === 'function')
        ? window.store.getPlatform() : 'dingtalk';
    try {
        const data = await api.fetch('/api/platforms');
        const platforms = (data && data.platforms) || [];
        // 清空除「全部平台」外的选项，避免重复切换页面时累积
        sel.innerHTML = '<option value="all">全部平台</option>';
        for (const p of platforms) {
            const opt = document.createElement('option');
            opt.value = p.id;
            opt.textContent = p.display_name || p.id;
            sel.appendChild(opt);
        }
        sel.value = cur;
    } catch (e) {
        // 失败则保留「全部平台」，不影响日志功能
        sel.value = 'all';
    }
}

function loadLogsPage() {
    const stream = document.getElementById('logs-stream');
    if (!stream) return;
    _logsLastId = 0;
    stream.innerHTML = '<div class="rt-log-skeleton"><div class="skeleton-log-row"><div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line short"></div><div class="skeleton skeleton-line"></div></div></div>';

    // 平台选项填充（异步，不阻塞首屏拉取）
    _populateLogsPlatformSelect();

    // 控件绑定（使用 on* 赋值幂等覆盖，避免重复切换页面时叠加多个 listener）
    const levelSel = document.getElementById('logs-level-select');
    if (levelSel) {
        levelSel.onchange = () => {
            _logsLastId = 0;
            const s = document.getElementById('logs-stream');
            if (s) s.innerHTML = '';
            pollLogsPage();
        };
    }
    const platSel = document.getElementById('logs-platform');
    if (platSel) {
        platSel.onchange = () => {
            _logsLastId = 0;
            const s = document.getElementById('logs-stream');
            if (s) s.innerHTML = '';
            pollLogsPage();
        };
    }
    const search = document.getElementById('logs-search');
    if (search) {
        search.oninput = () => {
            _logsLastId = 0;
            const s = document.getElementById('logs-stream');
            if (s) s.innerHTML = '';
            pollLogsPage();
        };
    }
    const auditOnly = document.getElementById('logs-audit-only');
    if (auditOnly) {
        auditOnly.onchange = () => {
            _logsLastId = 0;
            const s = document.getElementById('logs-stream');
            if (s) s.innerHTML = '';
            pollLogsPage();
        };
    }
    const auto = document.getElementById('logs-autoscroll');
    if (auto) {
        _logsAutoScroll = auto.checked;
        auto.onchange = (e) => { _logsAutoScroll = e.target.checked; };
    }
    const clear = document.getElementById('logs-clear-btn');
    if (clear) {
        clear.onclick = () => {
            const s = document.getElementById('logs-stream');
            if (s) s.innerHTML = '<div class="rt-log-empty"><span class="empty-icon">📜</span><span>已清空，等待新日志…</span></div>';
        };
    }
    pollLogsPage();
}

// 暴露给 app.js 的 switchPage 调用（与 window.loadPersonaPage 等模式一致）
window.loadLogsPage = loadLogsPage;
window.startLogsPolling = startLogsPolling;
window.stopLogsPolling = stopLogsPolling;
