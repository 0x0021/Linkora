// ============ pages/rag.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ RAG Knowledge Base ============
let currentRagTab = 'overview';
let _kbPage = 1;
const _KB_PAGE_SIZE = 20;
let _kbTotal = 0;

/**
 * 结构化来源标签：将原始 source 值映射为可读分类 + 彩色 tag
 * 分类规则：
 *   dingtalk     → 📎 钉钉文档  (blue)
 *   manual       → ✏️ 手动录入  (purple)
 *   upload:*     → 📁 本地上传  (green)
 *   web:* / doc_type=web → 🌐 WEB 爬取 (orange)
 *   其他          → 📎 其他     (gray)
 */
function formatSource(source, docType) {
    var s = (source || '').trim();
    if (!s) return '<span class="tag tag-gray">\u2212</span>';
    if (s === 'dingtalk') {
        return '<span class="tag tag-source-dingtalk">\u9489\u9489\u6587\u6863</span>';
    }
    if (s === 'manual') {
        return '<span class="tag tag-source-manual">\u624b\u52a8\u5f55\u5165</span>';
    }
    if (s.indexOf('upload:') === 0 || s === 'upload') {
        return '<span class="tag tag-source-upload">\u672c\u5730\u4e0a\u4f20</span>';
    }
    if (s.indexOf('web:') === 0 || docType === 'web' || s.indexOf('http://') === 0 || s.indexOf('https://') === 0) {
        return '<span class="tag tag-source-web">WEB \u722c\u53d6</span>';
    }
    // marketplace 等其他
    return '<span class="tag tag-gray">' + escapeHtml(s) + '</span>';
}

/**
 * 结构化类型标签：将原始 doc_type 值映射为可读中文 + 彩色 tag
 * 配色与来源标签体系一致，重叠类型（dingtalk/web/manual/upload）直接复用来源色
 */
function formatDocType(docType) {
    var t = (docType || '').trim();
    if (!t) return '<span class="tag tag-gray">\u672a\u77e5</span>';
    var map = {
        'text':     { label: '\u7eaf\u6587\u672c',   cls: 'tag-type-text' },
        'markdown': { label: 'Markdown',  cls: 'tag-type-markdown' },
        'dingtalk': { label: '\u9489\u9489\u6587\u6863', cls: 'tag-type-dingtalk' },
        'faq':      { label: 'FAQ',       cls: 'tag-type-faq' },
        'web':      { label: 'WEB \u722c\u53d6', cls: 'tag-type-web' },
        'manual':   { label: '\u624b\u52a8\u5f55\u5165', cls: 'tag-type-manual' },
        'upload':   { label: '\u672c\u5730\u4e0a\u4f20', cls: 'tag-type-upload' },
        'doc':      { label: 'Word',      cls: 'tag-type-doc' },
        'docx':     { label: 'Word',      cls: 'tag-type-doc' },
        'pdf':      { label: 'PDF',       cls: 'tag-type-pdf' }
    };
    var m = map[t];
    if (m) return '<span class="tag ' + m.cls + '">' + m.label + '</span>';
    return '<span class="tag tag-gray">' + escapeHtml(t) + '</span>';
}

/** 根据当前活跃标签页刷新对应数据（解决 mutation 后视图不更新的根因） */
function refreshActiveRagData() {
    switch (currentRagTab) {
        case 'overview':  return loadRagOverview();
        case 'documents': return loadKbDocs();
        case 'chunks':   return loadKbChunksPage();
        case 'memory':   return loadMemoryList();
        default:         return loadKbDocs();  // fallback
    }
}

/**
 * 根据当前选中的平台动态显示/隐藏导入按钮：
 * - 钉钉平台 → 显示「钉钉导入」，隐藏「飞书导入」
 * - 飞书平台 → 显示「飞书导入」，隐藏「钉钉导入」
 * - 企微禁用 → 两按钮均隐藏
 * - 企微启用 → 两按钮均显示（默认行为）
 */
function syncImportButtonsByPlatform() {
    const pid = window.store.getPlatform();
    const wecomBtn = document.querySelector('.platform-btn[data-platform="wecom"]');
    const wecomDisabled = wecomBtn && wecomBtn.classList.contains('disabled');

    // 平台名称映射（用于 onboarding 文案）
    const platformDocName = (pid === 'feishu') ? '飞书' : (pid === 'wecom') ? '企微' : '钉钉';

    // 同步 onboarding 卡片内的平台感知文案
    var mainDesc = document.getElementById('onboarding-desc-main');
    if (mainDesc) {
        mainDesc.textContent = '将' + platformDocName + '文档、FAQ、产品手册等知识导入知识库，让 AI 助手基于你的知识进行智能回答';
    }
    var step1Desc = document.getElementById('onboarding-desc-step1');
    if (step1Desc) {
        step1Desc.textContent = '从' + platformDocName + '文档同步或手动添加知识文档';
    }

    const dingtalkBtns = document.querySelectorAll('[id^="btn-dingtalk-import"]');
    const feishuBtns = document.querySelectorAll('[id^="btn-feishu-import"]');

    // 企微平台且被禁用 → 全部隐藏
    if (pid === 'wecom' && wecomDisabled) {
        dingtalkBtns.forEach(function (b) { b.style.display = 'none'; });
        feishuBtns.forEach(function (b) { b.style.display = 'none'; });
        return;
    }

    if (pid === 'dingtalk') {
        dingtalkBtns.forEach(function (b) { b.style.display = ''; });
        feishuBtns.forEach(function (b) { b.style.display = 'none'; });
        return;
    }

    if (pid === 'feishu') {
        dingtalkBtns.forEach(function (b) { b.style.display = 'none'; });
        feishuBtns.forEach(function (b) { b.style.display = ''; });
        return;
    }

    // 默认（企微启用或其他）：全部显示
    dingtalkBtns.forEach(function (b) { b.style.display = ''; });
    feishuBtns.forEach(function (b) { b.style.display = ''; });
}

function loadRagPage() {
    switchRagTab('overview');
    syncImportButtonsByPlatform();
}

function switchRagTab(tab) {
    currentRagTab = tab;
    document.querySelectorAll('#page-rag .section-tab').forEach(t => {
        t.classList.toggle('active', t.dataset.tab === tab);
    });
    document.querySelectorAll('#page-rag .section-tab-content').forEach(c => {
        const isActive = c.id === `rag-tab-${tab}`;
        c.classList.toggle('active', isActive);
        c.style.display = isActive ? '' : 'none';
    });

    if (tab === 'overview') loadRagOverview();
    if (tab === 'documents') loadKbDocs();
    if (tab === 'chunks') loadKbChunksPage();
    if (tab === 'search') initRagSearch();
    if (tab === 'chat') initRagChat();
    if (tab === 'memory') { initMemoryFilters(); loadMemoryList(); }
}

async function loadRagOverview() {
    try {
        const stats = await api.getKbStats();
        if (!stats) return;

        const totalDocs = stats.total_documents || 0;
        const totalChunks = stats.total_chunks || 0;

        document.getElementById('rag-stat-docs').textContent = totalDocs;
        document.getElementById('rag-stat-chunks').textContent = totalChunks;
        document.getElementById('rag-stat-indexed').textContent = stats.indexed_docs || 0;
        document.getElementById('rag-stat-sources').textContent = stats.by_source?.length || 0;
        document.getElementById('rag-stat-memories').textContent = stats.total_memories || 0;

        const onboarding = document.getElementById('rag-onboarding');
        const content = document.getElementById('rag-overview-content');
        if (totalDocs === 0) {
            onboarding.style.display = 'flex';
            content.style.display = 'none';
        } else {
            onboarding.style.display = 'none';
            content.style.display = 'block';
        }

        const byTypeContainer = document.getElementById('rag-by-type');
        if (stats.by_type && stats.by_type.length > 0) {
            byTypeContainer.innerHTML = stats.by_type.map(t => `
                <div class="type-stat-item">
                    <span class="type-stat-name">${formatDocType(t.doc_type)}</span>
                    <div class="type-stat-bar">
                        <div class="type-stat-fill" style="width: ${(t.cnt / (totalDocs || 1) * 100)}%"></div>
                    </div>
                    <span class="type-stat-count">${t.cnt}</span>
                </div>
            `).join('');
        }

        const docs = await api.getKbDocs('', '', 10);
        if (docs && docs.documents) {
        const tbody = document.getElementById('rag-recent-docs-body');
        tbody.innerHTML = docs.documents.map(d => `
            <tr>
                <td>${escapeHtml(d.title)}</td>
                <td>${formatDocType(d.doc_type)}</td>
                <td>${formatSource(d.source, d.doc_type)}</td>
                <td>${d.chunk_count || 0}</td>
                <td><span class="status-badge ${d.status === 'indexed' ? 'success' : 'warning'}">${d.status === 'indexed' ? '已索引' : '待索引'}</span></td>
                <td>${formatTime(d.updated_at)}</td>
            </tr>
        `).join('');
        }
        // 加载 Embedding 状态信息
        loadRagSettings();
        syncImportButtonsByPlatform();
    } catch (e) {
        console.error('loadRagOverview failed:', e);
        showToast('概览加载失败', 'error');
    }
}

async function loadKbDocs() {
    try {
        const status = document.getElementById('kb-status-filter').value;
        const docType = document.getElementById('kb-type-filter').value;
        const data = await api.getKbDocs(status, docType, _KB_PAGE_SIZE, (_kbPage - 1) * _KB_PAGE_SIZE);
        if (!data) return;
        _kbTotal = data.total || 0;

        if (data.stats) {
            document.getElementById('kb-stat-docs').textContent = `${data.stats.total_documents || 0} 篇文档`;
        document.getElementById('kb-stat-chunks').textContent = `${data.stats.total_chunks || 0} 个分块`;
    }

    const tbody = document.getElementById('kb-body');
    if (!data.documents || data.documents.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无文档，点击"添加文档"开始构建知识库</td></tr>';
        return;
    }

    tbody.innerHTML = data.documents.map(d => `
        <tr>
            <td>
                <div class="doc-title-cell">
                    <span class="doc-icon">${iconize("📄")}</span>
                    <span>${escapeHtml(d.title)}</span>
                </div>
            </td>
            <td>${formatDocType(d.doc_type)}</td>
            <td>${formatSource(d.source, d.doc_type)}</td>
            <td>${d.chunk_count || 0}</td>
            <td><span class="status-badge ${d.status === 'indexed' ? 'success' : 'warning'}">${d.status === 'indexed' ? '已索引' : '待索引'}</span></td>
            <td>${formatTime(d.updated_at)}</td>
            <td>
                <div class="action-btns">
                    <button class="btn btn-sm btn-outline-secondary" onclick="viewKbDoc(${d.id})" title="查看"><i class="fa-solid fa-eye"></i></button>
                    <button class="btn btn-sm btn-outline-secondary" onclick="editKbDoc(${d.id})" title="编辑"><i class="fa-solid fa-pen-to-square"></i></button>
                    <button class="btn btn-sm btn-outline-secondary" onclick="reindexKbDoc(${d.id})" title="重建索引"><i class="fa-solid fa-rotate-right"></i></button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteKbDoc(${d.id})" title="删除"><i class="fa-solid fa-trash"></i></button>
                </div>
            </td>
        </tr>
    `).join('');
        renderKbPager();
    } catch (e) {
        console.error('loadKbDocs failed:', e);
        showToast('文档列表加载失败', 'error');
    }
}

// 筛选变化：重置到第 1 页再加载（保持与翻页一致）
function reloadKbDocs() {
    _kbPage = 1;
    loadKbDocs();
}

function renderKbPager() {
    renderPager('kb-pagination', {
        total: _kbTotal, page: _kbPage, pageSize: _KB_PAGE_SIZE,
    }, function (p) {
        _kbPage = p;
        loadKbDocs();
    });
}

// 当前选中的文档 ID
var selectedChunkDocId = null;

async function loadKbChunksPage() {
    try {
        const data = await api.getKbDocs('', '', 50);
        const container = document.getElementById('chunk-doc-list');
        const countEl = document.getElementById('chunk-doc-count');

        if (!data || !data.documents || data.documents.length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon"><i class="fa-solid fa-file-circle-question"></i></div><p>\u6682\u65e0\u6587\u6863</p></div>';
            countEl.textContent = '0 \u7bc7';
            return;
        }

        countEl.textContent = data.documents.length + ' \u7bc7';

        container.innerHTML = data.documents.map(function(d) {
            var isActive = selectedChunkDocId && String(selectedChunkDocId) === String(d.id);
            return '<div class="chunk-doc-item' + (isActive ? ' active' : '') + '" onclick="selectChunkDoc(' + d.id + ')">' +
                '<div class="chunk-doc-item-title" title="' + escapeHtml(d.title) + '">' + escapeHtml(d.title) + '</div>' +
                '<div class="chunk-doc-item-meta">' +
                    '<span><i class="fa-solid fa-puzzle-piece"></i> ' + (d.chunk_count || 0) + ' \u5757</span>' +
                    (d.source ? '<span>' + formatSource(d.source, d.doc_type) + '</span>' : '') +
                '</div>' +
            '</div>';
        }).join('');

        // 自动选中第一个（如果还没有选中项）
        if (!selectedChunkDocId && data.documents.length > 0) {
            selectChunkDoc(data.documents[0].id);
        }
    } catch (e) {
        console.error('loadKbChunksPage failed:', e);
        showToast('分块列表加载失败', 'error');
    }
}

function selectChunkDoc(docId) {
    // 更新左侧列表高亮
    var items = document.querySelectorAll('.chunk-doc-item');
    items.forEach(function(item) {
        item.classList.toggle('active', item.getAttribute('onclick') === ('selectChunkDoc(' + docId + ')'));
    });

    selectedChunkDocId = docId;
    viewKbDocChunks(docId);
}

async function viewKbDocChunks(docId) {
    docId = docId || selectedChunkDocId;
    if (!docId) return;

    var container = document.getElementById('chunk-list');

    // 显示加载状态
    container.innerHTML = '<div class="loading-state"><div class="loading"></div> \u52a0\u8f7d\u4e2d...</div>';

    try {
        var data = await api.getKbDocument(parseInt(docId));
        if (!data || !data.document) {
            container.innerHTML = '<div class="empty-state"><h3>\u52a0\u8f7d\u5931\u8d25</h3></div>';
            return;
        }

        var doc = data.document;
        var chunks = doc.chunks || [];

        // 更新右侧标题和计数
        document.getElementById('chunk-selected-doc').textContent = doc.title || '';
        document.getElementById('chunk-count').textContent = chunks.length + ' \u4e2a\u5206\u5757';

        if (chunks.length === 0) {
            container.innerHTML = '<div class="empty-state">' +
                '<div class="empty-icon"><i class="fa-solid fa-puzzle-piece"></i></div>' +
                '<h3>\u8be5\u6587\u6863\u6682\u65e0\u5206\u5757</h3>' +
                '<p>\u8be5\u6587\u6863\u5c1a\u672a\u88ab\u5206\u5757\u5904\u7406</p>' +
            '</div>';
            return;
        }

        container.innerHTML = chunks.map(function(c) {
            return '<div class="chunk-card">' +
                '<div class="chunk-header">' +
                    '<span class="chunk-index">\u7b2c ' + (c.chunk_index + 1) + ' \u5757</span>' +
                    '<span class="chunk-status ' + (c.embedding ? 'has-embedding' : 'no-embedding') + '">' +
                        (c.embedding ? iconize('\u2713') + ' \u5df2\u5411\u91cf\u5316' : iconize('\u2717') + ' \u672a\u5411\u91cf\u5316') +
                    '</span>' +
                '</div>' +
                '<div class="chunk-content">' + escapeHtml(c.content) + '</div>' +
                '<div class="similarity-bar">' +
                    '<div class="similarity-bar-fill" style="width: ' + (c.embedding ? '100%' : '0%') + '"></div>' +
                '</div>' +
            '</div>';
        }).join('');
    } catch (e) {
        console.error('viewKbDocChunks failed:', e);
        container.innerHTML = '<div class="empty-state"><h3>加载失败</h3></div>';
    }
}


// ============ RAG Search Test ============
async function initRagSearch() {
    try {
        const cfg = await api.getConfig();
        const adv = cfg?.llm?.advanced || {};
        // 相似度阈值默认值取配置文件里的 rag_min_similarity
        const sim = adv.rag_min_similarity;
        if (sim !== undefined && sim !== null) {
            document.getElementById('rag-search-threshold').value = sim;
        }
        // 返回数量默认值取配置文件里的 rag_max_results
        const maxResults = adv.rag_max_results;
        if (maxResults !== undefined && maxResults !== null) {
            document.getElementById('rag-search-topk').value = maxResults;
        }
    } catch (e) {
        // 拉取失败则保留 HTML 默认值
    }
}

async function testRagSearch() {
    const query = document.getElementById('rag-search-query').value.trim();
    const topK = parseInt(document.getElementById('rag-search-topk').value) || 5;
    const threshold = parseFloat(document.getElementById('rag-search-threshold').value) || 0;

    if (!query) {
        showToast('请输入查询文本', 'warning');
        return;
    }

    const resultContainer = document.getElementById('rag-search-result');
    resultContainer.innerHTML = '<div class="loading-state"><div class="loading"></div> 检索中...</div>';

    try {
    const result = await api.kbQuery(query, topK, threshold);

    if (!result || !result.success) {
        resultContainer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">${iconize("❌")}</div>
                <h3>检索失败</h3>
                <p>${result?.message || '请稍后再试'}</p>
            </div>
        `;
        return;
    }

    let results = result.results || [];
    results = results.filter(r => (r.similarity || 0) >= threshold);

    if (results.length === 0) {
        resultContainer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">${iconize("🔍")}</div>
                <h3>未找到相关内容</h3>
                <p>没有找到相似度大于 ${(threshold * 100).toFixed(0)}% 的知识片段</p>
            </div>
        `;
        return;
    }

    resultContainer.innerHTML = `
        <div class="search-results-header">
            <span>找到 <strong>${results.length}</strong> 个相关片段</span>
            <span class="text-muted">阈值 ${(threshold * 100).toFixed(0)}%</span>
        </div>
        <div class="search-results-list">
            ${results.map((r, i) => `
                <div class="search-result-card">
                    <div class="search-result-header">
                        <span class="search-result-rank">#${i + 1}</span>
                        <span class="search-result-title">${escapeHtml(r.title)}</span>
                        <span class="search-result-score">${(r.similarity * 100).toFixed(1)}%</span>
                    </div>
                    <div class="search-result-content">${escapeHtml(r.content)}</div>
                    <div class="similarity-bar">
                        <div class="similarity-bar-fill ${r.similarity > 0.7 ? 'high' : r.similarity > 0.5 ? 'medium' : ''}" 
                             style="width: ${r.similarity * 100}%"></div>
                    </div>
                    <div class="search-result-meta">
                        ${formatDocType(r.doc_type || 'text')}
                        来源: ${formatSource(r.source, r.doc_type)}
                        ${r.url && /^https?:\/\//i.test(r.url) ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" class="link">查看原文</a>` : ''}
                    </div>
                </div>
            `).join('')}
        </div>
    `;
    } catch (e) {
        console.error('testRagSearch failed:', e);
        resultContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">⚠</div><h3>检索失败</h3><p>请稍后再试</p></div>';
        showToast('检索失败', 'error');
    }
}

function clearRagSearch() {
    document.getElementById('rag-search-query').value = '';
    document.getElementById('rag-search-result').innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">${iconize("🔍")}</div>
            <h3>等待检索</h3>
            <p>输入查询文本，测试知识库的语义检索效果</p>
        </div>
    `;
}

async function viewKbDoc(docId) {
    try {
        const data = await api.getKbDocument(docId);
        if (!data || !data.document) {
            showToast('文档不存在', 'error');
            return;
        }
        document.getElementById('doc-view-title').textContent = data.document.title || '文档内容';
        const chunks = data.document.chunks || [];
        const content = chunks.map(c => c.content).join('\n\n---\n\n');
        document.getElementById('doc-view-content').innerHTML = simpleMarkdown(content);
        document.getElementById('doc-view-modal').classList.add('active');
    } catch (e) {
        console.error('viewKbDoc failed:', e);
        showToast('文档加载失败', 'error');
    }
}

async function reindexKbDoc(docId) {
    try {
        const result = await api.reindexKbDocument(docId);
        if (result && result.success) {
            showToast(result.message);
            refreshActiveRagData();
        } else {
            showToast(result?.message || '重建索引失败', 'error');
        }
    } catch (e) {
        console.error('reindexKbDoc failed:', e);
        showToast('重建索引失败', 'error');
    }
}

async function deleteKbDoc(docId) {
    if (!confirm('确定删除这篇文档吗？所有分块和索引也会被删除。')) return;
    try {
        const result = await api.deleteKbDocument(docId);
        if (result && result.success) {
            showToast('删除成功');
            refreshActiveRagData();
        } else {
            showToast(result?.message || '删除失败', 'error');
        }
    } catch (e) {
        console.error('deleteKbDoc failed:', e);
        showToast('删除失败', 'error');
    }
}


// ============ 文档编辑（KB文档 & 钉钉文档共用） ============
let editingDocId = null;   // 当前编辑的 KB 文档 ID
let editingDdocId = null;  // 当前编辑的钉钉文档 ID

async function editKbDoc(docId) {
    editingDocId = docId;
    editingDdocId = null;
    try {
    const data = await api.getKbDocument(docId);
    if (!data || !data.document) { showToast('加载失败', 'error'); return; }
    const d = data.document;
    const modal = document.getElementById('doc-edit-modal');
    document.getElementById('doc-edit-title').value = d.title || '';
    document.getElementById('doc-edit-content').value = d.content || '';
    document.getElementById('doc-edit-modal-title').innerHTML = `${icon('pencil')} 编辑知识库文档`;
    modal.classList.add('active');
    } catch (e) {
        console.error('editKbDoc failed:', e);
        editingDocId = null;
        showToast('加载失败', 'error');
    }
}

function closeDocEditModal() {
    document.getElementById('doc-edit-modal').classList.remove('active');
    editingDocId = null;
    editingDdocId = null;
}

async function saveDocEdit() {
    const title = document.getElementById('doc-edit-title').value.trim();
    const content = document.getElementById('doc-edit-content').value.trim();
    if (!title) { showToast('标题不能为空', 'warning'); return; }

    try {
    let result;
    if (editingDocId !== null) {
        result = await api.updateKbDocument(editingDocId, { title, content });
    } else if (editingDdocId !== null) {
        result = await api.updateDingtalkDoc(editingDdocId, { title, content });
    }

    if (result && result.success) {
        showToast('保存成功');
        closeDocEditModal();
        // 刷新对应列表/详情
        if (editingDocId !== null) refreshActiveRagData();
    } else {
        showToast(result?.detail || result?.message || '保存失败', 'error');
    }
    } catch (e) {
        console.error('saveDocEdit failed:', e);
        showToast('保存失败', 'error');
    }
}

function showKbModal() {
    document.getElementById('kb-modal').classList.add('active');
}

function closeKbModal() {
    document.getElementById('kb-modal').classList.remove('active');
}

async function saveKbDocument() {
    const data = {
        title: document.getElementById('kb-modal-title').value.trim(),
        content: document.getElementById('kb-modal-content').value.trim(),
        doc_type: document.getElementById('kb-modal-type').value,
        source: document.getElementById('kb-modal-source').value.trim() || 'manual',
    };

    if (!data.title || !data.content) {
        showToast('标题和内容不能为空', 'warning');
        return;
    }

    try {
    const result = await api.createKbDocument(data);
    if (result && result.success) {
        showToast(result.message || '添加成功');
        closeKbModal();
        refreshActiveRagData();
    } else if (result && result.duplicate) {
        showToast(result.message || '文档重复', 'warning');
    } else {
        showToast(result?.message || '添加失败', 'error');
    }
    } catch (e) {
        console.error('saveKbDocument failed:', e);
        showToast('创建失败', 'error');
    }
}


// ============ Batch Upload ============
let batchUploadFiles = [];

function showBatchUploadModal() {
    batchUploadFiles = [];
    document.getElementById('batch-file-input').value = '';
    renderBatchFileList();
    document.getElementById('batch-upload-modal').classList.add('active');
}

function closeBatchUploadModal() {
    document.getElementById('batch-upload-modal').classList.remove('active');
}


// ============ 钉钉文档导入（整合进 RAG）============
let ddocImportSelectedId = null;

function showDingtalkImportModal() {
    ddocImportSelectedId = null;
    document.getElementById('dingtalk-import-modal').classList.add('active');
    document.getElementById('ddoc-import-query').value = '';
    document.getElementById('ddoc-import-search').value = '';
    document.getElementById('ddoc-import-remote-list').innerHTML = '';
    document.getElementById('ddoc-import-preview').innerHTML = '';
    document.getElementById('ddoc-import-confirm-btn').disabled = true;
    refreshDingtalkImportList();
}

function closeDingtalkImportModal() {
    document.getElementById('dingtalk-import-modal').classList.remove('active');
}

async function refreshDingtalkImportList() {
    const keyword = document.getElementById('ddoc-import-search').value;
    const listEl = document.getElementById('ddoc-import-list');
    try {
        const data = await api.getDingtalkDocs(keyword);
        if (!data || !data.docs) {
            listEl.innerHTML = '<div class="list-item-empty">暂无已同步的钉钉文档</div>';
            return;
        }
        listEl.innerHTML = data.docs.map(doc => `
            <div class="list-item ${ddocImportSelectedId === doc.doc_id ? 'selected' : ''}" 
                 data-doc-id="${escapeHtml(doc.doc_id)}" onclick="selectDingtalkImportDoc(this.dataset.docId, this)">
                <div class="list-item-title">${escapeHtml(doc.title)}</div>
                <div class="list-item-meta">${formatTime(doc.synced_at)}</div>
            </div>
        `).join('');
    } catch (e) {
        listEl.innerHTML = '<div class="list-item-empty">加载失败</div>';
    }
}

async function selectDingtalkImportDoc(docId, el) {
    ddocImportSelectedId = docId;
    // 高亮选中
    document.querySelectorAll('#ddoc-import-list .list-item').forEach(li => li.classList.remove('selected'));
    el.classList.add('selected');
    
    // 加载预览
    const previewEl = document.getElementById('ddoc-import-preview');
    previewEl.innerHTML = '<div class="spinner"></div> 加载中...';
    try {
        const data = await api.getDingtalkDoc(docId);
        if (data && data.doc) {
            const content = data.doc.content || data.doc.markdown || '';
            previewEl.innerHTML = `<div style="font-weight:600;margin-bottom:8px;">${escapeHtml(data.doc.title)}</div>` + 
                `<div>${simpleMarkdown(content.substring(0, 500))}${content.length > 500 ? '...' : ''}</div>`;
            document.getElementById('ddoc-import-confirm-btn').disabled = false;
        } else {
            previewEl.innerHTML = '<span style="color:var(--danger)">无法加载文档内容，请先同步</span>';
            document.getElementById('ddoc-import-confirm-btn').disabled = true;
        }
    } catch (e) {
        previewEl.innerHTML = '<span style="color:var(--danger)">加载失败</span>';
    }
}

async function searchDingtalkImport() {
    const query = document.getElementById('ddoc-import-query').value.trim();
    if (!query) { showToast('请输入搜索关键词', 'warning'); return; }
    const listEl = document.getElementById('ddoc-import-remote-list');
    listEl.innerHTML = '<div class="spinner"></div> 搜索中...';
    try {
        const data = await api.searchDingtalkDocs(query);
        if (!data || !data.docs || data.docs.length === 0) {
            listEl.innerHTML = '<div class="list-item-empty">未找到相关文档</div>';
            return;
        }
        listEl.innerHTML = data.docs.map(doc => {
            const docId = doc.docId || doc.id || doc.nodeId || '';
            const title = doc.title || doc.name || '';
            return `
                <div class="remote-doc-item">
                    <span>${escapeHtml(title)}</span>
                    <button class="btn btn-sm btn-outline-secondary" onclick="syncAndSelectDingtalkDoc('${docId}', '${escapeHtml(title)}')"><i class="fa-solid fa-download"></i> 同步并导入</button>
                </div>
            `;
        }).join('');
    } catch (e) {
        listEl.innerHTML = '<div class="list-item-empty">搜索失败</div>';
    }
}

async function syncAndSelectDingtalkDoc(docId, title) {
    const previewEl = document.getElementById('ddoc-import-preview');
    previewEl.innerHTML = '<div class="spinner"></div> 同步中...';
    try {
        const data = await api.syncDingtalkDoc(docId);
        if (data && data.success) {
            showToast('同步成功');
            await refreshDingtalkImportList();
            // 自动选中刚同步的文档
            const docs = await api.getDingtalkDocs();
            const synced = docs.docs ? docs.docs.find(d => d.doc_id === docId || d.title === title) : null;
            if (synced) {
                ddocImportSelectedId = synced.doc_id;
                await refreshDingtalkImportList();
                const preview = document.querySelector(`#ddoc-import-list .list-item[onclick*="${synced.doc_id}"]`);
                if (preview) selectDingtalkImportDoc(synced.doc_id, preview);
            }
        } else {
            previewEl.innerHTML = '<span style="color:var(--danger)">' + (data?.message || '同步失败') + '</span>';
        }
    } catch (e) {
        previewEl.innerHTML = '<span style="color:var(--danger)">同步失败: ' + escapeHtml(e.message) + '</span>';
    }
}

async function confirmDingtalkImport() {
    if (!ddocImportSelectedId) { showToast('请先选择文档', 'warning'); return; }
    const btn = document.getElementById('ddoc-import-confirm-btn');
    btn.disabled = true;
    btn.textContent = '导入中...';
    try {
        const data = await api.importDingtalkDocToKb(ddocImportSelectedId);
        if (data && data.success) {
            showToast('导入成功！文档已加入知识库');
            closeDingtalkImportModal();
            refreshActiveRagData();  // 刷新当前 RAG 视图（含概览/文档列表）
        } else {
            showToast(data?.message || '导入失败', 'error');
        }
    } catch (e) {
        showToast('导入失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '导入到知识库';
    }
}


// ============ 飞书文档导入 ============
let fdocImportSelectedToken = null;
let fdocImportSelectedEntityType = '';

function showFeishuImportModal() {
    fdocImportSelectedToken = null;
    fdocImportSelectedEntityType = '';
    document.getElementById('feishu-import-modal').classList.add('active');
    document.getElementById('fdoc-import-query').value = '';
    document.getElementById('fdoc-import-list').innerHTML = '';
    document.getElementById('fdoc-import-preview').innerHTML = '';
    document.getElementById('fdoc-import-confirm-btn').disabled = true;
}

function closeFeishuImportModal() {
    document.getElementById('feishu-import-modal').classList.remove('active');
}

async function searchFeishuDocs() {
    const query = document.getElementById('fdoc-import-query').value.trim();
    if (!query) { showToast('请输入搜索关键词', 'warning'); return; }
    const listEl = document.getElementById('fdoc-import-list');
    listEl.innerHTML = '<div class="spinner"></div> 搜索中...';
    try {
        const data = await api.searchFeishuDocs(query);
        if (!data || !data.documents || data.documents.length === 0) {
            listEl.innerHTML = '<div class="list-item-empty">未找到相关飞书文档</div>';
            return;
        }
        // 检查是否返回了错误标记（如认证失败）
        const errItem = data.documents.find(d => d.error);
        if (errItem) {
            const errMsg = errItem.message || errItem.error || '未知错误';
            const errType = errItem.error === 'auth' ? '飞书认证失败，请检查 lark-cli 登录状态' : '搜索出错';
            listEl.innerHTML = `<div class="list-item-empty" style="color:var(--brand-danger);">${escapeHtml(errType)}: ${escapeHtml(errMsg)}</div>`;
            return;
        }
        listEl.innerHTML = data.documents.map(doc => {
            const token = doc.doc_token || doc.token || doc.id || '';
            const title = doc.title || doc.name || '';
            const etype = (doc.entity_type || '').replace(/'/g, '');
            if (doc.importable === false) {
                return `
                    <div class="remote-doc-item">
                        <span>${escapeHtml(title)}</span>
                        <span style="font-size:12px;color:var(--text-secondary);">${escapeHtml(doc.unsupported_reason || '该类型暂不支持导入')}</span>
                    </div>
                `;
            }
            return `
                <div class="remote-doc-item">
                    <span>${escapeHtml(title)}</span>
                    <button class="btn btn-sm btn-primary" onclick="selectFeishuImportDoc('${escapeHtml(token)}', '${escapeHtml(title)}', '${escapeHtml(etype)}', this)">导入此文档</button>
                </div>
            `;
        }).join('');
    } catch (e) {
        listEl.innerHTML = '<div class="list-item-empty">搜索失败: ' + escapeHtml(e.message) + '</div>';
    }
}

function selectFeishuImportDoc(token, title, entityType, el) {
    fdocImportSelectedToken = token;
    fdocImportSelectedEntityType = entityType || '';
    // 高亮选中
    document.querySelectorAll('#fdoc-import-list .remote-doc-item').forEach(li => li.style.background = '');
    el.parentElement.style.background = 'var(--bg-secondary)';
    const previewEl = document.getElementById('fdoc-import-preview');
    previewEl.innerHTML = `<div style="font-weight:600;margin-bottom:8px;">已选择：${escapeHtml(title)}</div>
        <div style="font-size:12px;color:var(--text-secondary);">文档 Token: ${escapeHtml(token)}</div>`;
    document.getElementById('fdoc-import-confirm-btn').disabled = false;
}

async function confirmFeishuImport() {
    if (!fdocImportSelectedToken) { showToast('请先选择文档', 'warning'); return; }
    const btn = document.getElementById('fdoc-import-confirm-btn');
    btn.disabled = true;
    btn.textContent = '导入中...';
    try {
        const data = await api.importFeishuDoc(fdocImportSelectedToken, '', fdocImportSelectedEntityType);
        if (data && data.success) {
            showToast('导入成功！飞书文档已加入知识库');
            closeFeishuImportModal();
            refreshActiveRagData();
        } else {
            showToast(data?.message || '导入失败', 'error');
        }
    } catch (e) {
        showToast('导入失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = '导入到知识库';
    }
}


// ============ URL Import ============
function showImportUrlModal() {
    document.getElementById('import-url-modal').classList.add('active');
    document.getElementById('import-url-input').value = '';
    document.getElementById('import-url-title').value = '';
    document.getElementById('import-url-preview').style.display = 'none';
    document.getElementById('import-url-btn').disabled = false;
    document.getElementById('import-url-btn').textContent = '导入';
}

function closeImportUrlModal() {
    document.getElementById('import-url-modal').classList.remove('active');
}

async function startImportUrl() {
    const url = document.getElementById('import-url-input').value.trim();
    if (!url) {
        showToast('请输入网页 URL', 'warning');
        return;
    }

    const btn = document.getElementById('import-url-btn');
    btn.disabled = true;
    btn.textContent = '导入中...';

    try {
        const body = { url };
        const title = document.getElementById('import-url-title').value.trim();
        if (title) body.title = title;

        const result = await api.fetch('/api/kb/import-url', 'POST', body);

        if (result.success) {
            showToast(`网页导入成功，已分 ${result.chunks} 个块`, 'success');
            closeImportUrlModal();
            refreshActiveRagData();
        } else if (result.duplicate) {
            showToast(`文档重复：${result.reason}`, 'warning');
        } else {
            showToast(result.message || '导入失败', 'error');
            btn.disabled = false;
            btn.textContent = '导入';
        }
    } catch (e) {
        showToast(`导入失败：${e.message}`, 'error');
        btn.disabled = false;
        btn.textContent = '导入';
    }
}

function handleDragOver(e) {
    e.preventDefault();
    e.currentTarget.classList.add('drag-over');
}

function handleDragLeave(e) {
    e.currentTarget.classList.remove('drag-over');
}

function handleDrop(e) {
    e.preventDefault();
    e.currentTarget.classList.remove('drag-over');
    const allowed = ['.txt', '.md', '.markdown', '.pdf', '.ppt', '.pptx', '.doc', '.docx', '.html', '.htm', '.png', '.jpg', '.jpeg', '.bmp'];
    const files = Array.from(e.dataTransfer.files).filter(f =>
        allowed.some(ext => f.name.toLowerCase().endsWith(ext))
    );
    addBatchFiles(files);
}

function handleBatchFileSelect(e) {
    const files = Array.from(e.target.files);
    addBatchFiles(files);
}

function addBatchFiles(files) {
    for (const file of files) {
        if (!batchUploadFiles.find(f => f.name === file.name && f.size === file.size)) {
            batchUploadFiles.push({ file, status: 'pending', result: null });
        }
    }
    renderBatchFileList();
}

function removeBatchFile(index) {
    batchUploadFiles.splice(index, 1);
    renderBatchFileList();
}

function renderBatchFileList() {
    const container = document.getElementById('upload-file-list');
    const btn = document.getElementById('batch-upload-btn');
    if (batchUploadFiles.length === 0) {
        container.innerHTML = '';
        btn.disabled = true;
        return;
    }
    btn.disabled = false;
    container.innerHTML = batchUploadFiles.map((item, i) => {
        const statusText = {
            pending: '待上传',
            uploading: '上传中...',
            success: `${iconize("✓")} 成功`,
            error: `${iconize("✗")} 失败`,
        }[item.status];
        return `
            <div class="upload-file-item">
                <span class="file-name">${escapeHtml(item.file.name)}</span>
                <span class="file-size">${(item.file.size / 1024).toFixed(1)} KB</span>
                <span class="file-status ${item.status}">${statusText}</span>
                ${item.status === 'pending' ? `<span class="file-remove" onclick="removeBatchFile(${i})">${iconize("✕")}</span>` : ''}
            </div>
        `;
    }).join('');
}

async function startBatchUpload() {
    const pending = batchUploadFiles.filter(f => f.status === 'pending');
    if (pending.length === 0) return;

    const btn = document.getElementById('batch-upload-btn');
    btn.disabled = true;

    const totalCount = pending.length;
    let completedCount = 0;

    for (const item of pending) {
        item.status = 'uploading';
        completedCount++;
        // 更新按钮文字显示进度
        btn.textContent = `上传中 (${completedCount}/${totalCount})`;
        renderBatchFileList();

        try {
            let text = '';
            let docType = 'text';
            const title = item.file.name.replace(/\.[^/.]+$/, '');

            // iconize("🆕") 支持多种文档格式（PDF、PPT、Word、图片）
            const fileName = item.file.name.toLowerCase();
            const isPdf = fileName.endsWith('.pdf');
            const isPpt = fileName.endsWith('.ppt') || fileName.endsWith('.pptx');
            const isDocx = fileName.endsWith('.doc') || fileName.endsWith('.docx');
            const isImage = fileName.endsWith('.png') || fileName.endsWith('.jpg') || fileName.endsWith('.jpeg') || fileName.endsWith('.bmp');
            const isHtml = fileName.endsWith('.html') || fileName.endsWith('.htm');
            
            if (isPdf || isPpt || isDocx || isImage || isHtml) {
                // 走后端统一解析接口
                const formData = new FormData();
                formData.append('file', item.file);
                
                const parseResult = await api.upload('/api/kb/parse-document', formData);
                
                if (parseResult.success) {
                    text = parseResult.text;
                    docType = 'text';
                } else {
                    throw new Error(parseResult.error || '文档解析失败');
                }
            } else {
                // 其他文件（txt, md）走原有逻辑
                text = await readFileAsText(item.file);
                docType = item.file.name.endsWith('.md') || item.file.name.endsWith('.markdown') ? 'markdown' : 'text';
            }

            const result = await api.createKbDocument({
                title: title,
                content: text,
                doc_type: docType,
                source: 'upload:' + item.file.name,
            });

            if (result && result.success) {
                item.status = 'success';
                item.result = result;
            } else if (result && result.duplicate) {
                item.status = 'error';
                item.result = result;
            } else {
                item.status = 'error';
                item.result = { message: result?.message || '上传失败' };
            }
        } catch (e) {
            item.status = 'error';
            item.result = { message: String(e) };
        }
        renderBatchFileList();
    }

    btn.disabled = false;
    btn.textContent = '开始上传';
    const successCount = batchUploadFiles.filter(f => f.status === 'success').length;
    const errorCount = batchUploadFiles.filter(f => f.status === 'error').length;
    if (successCount > 0) {
        showToast(`成功上传 ${successCount} 个文档`, 'success');
        refreshActiveRagData();
    }
    if (errorCount > 0) {
        showToast(`${errorCount} 个文档上传失败`, 'warning');
    }
}

function readFileAsText(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(reader.error);
        reader.readAsText(file, 'UTF-8');
    });
}


// ============ RAG Chat ============
let ragChatHistory = [];

function initRagChat() {
    ragChatHistory = [];
    const container = document.getElementById('rag-chat-messages');
    container.innerHTML = `
        <div class="chat-message assistant">
            <div class="chat-avatar">${iconize("🤖")}</div>
            <div class="chat-bubble">
                你好！我是基于知识库的 AI 助手。你可以问我任何关于知识库中的问题，我会基于知识内容给你准确的回答并标注来源。
            </div>
        </div>
    `;
}

function handleChatKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendRagChat();
    }
}

async function sendRagChat() {
    const input = document.getElementById('rag-chat-input');
    const query = input.value.trim();
    if (!query) return;

    const topK = parseInt(document.getElementById('rag-chat-topk').value) || 5;
    const useLlm = document.getElementById('rag-use-llm').checked;

    input.value = '';

    const container = document.getElementById('rag-chat-messages');
    container.innerHTML += `
        <div class="chat-message user">
            <div class="chat-avatar">${iconize("👤")}</div>
            <div class="chat-bubble">${escapeHtml(query)}</div>
        </div>
    `;
    container.scrollTop = container.scrollHeight;

    container.innerHTML += `
        <div class="chat-message assistant" id="rag-chat-loading">
            <div class="chat-avatar">${iconize("🤖")}</div>
            <div class="chat-bubble">
                <span class="loading"></span> 思考中...
            </div>
        </div>
    `;
    container.scrollTop = container.scrollHeight;

    try {
    const result = await api.kbChat(query, topK, useLlm);

    document.getElementById('rag-chat-loading')?.remove();

    if (!result || !result.success) {
        container.innerHTML += `
            <div class="chat-message assistant">
                <div class="chat-avatar">${iconize("🤖")}</div>
                <div class="chat-bubble">抱歉，查询失败了，请稍后再试。</div>
            </div>
        `;
        container.scrollTop = container.scrollHeight;
        return;
    }

    let answerHtml = '';
    let modeBadgeHtml = '';
    if (result.answer && result.llm_status === 'success') {
        // LLM 生成成功
        modeBadgeHtml = `<div class="chat-mode-badge chat-mode-badge-success"><i class="fa-solid fa-wand-magic-sparkles"></i> AI 生成</div>`;
        answerHtml = simpleMarkdown(result.answer);
    } else if (result.results && result.results.length > 0) {
        // 检索模式：按 llm_status 区分文案
        const status = result.llm_status || 'skipped';
        let introText = '';
        let badgeIcon = 'fa-magnifying-glass';
        let badgeClass = '';
        if (status === 'unavailable') {
            // 想用 LLM 但没配 API Key
            introText = `<strong>LLM 不可用：</strong>${escapeHtml(result.llm_skip_reason || '未配置 API Key')}。以下是根据知识库检索到的 <strong>${result.results.length}</strong> 条相关片段：`;
            badgeIcon = 'fa-triangle-exclamation';
            badgeClass = 'chat-mode-badge-warn';
        } else if (status === 'failed') {
            // LLM 调用失败
            introText = `<strong>LLM 调用失败：</strong>${escapeHtml(result.llm_skip_reason || '未知错误')}。以下是根据知识库检索到的 <strong>${result.results.length}</strong> 条相关片段：`;
            badgeIcon = 'fa-triangle-exclamation';
            badgeClass = 'chat-mode-badge-error';
        } else {
            // skipped（用户明确未启用 LLM）
            introText = `未启用 LLM 生成回答，已根据知识库检索到 <strong>${result.results.length}</strong> 条相关片段：`;
        }
        if (badgeClass) {
            modeBadgeHtml = `<div class="chat-mode-badge ${badgeClass}"><i class="fa-solid ${badgeIcon}"></i> 检索模式</div>`;
        } else {
            modeBadgeHtml = `<div class="chat-mode-badge"><i class="fa-solid ${badgeIcon}"></i> 检索模式</div>`;
        }
        answerHtml = `<div class="chat-answer-intro">${introText}</div>`;
    } else {
        answerHtml = '<div class="chat-answer-intro">未找到相关的知识内容。</div>';
    }

    let sourcesHtml = '';
    if (result.sources && result.sources.length > 0) {
        sourcesHtml = '<div class="chat-sources">';
        sourcesHtml += '<div class="chat-sources-title">参考来源</div>';
        result.sources.forEach((s, i) => {
            const simPercent = (s.similarity * 100).toFixed(1);
            sourcesHtml += `
                <div class="chat-source-item">
                    <span class="source-index">[${i + 1}]</span>
                    <span class="chat-source-title">${escapeHtml(s.title)}</span>
                    <span class="source-score">${simPercent}%</span>
                </div>
            `;
        });
        sourcesHtml += '</div>';
    }

    container.innerHTML += `
        <div class="chat-message assistant">
            <div class="chat-avatar">${iconize("🤖")}</div>
            <div class="chat-bubble">
                ${modeBadgeHtml}
                ${answerHtml}
                ${sourcesHtml}
            </div>
        </div>
    `;
    container.scrollTop = container.scrollHeight;

    const sidebarSources = document.getElementById('rag-chat-sources-sidebar');
    if (result.results && result.results.length > 0) {
        sidebarSources.innerHTML = result.results.map((r, i) => `
            <div class="rag-result-card">
                <div class="rag-result-header">
                    <span class="rag-result-rank">#${i + 1}</span>
                    <span class="rag-result-score">${(r.similarity * 100).toFixed(1)}%</span>
                </div>
                <div class="rag-result-title">${escapeHtml(r.title)}</div>
                <div class="rag-result-content">${escapeHtml(r.content)}</div>
                <div class="similarity-bar">
                    <div class="similarity-bar-fill ${r.similarity > 0.7 ? 'high' : r.similarity > 0.5 ? 'medium' : ''}" style="width: ${r.similarity * 100}%"></div>
                </div>
            </div>
        `).join('');
    } else {
        sidebarSources.innerHTML = `
            <div class="empty-state" style="padding: 24px 12px;">
                <div style="font-size: 32px; margin-bottom: 8px;">${iconize("🔎")}</div>
                <p style="font-size: 12px;">未找到相关内容</p>
            </div>
        `;
    }
    } catch (e) {
        console.error('sendRagChat failed:', e);
        document.getElementById('rag-chat-loading')?.remove();
        showToast('查询失败', 'error');
    }
}


// ============ RAG Memory Tab ============

// 记忆筛选状态（按范围 / 对象类型 / 具体人 / 关键词）
let memoryFilters = { scope: 'all', object_type: 'all', sender: '', keyword: '' };
let _memoryFilterInitialized = false;
let _memoryModalMode = 'add';   // 'add' | 'edit'
let _memoryModalEditId = null;

async function initMemoryFilters() {
    if (!_memoryFilterInitialized) {
        const scopeSel = document.getElementById('memory-filter-scope');
        const typeSel = document.getElementById('memory-filter-type');
        const senderSel = document.getElementById('memory-filter-sender');
        const kwInput = document.getElementById('memory-filter-keyword');
        const applyBtn = document.getElementById('memory-filter-apply');
        const resetBtn = document.getElementById('memory-filter-reset');
        if (scopeSel) scopeSel.addEventListener('change', () => applyMemoryFilters());
        if (typeSel) typeSel.addEventListener('change', () => applyMemoryFilters());
        if (senderSel) senderSel.addEventListener('change', () => applyMemoryFilters());
        if (kwInput) kwInput.addEventListener('keydown', e => { if (e.key === 'Enter') applyMemoryFilters(); });
        if (applyBtn) applyBtn.addEventListener('click', () => applyMemoryFilters());
        if (resetBtn) resetBtn.addEventListener('click', () => resetMemoryFilters());
        _memoryFilterInitialized = true;
    }
    await loadMemoryFacets();
    await loadClassifySpec();
}

function applyMemoryFilters() {
    const scopeSel = document.getElementById('memory-filter-scope');
    const typeSel = document.getElementById('memory-filter-type');
    const senderSel = document.getElementById('memory-filter-sender');
    const kwInput = document.getElementById('memory-filter-keyword');
    memoryFilters.scope = scopeSel ? scopeSel.value : 'all';
    memoryFilters.object_type = typeSel ? typeSel.value : 'all';
    memoryFilters.sender = senderSel ? senderSel.value : '';
    memoryFilters.keyword = kwInput ? kwInput.value.trim() : '';
    loadMemoryList();
}

function resetMemoryFilters() {
    memoryFilters = { scope: 'all', object_type: 'all', sender: '', keyword: '' };
    const scopeSel = document.getElementById('memory-filter-scope');
    const typeSel = document.getElementById('memory-filter-type');
    const senderSel = document.getElementById('memory-filter-sender');
    const kwInput = document.getElementById('memory-filter-keyword');
    if (scopeSel) scopeSel.value = 'all';
    if (typeSel) typeSel.value = 'all';
    if (senderSel) senderSel.value = '';
    if (kwInput) kwInput.value = '';
    loadMemoryList();
}

async function loadMemoryFacets() {
    try {
        const facets = await api.getMemoryFacets();
        if (!facets) return;
        const sel = document.getElementById('memory-filter-sender');
        if (!sel) return;
        const current = sel.value;
        let html = '<option value="">全部</option>';
        (facets.people || []).forEach(p => {
            const name = p.sender_name || p.sender_id || '未知';
            const label = `${name} (${p.count})`;
            html += `<option value="${escapeHtml(p.sender_id)}">${escapeHtml(label)}</option>`;
        });
        sel.innerHTML = html;
        if (current) sel.value = current;
    } catch (e) {
        console.warn('加载记忆 facets 失败', e);
    }
}

async function loadClassifySpec() {
    const box = document.getElementById('classify-spec-content');
    if (!box) return;
    try {
        const data = await api.getMemoryClassifySpec();
        const spec = (data && data.spec) || [];
        if (!spec.length) { box.innerHTML = '<span class="hint">暂无规则说明</span>'; return; }
        box.innerHTML = spec.map(r => `
            <div class="memory-rule-item">
                <span class="memory-rule-order">${r.order}</span>
                <div class="memory-rule-text">
                    <strong>${escapeHtml(r.name)}</strong>：${escapeHtml(r.rule)}
                    ${r.example ? `<span class="rule-hint">示例：${escapeHtml(r.example)}</span>` : ''}
                </div>
            </div>`).join('');
    } catch (e) {
        box.innerHTML = '<span class="hint">规则加载失败</span>';
    }
}

async function loadMemoryList() {
    const tbody = document.getElementById('memory-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">加载中…</td></tr>';
    try {
    const data = await api.getMemories({
        limit: 200,
        scope: memoryFilters.scope,
        object_type: memoryFilters.object_type,
        sender: memoryFilters.sender,
        keyword: memoryFilters.keyword,
    });
    if (!data || !data.memories || data.memories.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">暂无记忆数据</td></tr>';
        return;
    }
    tbody.innerHTML = data.memories.map(m => {
        const content = escapeHtml(m.content || '').substring(0, 200);
        const source = escapeHtml(m.source || '-');
        const chatId = escapeHtml((m.chat_id || '').substring(0, 12));
        const createdAt = escapeHtml(m.created_at || '-');
        const objType = m.object_type || 'other';
        let objName = m.sender_name || m.chat_name || '';
        if (!objName || objType === 'other') {
            objName = '其他';
        }
        const objClass = objType === 'person' ? 'tag-blue' : objType === 'group' ? 'tag-green' : 'tag-gray';
        const objTag = `<span class="tag ${objClass}">${escapeHtml(objName)}</span>`;
        // 范围（个人 / 公共）清晰区分：公共琥珀色徽章 + 行左侧强调条
        const scope = m.scope || 'personal';
        const scopeLabel = scope === 'public' ? '公共' : '个人';
        const scopeClass = scope === 'public' ? 'tag-amber' : 'tag-blue';
        const scopeTag = `<span class="tag ${scopeClass}">${scopeLabel}</span>`;
        const rowClass = scope === 'public' ? 'memory-row-public' : '';
        return `<tr class="${rowClass}" data-id="${m.id}">
            <td>${m.id}</td>
            <td class="memory-content-cell" title="${escapeHtml(m.content || '')}">${content}${(m.content||'').length > 200 ? '…' : ''}</td>
            <td>${scopeTag}</td>
            <td><span class="tag tag-gray">${source}</span></td>
            <td>${objTag}</td>
            <td>${chatId}</td>
            <td>${createdAt}</td>
            <td>
                <div class="action-btns">
                    <button class="btn btn-sm btn-outline-secondary" onclick="editMemory(${m.id})" title="编辑"><i class="fa-solid fa-pen-to-square"></i></button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteMemoryConfirm(${m.id})" title="删除"><i class="fa-solid fa-trash"></i></button>
                </div>
            </td>
        </tr>`;
    }).join('');
    } catch (e) {
        console.error('loadMemoryList failed:', e);
        tbody.innerHTML = '<tr><td colspan="8" class="empty-cell">加载失败，请稍后重试</td></tr>';
    }
}

// ============ 记忆新增 / 编辑 模态框 ============
const MEMORY_SCOPE_HINTS = {
    personal: '个人记忆：仅在该对话人上下文中召回（点对点）。',
    public: '公共记忆：团队/公司级共享知识，向所有对话人召回。',
};

function showMemoryModal(mode = 'add', id = null) {
    _memoryModalMode = mode;
    _memoryModalEditId = id;
    const titleEl = document.getElementById('memory-modal-title');
    const contentEl = document.getElementById('memory-modal-content');
    const scopeEl = document.getElementById('memory-modal-scope');
    const sourceEl = document.getElementById('memory-modal-source');
    const hintEl = document.getElementById('memory-modal-scope-hint');
    if (mode === 'edit') {
        titleEl.textContent = '编辑记忆';
        const row = document.querySelector(`#memory-tbody tr[data-id="${id}"]`);
        const oldContent = row ? (row.querySelector('.memory-content-cell').getAttribute('title') || '') : '';
        contentEl.value = oldContent;
        const oldScope = row ? (row.querySelector('.tag-amber') ? 'public' : 'personal') : 'personal';
        scopeEl.value = oldScope;
        sourceEl.value = '';
    } else {
        titleEl.textContent = '新增记忆';
        contentEl.value = '';
        scopeEl.value = 'personal';
        sourceEl.value = '';
    }
    if (hintEl) hintEl.textContent = MEMORY_SCOPE_HINTS[scopeEl.value] || '';
    scopeEl.onchange = () => { if (hintEl) hintEl.textContent = MEMORY_SCOPE_HINTS[scopeEl.value] || ''; };
    document.getElementById('memory-modal').classList.add('active');
}

function closeMemoryModal() {
    document.getElementById('memory-modal').classList.remove('active');
}

async function submitMemoryModal() {
    const contentEl = document.getElementById('memory-modal-content');
    const scopeEl = document.getElementById('memory-modal-scope');
    const sourceEl = document.getElementById('memory-modal-source');
    const content = (contentEl.value || '').trim();
    if (!content) {
        showToast('请输入记忆内容', 'error');
        return;
    }
    const scope = scopeEl.value;
    const source = sourceEl.value.trim();
    const saveBtn = document.getElementById('memory-modal-save');
    if (saveBtn) saveBtn.disabled = true;
    try {
        let result;
        if (_memoryModalMode === 'edit' && _memoryModalEditId != null) {
            result = await api.updateMemory(_memoryModalEditId, content, scope);
        } else {
            result = await api.addMemory(content, source, '', scope);
        }
        if (result && result.success) {
            showToast(_memoryModalMode === 'edit' ? '记忆已更新' : '记忆已添加');
            closeMemoryModal();
            loadMemoryList();
            loadMemoryFacets();
        } else {
            showToast(result?.message || '操作失败', 'error');
        }
    } catch (e) {
        showToast('操作失败：' + (e?.message || e), 'error');
    } finally {
        if (saveBtn) saveBtn.disabled = false;
    }
}

function editMemory(id) {
    showMemoryModal('edit', id);
}

async function deleteMemoryConfirm(id) {
    if (!confirm('确定删除这条记忆？此操作不可撤销。')) return;
    try {
    const result = await api.deleteMemory(id);
    if (result && result.success) {
        showToast('记忆已删除');
        loadMemoryList();
        loadMemoryFacets();
    } else {
        showToast(result?.message || '删除失败', 'error');
    }
    } catch (e) {
        console.error('deleteMemoryConfirm failed:', e);
        showToast('删除失败', 'error');
    }
}


// ============ RAG Settings ============
async function loadRagSettings() {
    // Embedding 状态已移到概览页显示
    try {
    const data = await api.getStatus();
    if (data && data.config) {
        const modelEl = document.getElementById('rag-display-embedding-model');
        const statusEl = document.getElementById('rag-display-embedding-status');
        if (modelEl) modelEl.textContent = data.config.embedding_model || '-';
        if (statusEl) {
            statusEl.textContent = data.config.embedding_enabled ? '已启用' : '未启用';
            statusEl.className = `status-badge ${data.config.embedding_enabled ? 'success' : 'warning'}`;
        }
    }
    } catch (e) {
        console.error('loadRagSettings failed:', e);
    }
}

function saveChunkConfig() {
    // 分块配置已移到系统配置页，统一保存
    switchPage('config');
}

async function rechunkAllDocs() {
    if (!confirm('确定要重新分块所有文档吗？这会根据当前的分块大小/重叠设置，删除所有旧分块并重新生成。')) return;

    const btn = document.getElementById('rechunk-all-btn');
    if (!btn) return;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> 重新分块中...';
    
    try {
        const result = await api.rechunkAllDocs();
        if (result && result.success) {
            showToast(result.message);
            refreshActiveRagData();  // 刷新当前视图
        } else {
            showToast(result?.message || '重新分块失败', 'error');
        }
    } catch (e) {
        showToast('重新分块失败：' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-rotate-right"></i> 重新分块所有文档';
    }
}

// 订阅全局平台变化：切换平台时同步钉钉/飞书导入按钮显隐，避免手动刷新页面
if (window.store && typeof window.store.subscribe === 'function') {
    window.store.subscribe('platform', syncImportButtonsByPlatform);
}

// 显式挂到 window：经典 <script> 中顶层 function 本就是全局，这里仅为在 ESM/测试环境下也能访问，
// 便于 vitest 对纯函数做单元断言（浏览器内等价于 no-op）。
if (typeof window !== 'undefined') {
    window.formatSource = formatSource;
    window.formatDocType = formatDocType;
    window.loadRagPage = loadRagPage;
}

