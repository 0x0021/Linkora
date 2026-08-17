// ============ pages/models.js ============
// 「模型状态」页：展示本地重排(rerank)与嵌入(embedding)模型详情，
// 并轮询后端 /api/models/status 实时刷新 CPU / 内存 / GPU / 进程 资源占用。

let modelsPolling = null;

function startModelsPolling() {
    if (modelsPolling) return;
    modelsPolling = setInterval(loadModelsPage, 3000);
    loadModelsPage();
}

function stopModelsPolling() {
    if (modelsPolling) {
        clearInterval(modelsPolling);
        modelsPolling = null;
    }
}

// 格式化字节为易读单位（B/KB/MB/GB）
function fmtBytes(n) {
    if (n == null) return '-';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let v = Number(n);
    let i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(1)) + ' ' + u[i];
}

// 依据占用百分比返回配色档位（与系统主题一致）
function usageLevel(pct) {
    if (pct == null) return 'unknown';
    if (pct >= 90) return 'critical';
    if (pct >= 75) return 'warn';
    if (pct >= 50) return 'mid';
    return 'ok';
}

function badgeClass(state) {
    // 与 vector_status.js 的 state→badge 映射保持一致
    const map = {
        ready: 'success', loaded: 'success', idle: 'warning',
        pending: 'warning', downloading: 'warning', loading: 'warning',
        disabled: 'muted', error: 'error', unknown: 'muted', not_loaded: 'muted',
    };
    return map[state] || 'muted';
}

function badgeText(state) {
    const map = {
        ready: '已就绪', loaded: '已加载', idle: '待命中',
        pending: '等待中', downloading: '下载中', loading: '加载中',
        disabled: '已禁用', error: '错误', unknown: '未知', not_loaded: '未加载',
    };
    return map[state] || state || '-';
}

function renderModelCard(kind, d) {
    if (!d) d = {};
    const isEmb = kind === 'embedding';
    const title = isEmb ? '嵌入模型 (Embedding)' : '重排模型 (Rerank)';
    const icon = isEmb ? 'fa-brain' : 'fa-arrows-left-right';
    const enabled = d.enabled !== false; // rerank 默认视为开启态由 state 体现
    const state = d.state || 'unknown';
    const showProgress = isEmb && (state === 'downloading' || state === 'loading');

    const rows = [];
    rows.push(`<div class="mc-row"><span class="mc-label">模型名称</span><span class="mc-value">${escapeHtml(d.model || '-')}</span></div>`);
    if (isEmb) {
        rows.push(`<div class="mc-row"><span class="mc-label">提供方</span><span class="mc-value">${escapeHtml(d.provider || '-')}</span></div>`);
    }
    rows.push(`<div class="mc-row"><span class="mc-label">功能开关</span><span class="mc-value">${enabled ? '已开启' : '已关闭'}</span></div>`);
    if (d.offline !== null && d.offline !== undefined) {
        rows.push(`<div class="mc-row"><span class="mc-label">离线模式</span><span class="mc-value">${d.offline ? '是' : '否'}</span></div>`);
    }
    if (!isEmb) {
        rows.push(`<div class="mc-row"><span class="mc-label">已载入内存</span><span class="mc-value">${d.loaded ? '是' : '否'}</span></div>`);
        if (d.device) rows.push(`<div class="mc-row"><span class="mc-label">运行设备</span><span class="mc-value">${escapeHtml(d.device)}</span></div>`);
    }
    if (d.message) rows.push(`<div class="mc-row mc-msg"><span class="mc-value">${escapeHtml(d.message)}</span></div>`);

    const progressHtml = showProgress
        ? `<div class="mc-progress"><div class="mc-progress-bar" style="width:${Math.max(0, Math.min(100, d.progress || 0))}%"></div></div>`
        : '';

    return `
    <div class="model-card">
        <div class="mc-head">
            <div class="mc-title"><i class="fa-solid ${icon}"></i> ${title}</div>
            <span class="status-badge ${badgeClass(state)}">${badgeText(state)}</span>
        </div>
        <div class="mc-body">${rows.join('')}</div>
        ${progressHtml}
    </div>`;
}

function renderResourcePanel(sys) {
    if (!sys) sys = {};
    const cpu = sys.cpu_percent;
    const mem = sys.memory || {};
    const gpu = sys.gpu || {};
    const proc = sys.process || {};

    const cpuLevel = usageLevel(cpu);
    const memLevel = usageLevel(mem.percent);

    let gpuHtml;
    if (gpu.available && gpu.devices && gpu.devices.length) {
        // 过滤出有实际数据的设备；全部无数据时折叠为单行提示
        const devicesWithData = gpu.devices.filter(dev =>
            dev.utilization_percent != null || (dev.memory_total_bytes && dev.memory_total_bytes > 0)
        );
        if (devicesWithData.length) {
            gpuHtml = devicesWithData.map(dev => {
                const lvl = usageLevel(dev.utilization_percent);
                const util = dev.utilization_percent != null ? `${dev.utilization_percent}%` : 'N/A';
                const memPct = (dev.memory_total_bytes) ? Math.round((dev.memory_used_bytes || 0) / dev.memory_total_bytes * 100) : null;
                const memLvl = usageLevel(memPct);
                return `
            <div class="gpu-card">
                <div class="gpu-head"><i class="fa-solid fa-microchip"></i> ${escapeHtml(dev.name || ('GPU ' + dev.index))}
                    <span class="gpu-backend">${escapeHtml(gpu.backend || '')}</span></div>
                <div class="meter">
                    <div class="meter-head"><span>利用率</span><span>${util}</span></div>
                    <div class="meter-track"><div class="meter-fill lvl-${lvl}" style="width:${dev.utilization_percent != null ? dev.utilization_percent : 0}%"></div></div>
                </div>
                <div class="meter">
                    <div class="meter-head"><span>显存</span><span>${fmtBytes(dev.memory_used_bytes)} / ${fmtBytes(dev.memory_total_bytes)}</span></div>
                    <div class="meter-track"><div class="meter-fill lvl-${memLvl}" style="width:${memPct != null ? memPct : 0}%"></div></div>
                </div>
            </div>`;
            }).join('');
        } else {
            // 设备存在但无可读指标（如 Apple Silicon MPS），折叠为一行
            const names = gpu.devices.map(d => escapeHtml(d.name || ('GPU ' + d.index))).join('、');
            gpuHtml = `<div class="gpu-compact"><i class="fa-solid fa-microchip"></i> GPU：${names}（${escapeHtml(gpu.backend || '')}，暂无占用指标）</div>`;
        }
    } else {
        gpuHtml = `<div class="gpu-none"><i class="fa-solid fa-circle-info"></i> 未检测到 GPU（${escapeHtml(gpu.reason || '当前环境无可用 GPU 库')}）</div>`;
    }

    return `
    <div class="resource-panel">
        <div class="rp-title"><i class="fa-solid fa-gauge-high"></i> 运行时资源占用</div>
        <div class="rp-grid">
            <div class="meter-card">
                <div class="meter">
                    <div class="meter-head"><span><i class="fa-solid fa-microchip"></i> CPU</span><span>${cpu != null ? cpu + '%' : 'N/A'}</span></div>
                    <div class="meter-track"><div class="meter-fill lvl-${cpuLevel}" style="width:${cpu != null ? cpu : 0}%"></div></div>
                    <div class="meter-sub">${sys.cpu_count_physical || '?'} 物理核 / ${sys.cpu_count_logical || '?'} 逻辑核</div>
                </div>
            </div>
            <div class="meter-card">
                <div class="meter">
                    <div class="meter-head"><span><i class="fa-solid fa-memory"></i> 内存</span><span>${mem.percent != null ? mem.percent + '%' : 'N/A'}</span></div>
                    <div class="meter-track"><div class="meter-fill lvl-${memLevel}" style="width:${mem.percent != null ? mem.percent : 0}%"></div></div>
                    <div class="meter-sub">${fmtBytes(mem.used_bytes)} / ${fmtBytes(mem.total_bytes)}</div>
                </div>
            </div>
        </div>
        <div class="gpu-section">
            <div class="gpu-section-title"><i class="fa-solid fa-server"></i> GPU</div>
            ${gpuHtml}
        </div>
        <div class="proc-line">
            <i class="fa-solid fa-gear"></i> 托管进程：PID ${proc.pid != null ? proc.pid : '-'}
            ${proc.name ? '· ' + escapeHtml(proc.name) : ''}
            ${proc.cpu_percent != null ? '· CPU ' + proc.cpu_percent + '%' : ''}
            ${proc.memory_rss_bytes != null ? '· 内存 ' + fmtBytes(proc.memory_rss_bytes) : ''}
            ${proc.num_threads != null ? '· 线程 ' + proc.num_threads : ''}
        </div>
    </div>`;
}

async function loadModelsPage() {
    const body = document.getElementById('models-body');
    if (!body) return;
    try {
        const r = await api.fetch('/api/models/status');
        if (!r || r.error) {
            body.innerHTML = `<div class="models-error"><i class="fa-solid fa-triangle-exclamation"></i> 无法获取模型状态（${r && r.error ? r.error : '未知错误'}）</div>`;
            return;
        }
        const cards = `
            <div class="models-cards">
                ${renderModelCard('embedding', r.embedding)}
                ${renderModelCard('rerank', r.rerank)}
            </div>
            ${renderResourcePanel(r.system)}
            <div class="models-foot">最后更新：${formatTsLocal(new Date(r.timestamp).toISOString())}</div>
        `;
        body.innerHTML = cards;
    } catch (e) {
        // 瞬时网络错误：保留上一次内容，仅提示
        if (body.innerHTML.indexOf('models-error') === -1) {
            body.innerHTML = `<div class="models-error"><i class="fa-solid fa-triangle-exclamation"></i> 状态刷新失败，将自动重试…</div>`;
        }
    }
}

window.loadModelsPage = loadModelsPage;
window.startModelsPolling = startModelsPolling;
window.stopModelsPolling = stopModelsPolling;
