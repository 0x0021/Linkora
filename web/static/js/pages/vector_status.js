// ============ pages/vector_status.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ 向量模型加载状态（含下载进度）轮询 ============
function startEmbeddingStatusPolling() {
    if (embStatusPolling) return;
    embStatusPolling = setInterval(loadEmbeddingStatus, 1200);
    loadEmbeddingStatus();
}

function stopEmbeddingStatusPolling() {
    if (embStatusPolling) {
        clearInterval(embStatusPolling);
        embStatusPolling = null;
    }
}

async function loadEmbeddingStatus() {
    try {
        const st = await api.fetch('/api/embedding-status');
        if (!st) return;
        const modelEl = document.getElementById('rag-display-embedding-model');
        const statusEl = document.getElementById('rag-display-embedding-status');
        const barWrap = document.getElementById('emb-progress');
        const bar = document.getElementById('emb-progress-bar');
        const pctEl = document.getElementById('emb-progress-pct');
        if (modelEl) modelEl.textContent = st.model || '-';
        const map = {
            pending: ['等待中', 'warning'],
            downloading: ['下载中', 'warning'],
            loading: ['加载中', 'warning'],
            ready: ['已就绪', 'success'],
            error: ['错误', 'error'],
            disabled: ['已禁用', 'muted'],
            unknown: ['未知', 'muted'],
        };
        const [txt, cls] = map[st.state] || ['-', 'muted'];
        if (statusEl) { statusEl.textContent = txt; statusEl.className = `status-badge ${cls}`; }
        const active = (st.state === 'downloading' || st.state === 'loading');
        if (barWrap) barWrap.style.display = active ? 'block' : 'none';
        if (bar && active) bar.style.width = Math.max(0, Math.min(100, st.progress || 0)) + '%';
        if (pctEl) {
            pctEl.style.display = active ? 'block' : 'none';
            pctEl.textContent = (st.progress != null ? st.progress.toFixed(1) : '0.0') + '%';
        }
        // 终态（已就绪/错误/禁用/未知）停止快速轮询，避免空转
        if (embStatusPolling && ['ready', 'error', 'disabled', 'unknown'].includes(st.state)) {
            clearInterval(embStatusPolling);
            embStatusPolling = null;
        }
    } catch (e) {
        // 忽略瞬时网络错误
    }
}

