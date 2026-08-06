// ============ pages/keywords.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ 动效辅助 ============
// 数字 count-up（easeOutCubic）
function animateCount(el, target) {
    if (!el) return;
    const dur = 600;
    const start = performance.now();
    function tick(now) {
        const t = Math.min(1, (now - start) / dur);
        const eased = 1 - Math.pow(1 - t, 3);
        el.textContent = Math.round(target * eased);
        if (t < 1) requestAnimationFrame(tick);
        else el.textContent = target;
    }
    requestAnimationFrame(tick);
}

// 高频命中横向条形图
// 按命中次数降序排列，展示 Top N 关键词的命中对比
function renderKwHeatmap(topHits) {
    const container = document.getElementById('kw-heatmap');
    if (!container) return;
    if (!topHits || topHits.length === 0) {
        container.innerHTML = '<div class="kw-bar-empty">暂无命中数据</div>';
        return;
    }
    const sorted = [...topHits].sort((a, b) => (b.hit_count || 0) - (a.hit_count || 0));
    const maxHit = Math.max(...sorted.map(k => k.hit_count || 0), 1);
    const top10 = sorted.slice(0, 10);

    const rows = top10.map((k, i) => {
        const v = k.hit_count || 0;
        const pct = (v / maxHit) * 100;
        const rankClass = i === 0 ? 'kw-bar-rank top1' : i === 1 ? 'kw-bar-rank top2' : i === 2 ? 'kw-bar-rank top3' : 'kw-bar-rank';
        const title = `${k.match_pattern || '—'} · ${v} 次`;
        return `
            <div class="kw-bar-row" style="animation-delay:${i * 0.06}s">
                <span class="${rankClass}">${i + 1}</span>
                <span class="kw-bar-label" title="${escapeHtml(title)}">${escapeHtml(k.match_pattern || '—')}</span>
                <div class="kw-bar-track">
                    <div class="kw-bar-fill" style="width:${pct}%"></div>
                </div>
                <span class="kw-bar-count">${v}</span>
            </div>`;
    }).join('');

    container.innerHTML = rows;
}

// ============ Keywords ============
function switchKwTab(tab) {
    document.querySelectorAll('#page-keywords .section-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    document.querySelectorAll('#page-keywords .section-tab-content').forEach(c => {
        const isActive = c.id === `kw-tab-${tab}`;
        c.classList.toggle('active', isActive);
        c.style.display = isActive ? '' : 'none';
    });
}
window.switchKwTab = switchKwTab;
async function loadKeywords() {
    switchKwTab('rules');
    const search = document.getElementById('kw-search').value;
    const typeFilter = document.getElementById('kw-type-filter').value;
    const tbody = document.getElementById('kw-body');
    try {
        const data = await api.getKeywords("", search);
        if (!data || data.error) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty-cell" style="text-align:center;">加载失败，请重试</td></tr>';
            showToast('关键词列表加载失败', 'error');
            return;
        }

        try {
            const stats = await api.getKeywordStats();
            if (stats) {
                animateCount(document.getElementById('kw-stat-total'), stats.total || 0);
                animateCount(document.getElementById('kw-stat-enabled'), stats.enabled || 0);
                const totalHits = stats.top_hits?.reduce((sum, k) => sum + (k.hit_count || 0), 0) || 0;
                animateCount(document.getElementById('kw-stat-hits'), totalHits);
                renderKwHeatmap(stats.top_hits || []);
            }
        } catch (e) {
            console.error('关键词统计加载失败:', e);
        }

        let rules = data.rules || [];
        if (typeFilter) {
            rules = rules.filter(r => r.match_type === typeFilter);
        }

        if (rules.length === 0) {
            tbody.innerHTML = '<tr><td colspan="8" class="empty-cell" style="text-align:center;">暂无规则，点击"新建规则"开始添加</td></tr>';
            return;
        }

        tbody.innerHTML = rules.map((rule, i) => `
        <tr style="--i:${i}">
            <td><input type="checkbox" class="kw-checkbox" data-id="${rule.id}" ${selectedKeywordIds.has(rule.id) ? 'checked' : ''} onchange="toggleKwSelect(${rule.id})"></td>
            <td><code class="pattern-code">${escapeHtml(rule.match_pattern)}</code></td>
            <td><span class="tag ${rule.match_type === 'fuzzy' ? 'tag-green' : rule.match_type === 'exact' ? 'tag-orange' : 'tag-purple'}">
                ${rule.match_type === 'fuzzy' ? '模糊匹配' : rule.match_type === 'exact' ? '精确匹配' : '正则匹配'}
            </span></td>
            <td class="reply-cell">${escapeHtml(rule.reply_text)}</td>
            <td><span class="priority-badge">${rule.priority}</span></td>
            <td>${rule.hit_count || 0}</td>
            <td>
                <span class="status-badge ${rule.enabled ? 'success' : 'error'}">
                    ${rule.enabled ? '启用' : '禁用'}
                </span>
            </td>
            <td>
                <div class="action-btns">
                    <button class="btn btn-sm btn-outline-secondary" onclick="editKeyword(${rule.id})" title="编辑"><i class="fa-solid fa-pen-to-square"></i></button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteKeyword(${rule.id})" title="删除"><i class="fa-solid fa-trash"></i></button>
                </div>
            </td>
        </tr>
    `).join('');
    updateKwBulkActions();
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-cell" style="text-align:center;">加载失败: ' + escapeHtml(e.message || String(e)) + '</td></tr>';
        showToast('关键词列表加载失败', 'error');
    }
}

function toggleAllKwSelect(checkbox) {
    const checkboxes = document.querySelectorAll('.kw-checkbox');
    if (checkbox.checked) {
        checkboxes.forEach(cb => {
            const id = parseInt(cb.dataset.id);
            selectedKeywordIds.add(id);
            cb.checked = true;
        });
    } else {
        selectedKeywordIds.clear();
        checkboxes.forEach(cb => cb.checked = false);
    }
    updateKwBulkActions();
}

function toggleKwSelect(id) {
    if (selectedKeywordIds.has(id)) {
        selectedKeywordIds.delete(id);
    } else {
        selectedKeywordIds.add(id);
    }
    // sync checkbox state
    const cb = document.querySelector('.kw-checkbox[data-id="' + id + '"]');
    if (cb) cb.checked = selectedKeywordIds.has(id);
    updateKwBulkActions();
}

// Show/hide bulk action buttons: visible when >= 1 item selected
function updateKwBulkActions() {
    const el = document.getElementById('kw-bulk-actions');
    if (!el) return;
    el.style.display = selectedKeywordIds.size >= 1 ? '' : 'none';
}

function showKeywordModal(rule = null) {
    document.getElementById('kw-modal-id').value = rule?.id || '';
    document.getElementById('kw-modal-title').textContent = rule ? '编辑关键词规则' : '新建关键词规则';
    document.getElementById('kw-modal-pattern').value = rule?.match_pattern || '';
    document.getElementById('kw-modal-reply').value = rule?.reply_text || '';
    document.getElementById('kw-modal-type').value = rule?.match_type || 'fuzzy';
    document.getElementById('kw-modal-priority').value = rule?.priority || 0;
    document.getElementById('kw-modal-enabled').checked = rule ? rule.enabled !== 0 : true;
    document.getElementById('keyword-modal').classList.add('active');
}

function closeKeywordModal() {
    document.getElementById('keyword-modal').classList.remove('active');
}

async function saveKeyword() {
    if (window.__kwSaving) return;  // 防双击重复提交创建重复规则
    const id = document.getElementById('kw-modal-id').value;
    const data = {
        match_pattern: document.getElementById('kw-modal-pattern').value.trim(),
        reply_text: document.getElementById('kw-modal-reply').value.trim(),
        match_type: document.getElementById('kw-modal-type').value,
        priority: parseInt(document.getElementById('kw-modal-priority').value) || 0,
        enabled: document.getElementById('kw-modal-enabled').checked ? 1 : 0,
    };

    if (!data.match_pattern || !data.reply_text) {
        showToast('匹配模式和回复内容不能为空', 'warning');
        return;
    }

    window.__kwSaving = true;
    try {
    let result;
    if (id) {
        result = await api.updateKeyword(parseInt(id), data);
    } else {
        result = await api.addKeyword(data);
    }

    if (result && result.success) {
        showToast(result.message || '保存成功');
        closeKeywordModal();
        loadKeywords();
    } else {
        showToast(result?.message || '保存失败', 'error');
    }
    } catch (e) {
        console.error('saveKeyword failed:', e);
        showToast('保存失败', 'error');
    } finally {
        window.__kwSaving = false;
    }
}

async function editKeyword(id) {
    try {
    const data = await api.getKeyword(id);
    if (data && data.rule) {
        showKeywordModal(data.rule);
    }
    } catch (e) {
        console.error('editKeyword failed:', e);
        showToast('加载失败', 'error');
    }
}

async function deleteKeyword(id) {
    if (!confirm('确定删除这条规则吗？')) return;
    try {
    const result = await api.deleteKeyword(id);
    if (result && result.success) {
        showToast('删除成功');
        loadKeywords();
    } else {
        showToast(result?.message || '删除失败', 'error');
    }
    } catch (e) {
        console.error('deleteKeyword failed:', e);
        showToast('删除失败', 'error');
    }
}

async function batchEnable() {
    if (selectedKeywordIds.size === 0) {
        showToast('请先选择规则', 'warning');
        return;
    }
    try {
    const result = await api.batchKeywords(Array.from(selectedKeywordIds), 'enable');
    if (result && result.success) {
        showToast(result.message);
        selectedKeywordIds.clear();
        loadKeywords();
    }
    } catch (e) {
        console.error('batchEnable failed:', e);
        showToast('批量启用失败', 'error');
    }
}

async function batchDisable() {
    if (selectedKeywordIds.size === 0) {
        showToast('请先选择规则', 'warning');
        return;
    }
    try {
    const result = await api.batchKeywords(Array.from(selectedKeywordIds), 'disable');
    if (result && result.success) {
        showToast(result.message);
        selectedKeywordIds.clear();
        loadKeywords();
    }
    } catch (e) {
        console.error('batchDisable failed:', e);
        showToast('批量禁用失败', 'error');
    }
}

async function batchDelete() {
    if (selectedKeywordIds.size === 0) {
        showToast('请先选择规则', 'warning');
        return;
    }
    if (!confirm(`确定删除选中的 ${selectedKeywordIds.size} 条规则吗？`)) return;
    try {
    const result = await api.batchKeywords(Array.from(selectedKeywordIds), 'delete');
    if (result && result.success) {
        showToast(result.message);
        selectedKeywordIds.clear();
        loadKeywords();
    }
    } catch (e) {
        console.error('batchDelete failed:', e);
        showToast('批量删除失败', 'error');
    }
}

function showImportModal() {
    document.getElementById('import-modal').classList.add('active');
    document.getElementById('import-result').style.display = 'none';
    document.getElementById('import-preview').style.display = 'none';
    document.getElementById('upload-area').style.display = 'block';
}

function closeImportModal() {
    document.getElementById('import-modal').classList.remove('active');
}

let currentImportFile = null;

async function handleImportFile(event) {
    const file = event.target.files[0];
    if (!file) return;
    
    currentImportFile = file;
    
    const resultDiv = document.getElementById('import-result');
    const previewDiv = document.getElementById('import-preview');
    const uploadArea = document.getElementById('upload-area');
    const previewList = document.getElementById('import-preview-list');
    const previewCount = document.getElementById('import-preview-count');
    
    resultDiv.style.display = 'none';
    uploadArea.style.display = 'none';
    previewDiv.style.display = 'block';
    
    try {
        const text = await file.text();
        let count = 0;
        
        if (file.name.endsWith('.json')) {
            const data = JSON.parse(text);
            count = Array.isArray(data) ? data.length : 0;
        } else if (file.name.endsWith('.csv')) {
            const lines = text.split('\n').filter(line => line.trim());
            count = lines.length - 1;
        } else {
            const lines = text.split('\n').filter(line => line.trim());
            count = lines.length;
        }
        
        previewCount.textContent = `${count} 条`;
        previewList.innerHTML = `<p>文件: ${escapeHtml(file.name)}</p><p>预计导入 ${count} 条规则</p>`;
    } catch (e) {
        previewList.innerHTML = `<p class="text-danger">文件解析失败: ${escapeHtml(e.message)}</p>`;
        previewCount.textContent = '0 条';
    }
}

async function confirmImport() {
    if (!currentImportFile) return;
    
    const resultDiv = document.getElementById('import-result');
    const resultText = document.getElementById('import-result-text');
    const previewDiv = document.getElementById('import-preview');
    
    resultDiv.style.display = 'block';
    previewDiv.style.display = 'none';
    
    try {
        const result = await api.importKeywords(currentImportFile);
        
        if (result && result.success) {
            resultText.className = 'alert alert-success';
            resultText.textContent = result.message;
            loadKeywords();
        } else {
            resultText.className = 'alert alert-error';
            resultText.textContent = result?.message || '导入失败';
        }
    } catch (e) {
        resultText.className = 'alert alert-error';
        resultText.textContent = '导入失败: ' + e.message;
    }
    
    currentImportFile = null;
}

function clearImportPreview() {
    document.getElementById('import-preview').style.display = 'none';
    document.getElementById('upload-area').style.display = 'block';
    document.getElementById('import-file').value = '';
}

async function exportKeywords() {
    try {
        await api.exportKeywords('');
    } catch (e) {
        showToast('导出失败：' + (e && e.message ? e.message : e), 'error');
    }
}

function clearKwTest() {
    document.getElementById('kw-test-text').value = '';
    document.getElementById('kw-test-result').innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">${iconize("🧪")}</div>
            <h3>等待测试</h3>
            <p>输入文本，测试关键词匹配效果</p>
        </div>
    `;
}

async function testMatch() {
    const text = document.getElementById('kw-test-text').value.trim();
    if (!text) {
        showToast('请输入测试文本', 'warning');
        return;
    }
    const container = document.getElementById('kw-test-result');
    try {
        const result = await api.testKeywordMatch(text);
        if (!result || !result.success) {
            container.innerHTML = '<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#ef4444;"></i><p style="margin-top:.5rem;">测试失败</p></div>';
            return;
        }

    if (result.hit_count === 0) {
        container.innerHTML = `
            <div class="test-result-none">
                <div style="font-size: 32px; margin-bottom: 8px;">${iconize("😕")}</div>
                <p>未匹配到任何规则</p>
            </div>
        `;
    } else {
        container.innerHTML = `
            <div class="test-result-header">
                <span class="match-count">命中 ${result.hit_count} 条规则</span>
                <span class="top-reply">最优回复：${escapeHtml(result.top_reply)}</span>
            </div>
            <div class="test-result-list">
                ${result.matched.map(r => `
                    <div class="test-result-item">
                        <div class="test-result-pattern">
                            <code>${escapeHtml(r.match_pattern)}</code>
                            <span class="tag ${r.match_type === 'fuzzy' ? 'tag-green' : r.match_type === 'exact' ? 'tag-orange' : 'tag-purple'}">
                                ${r.match_type === 'fuzzy' ? '模糊' : r.match_type === 'exact' ? '精确' : '正则'}
                            </span>
                            <span class="priority-badge">优先级 ${r.priority}</span>
                        </div>
                        <div class="test-result-reply">${escapeHtml(r.reply_text)}</div>
                    </div>
                `).join('')}
            </div>
        `;
    }
    } catch (e) {
        container.innerHTML = '<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#ef4444;"></i><p style="margin-top:.5rem;">测试失败: ' + escapeHtml(e.message || String(e)) + '</p></div>';
        showToast('关键词匹配测试失败', 'error');
    }
}

