// ============ pages/gate_rules.js ============
// 答复门禁规则管理：增删改查 / 启用停用 / 命中测试
// 由 app.js 按页拆分（P2-13），逻辑未改动；全局函数经 data-action 或 inline onclick 调用

// 选中的规则 id 集合（批量操作）
const selectedGateRuleIds = new Set();

// 客户端分页：每页条数 + 当前页（1 起）+ 当前筛选后的全量列表
const GATE_PAGE_SIZE = 15;
let gatePage = 1;
let gateRulesFiltered = [];

const GATE_PRESET_CATEGORIES = [
    { category: 'private_privacy', label: '私人隐私' },
    { category: 'illegal_info', label: '违法信息' },
    { category: 'negative_emotion', label: '负面情绪' },
    { category: 'other', label: '其他' },
];

// ============ 标签页切换 ============
function switchGateTab(tab) {
    document.querySelectorAll('#page-gate-rules .section-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    document.querySelectorAll('#page-gate-rules .section-tab-content').forEach(c => {
        const isActive = c.id === `gate-tab-${tab}`;
        c.classList.toggle('active', isActive);
        c.style.display = isActive ? '' : 'none';
    });
}
window.switchGateTab = switchGateTab;

// ============ 列表加载 ============
async function loadGateRules(resetPage = true) {
    switchGateTab('rules');
    const search = document.getElementById('gate-search').value;
    const catFilter = document.getElementById('gate-cat-filter').value;
    const typeFilter = document.getElementById('gate-type-filter').value;
    if (resetPage) gatePage = 1; // 搜索/筛选变更回到第一页；删除/保存等数据操作保持当前页
    const tbody = document.getElementById('gate-body');
    try {
        const data = await api.getGateRules(catFilter, search);
        if (!data || data.error) {
            tbody.innerHTML = '<tr><td colspan="10" class="empty-cell" style="text-align:center;">加载失败，请重试</td></tr>';
            showToast('门禁规则加载失败', 'error');
            return;
        }

        try {
            const stats = await api.getGateRuleStats();
            if (stats) {
                animateGateCount(document.getElementById('gate-stat-total'), stats.total || 0);
                animateGateCount(document.getElementById('gate-stat-enabled'), stats.enabled || 0);
                const totalHits = stats.top_hits?.reduce((sum, k) => sum + (k.hit_count || 0), 0) || 0;
                animateGateCount(document.getElementById('gate-stat-hits'), totalHits);
                renderGateTopHits(stats.top_hits || []);
            }
        } catch (e) {
            console.error('门禁统计加载失败:', e);
        }

        // 分类下拉（保持与后台分类同步）
        const preset = (data.preset_categories || []).map(c => ({ category: c.category, label: c.category_label }));
        _fillGateCatFilter(preset, catFilter);

        let rules = data.rules || [];
        if (typeFilter) {
            rules = rules.filter(r => (r.match_type || 'keyword') === typeFilter);
        }

        gateRulesFiltered = rules;
        renderGateTablePage();
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-cell" style="text-align:center;">加载失败: ' + escapeHtml(e.message || String(e)) + '</td></tr>';
        showToast('门禁规则加载失败', 'error');
    }
}
window.loadGateRules = loadGateRules;

// 按当前页渲染表格（客户端分页，复用 app.js 的 renderPager）
function renderGateTablePage() {
    const tbody = document.getElementById('gate-body');
    if (!tbody) return;
    const total = gateRulesFiltered.length;
    const totalPages = Math.max(1, Math.ceil(total / GATE_PAGE_SIZE));
    if (gatePage > totalPages) gatePage = totalPages;
    const start = (gatePage - 1) * GATE_PAGE_SIZE;
    const pageRules = gateRulesFiltered.slice(start, start + GATE_PAGE_SIZE);

    if (total === 0) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-cell" style="text-align:center;">暂无规则，点击"新建"开始添加</td></tr>';
        if (typeof renderPager === 'function') renderPager('gate-pager', { total: 0, page: 1, pageSize: GATE_PAGE_SIZE }, null);
        updateGateBulkActions();
        return;
    }

    tbody.innerHTML = pageRules.map((rule, i) => {
        const cat = rule.category || 'other';
        const catLabel = rule.category_label || cat;
        const type = rule.match_type || 'keyword';
        const typeTag = type === 'regex'
            ? '<span class="tag tag-purple">正则</span>'
            : '<span class="tag tag-green">关键词</span>';
        return `
        <tr style="--i:${i}">
            <td><input type="checkbox" class="gate-checkbox" data-id="${rule.id}" ${selectedGateRuleIds.has(rule.id) ? 'checked' : ''} onchange="toggleGateSelect(${rule.id})"></td>
            <td><span class="tag gate-cat" data-cat="${escapeHtml(cat)}" title="${escapeHtml(catLabel)}">${escapeHtml(catLabel)}</span></td>
            <td class="gate-name-cell" title="${escapeHtml(rule.name || '')}">${escapeHtml(rule.name || '—')}</td>
            <td>${typeTag}</td>
            <td><code class="pattern-code" title="${escapeHtml(rule.pattern)}">${escapeHtml(rule.pattern)}</code></td>
            <td class="reply-cell" title="${escapeHtml(rule.intercept_message || '')}">${escapeHtml(rule.intercept_message || '—')}</td>
            <td><span class="priority-badge">${rule.priority}</span></td>
            <td>${rule.hit_count || 0}</td>
            <td>
                <span class="status-badge ${rule.enabled ? 'success' : 'error'}">
                    ${rule.enabled ? '启用' : '禁用'}
                </span>
            </td>
            <td>
                <div class="action-btns">
                    <button class="btn btn-sm btn-outline-secondary" onclick="editGateRule(${rule.id})" title="编辑"><i class="fa-solid fa-pen-to-square"></i></button>
                    <button class="btn btn-sm btn-outline-danger" onclick="deleteGateRule(${rule.id})" title="删除"><i class="fa-solid fa-trash"></i></button>
                </div>
            </td>
        </tr>`;
    }).join('');

    if (typeof renderPager === 'function') {
        renderPager('gate-pager', { total, page: gatePage, pageSize: GATE_PAGE_SIZE }, (p) => {
            gatePage = p;
            renderGateTablePage();
        });
    }
    updateGateBulkActions();
}

function _fillGateCatFilter(preset, current) {
    const sel = document.getElementById('gate-cat-filter');
    if (!sel) return;
    const opts = ['<option value="">全部分类</option>'];
    const seen = new Set();
    for (const c of preset) {
        if (seen.has(c.category)) continue;
        seen.add(c.category);
        const sel2 = c.category === current ? ' selected' : '';
        opts.push(`<option value="${escapeHtml(c.category)}"${sel2}>${escapeHtml(c.label)}</option>`);
    }
    sel.innerHTML = opts.join('');
}

// 数字 count-up
function animateGateCount(el, target) {
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

function renderGateTopHits(topHits) {
    const container = document.getElementById('gate-top-hits');
    if (!container) return;
    if (!topHits || topHits.length === 0) {
        container.innerHTML = '<div class="kw-bar-empty">暂无命中数据</div>';
        return;
    }
    const sorted = [...topHits].sort((a, b) => (b.hit_count || 0) - (a.hit_count || 0)).slice(0, 10);
    const maxHit = Math.max(...sorted.map(k => k.hit_count || 0), 1);
    container.innerHTML = sorted.map((k, i) => {
        const v = k.hit_count || 0;
        const pct = (v / maxHit) * 100;
        const rankClass = i === 0 ? 'kw-bar-rank top1' : i === 1 ? 'kw-bar-rank top2' : i === 2 ? 'kw-bar-rank top3' : 'kw-bar-rank';
        const name = k.name || k.category || '—';
        return `
            <div class="kw-bar-row" style="animation-delay:${i * 0.06}s">
                <span class="${rankClass}">${i + 1}</span>
                <span class="kw-bar-label" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
                <div class="kw-bar-track"><div class="kw-bar-fill" style="width:${pct}%"></div></div>
                <span class="kw-bar-count">${v}</span>
            </div>`;
    }).join('');
}

// ============ 选择 / 批量 ============
function toggleAllGateSelect(checkbox) {
    const boxes = document.querySelectorAll('.gate-checkbox');
    if (checkbox.checked) {
        boxes.forEach(cb => {
            const id = parseInt(cb.dataset.id);
            selectedGateRuleIds.add(id);
            cb.checked = true;
        });
    } else {
        selectedGateRuleIds.clear();
        boxes.forEach(cb => cb.checked = false);
    }
    updateGateBulkActions();
}
window.toggleAllGateSelect = toggleAllGateSelect;

function toggleGateSelect(id) {
    if (selectedGateRuleIds.has(id)) selectedGateRuleIds.delete(id);
    else selectedGateRuleIds.add(id);
    const cb = document.querySelector('.gate-checkbox[data-id="' + id + '"]');
    if (cb) cb.checked = selectedGateRuleIds.has(id);
    updateGateBulkActions();
}
window.toggleGateSelect = toggleGateSelect;

function updateGateBulkActions() {
    const el = document.getElementById('gate-bulk-actions');
    if (!el) return;
    el.style.display = selectedGateRuleIds.size >= 1 ? '' : 'none';
}

// ============ 弹窗（新建 / 编辑）============
function showGateRuleModal(rule = null) {
    document.getElementById('gate-modal-id').value = rule?.id || '';
    document.getElementById('gate-modal-title').textContent = rule ? '编辑门禁规则' : '新建门禁规则';
    document.getElementById('gate-modal-name').value = rule?.name || '';

    // 分类：预设选择；若为自定义分类则切到"自定义"并填入
    const catSel = document.getElementById('gate-modal-category');
    const customInput = document.getElementById('gate-modal-category-custom');
    const cat = rule?.category || 'other';
    const presetCats = GATE_PRESET_CATEGORIES.map(c => c.category);
    if (presetCats.includes(cat)) {
        catSel.value = cat;
        customInput.style.display = 'none';
        customInput.value = '';
    } else {
        catSel.value = '__custom__';
        customInput.style.display = '';
        customInput.value = cat;
    }

    document.getElementById('gate-modal-type').value = rule?.match_type || 'keyword';
    document.getElementById('gate-modal-pattern').value = rule?.pattern || '';
    document.getElementById('gate-modal-intercept').value = rule?.intercept_message || '';
    document.getElementById('gate-modal-priority').value = rule?.priority || 0;
    document.getElementById('gate-modal-enabled').checked = rule ? rule.enabled !== 0 : true;

    _onGateTypeChange();
    document.getElementById('gate-rule-modal').classList.add('active');
}
window.showGateRuleModal = showGateRuleModal;

function closeGateRuleModal() {
    document.getElementById('gate-rule-modal').classList.remove('active');
}
window.closeGateRuleModal = closeGateRuleModal;

// 分类选择切换自定义输入框
function onGateCategoryChange() {
    const sel = document.getElementById('gate-modal-category');
    const customInput = document.getElementById('gate-modal-category-custom');
    if (sel.value === '__custom__') {
        customInput.style.display = '';
        customInput.focus();
    } else {
        customInput.style.display = 'none';
        customInput.value = '';
    }
}
window.onGateCategoryChange = onGateCategoryChange;

// 命中方式切换：正则时提示语法
function _onGateTypeChange() {
    const type = document.getElementById('gate-modal-type').value;
    const hint = document.getElementById('gate-pattern-hint');
    if (!hint) return;
    hint.textContent = type === 'regex'
        ? '正则模式：支持 Python 正则语法（每行一条），命中任一条即拦截。'
        : '关键词模式：逗号分隔多个词，文本中出现任一关键词即拦截（大小写不敏感）。';
}
function onGateTypeChange() { _onGateTypeChange(); }
window.onGateTypeChange = onGateTypeChange;

// ============ 保存 ============
async function saveGateRule() {
    if (window.__gateSaving) return;
    const id = document.getElementById('gate-modal-id').value;
    const catSel = document.getElementById('gate-modal-category');
    const customInput = document.getElementById('gate-modal-category-custom');
    const category = catSel.value === '__custom__' ? (customInput.value.trim() || 'other') : catSel.value;
    const category_label = catSel.value === '__custom__'
        ? (customInput.value.trim() || '其他')
        : (GATE_PRESET_CATEGORIES.find(c => c.category === catSel.value)?.label || catSel.value);

    const data = {
        category,
        category_label,
        name: document.getElementById('gate-modal-name').value.trim(),
        match_type: document.getElementById('gate-modal-type').value,
        pattern: document.getElementById('gate-modal-pattern').value.trim(),
        intercept_message: document.getElementById('gate-modal-intercept').value.trim(),
        priority: parseInt(document.getElementById('gate-modal-priority').value) || 0,
        enabled: document.getElementById('gate-modal-enabled').checked ? 1 : 0,
    };

    if (!data.pattern) {
        showToast('命中模式不能为空', 'warning');
        return;
    }
    if (!data.category || data.category === '__custom__') {
        showToast('请选择或填写分类', 'warning');
        return;
    }

    window.__gateSaving = true;
    try {
        let result;
        if (id) {
            result = await api.updateGateRule(parseInt(id), data);
        } else {
            result = await api.addGateRule(data);
        }
        if (result && result.success) {
            showToast(result.message || '保存成功');
            closeGateRuleModal();
            loadGateRules(false);
        } else {
            showToast(result?.message || '保存失败', 'error');
        }
    } catch (e) {
        console.error('saveGateRule failed:', e);
        showToast('保存失败', 'error');
    } finally {
        window.__gateSaving = false;
    }
}
window.saveGateRule = saveGateRule;

async function editGateRule(id) {
    try {
        const data = await api.getGateRule(id);
        if (data && data.rule) showGateRuleModal(data.rule);
    } catch (e) {
        console.error('editGateRule failed:', e);
        showToast('加载失败', 'error');
    }
}
window.editGateRule = editGateRule;

async function deleteGateRule(id) {
    if (!confirm('确定删除这条门禁规则吗？删除后该规则不再拦截提问。')) return;
    try {
        const result = await api.deleteGateRule(id);
        if (result && result.success) {
            showToast('删除成功');
            selectedGateRuleIds.delete(id);
            loadGateRules(false);
        } else {
            showToast(result?.message || '删除失败', 'error');
        }
    } catch (e) {
        console.error('deleteGateRule failed:', e);
        showToast('删除失败', 'error');
    }
}
window.deleteGateRule = deleteGateRule;

async function toggleGateRule(id) {
    try {
        const result = await api.toggleGateRule(id);
        if (result && result.success) {
            showToast(result.message);
            loadGateRules(false);
        } else {
            showToast(result?.message || '操作失败', 'error');
        }
    } catch (e) {
        console.error('toggleGateRule failed:', e);
        showToast('操作失败', 'error');
    }
}
window.toggleGateRule = toggleGateRule;

// ============ 批量 ============
async function batchEnableGate() {
    if (selectedGateRuleIds.size === 0) return showToast('请先选择规则', 'warning');
    try {
        const result = await api.batchGateRules(Array.from(selectedGateRuleIds), 'enable');
        if (result && result.success) {
            showToast(result.message);
            selectedGateRuleIds.clear();
            loadGateRules(false);
        }
    } catch (e) {
        console.error('batchEnableGate failed:', e);
        showToast('批量启用失败', 'error');
    }
}
window.batchEnableGate = batchEnableGate;

async function batchDisableGate() {
    if (selectedGateRuleIds.size === 0) return showToast('请先选择规则', 'warning');
    try {
        const result = await api.batchGateRules(Array.from(selectedGateRuleIds), 'disable');
        if (result && result.success) {
            showToast(result.message);
            selectedGateRuleIds.clear();
            loadGateRules(false);
        }
    } catch (e) {
        console.error('batchDisableGate failed:', e);
        showToast('批量禁用失败', 'error');
    }
}
window.batchDisableGate = batchDisableGate;

async function batchDeleteGate() {
    if (selectedGateRuleIds.size === 0) return showToast('请先选择规则', 'warning');
    if (!confirm(`确定删除选中的 ${selectedGateRuleIds.size} 条规则吗？`)) return;
    try {
        const result = await api.batchGateRules(Array.from(selectedGateRuleIds), 'delete');
        if (result && result.success) {
            showToast(result.message);
            selectedGateRuleIds.clear();
            loadGateRules(false);
        }
    } catch (e) {
        console.error('batchDeleteGate failed:', e);
        showToast('批量删除失败', 'error');
    }
}
window.batchDeleteGate = batchDeleteGate;

// ============ 命中测试 ============
function clearGateTest() {
    document.getElementById('gate-test-text').value = '';
    document.getElementById('gate-test-result').innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">${iconize('🧪')}</div>
            <h3>等待测试</h3>
            <p>输入一条用户提问，验证是否会被门禁拦截</p>
        </div>`;
}
window.clearGateTest = clearGateTest;

async function testGateMatch() {
    const text = document.getElementById('gate-test-text').value.trim();
    if (!text) {
        showToast('请输入测试文本', 'warning');
        return;
    }
    const container = document.getElementById('gate-test-result');
    container.innerHTML = '<div class="empty-state"><div class="empty-icon"><i class="fa-solid fa-spinner fa-spin"></i></div><p>检测中…</p></div>';
    try {
        const result = await api.testGateMatch(text);
        if (!result || !result.success) {
            container.innerHTML = '<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#ef4444;"></i><p style="margin-top:.5rem;">测试失败</p></div>';
            return;
        }
        if (!result.blocked) {
            container.innerHTML = `
                <div class="test-result-none">
                    <div style="font-size:32px;margin-bottom:8px;">${iconize('😕')}</div>
                    <p>未命中任何门禁规则，将正常进入 AI 答复流程。</p>
                </div>`;
            return;
        }
        container.innerHTML = `
            <div class="test-result-header">
                <span class="match-count" style="color:#ef4444;">将被拦截</span>
                <span class="top-reply">分类：${escapeHtml(result.category_label || result.category)}</span>
            </div>
            <div class="test-result-list">
                <div class="test-result-item">
                    <div class="test-result-pattern">
                        <code>${escapeHtml(result.pattern || '')}</code>
                        <span class="tag ${result.match_type === 'regex' ? 'tag-purple' : 'tag-green'}">
                            ${result.match_type === 'regex' ? '正则' : '关键词'}
                        </span>
                        <span class="priority-badge">规则 #${result.rule_id}</span>
                    </div>
                    <div class="test-result-reply"><b>拦截提示：</b>${escapeHtml(result.intercept_message || '')}</div>
                </div>
            </div>`;
    } catch (e) {
        container.innerHTML = '<div class="empty-state"><i class="fa-solid fa-triangle-exclamation" style="font-size:1.75rem;opacity:.4;color:#ef4444;"></i><p style="margin-top:.5rem;">测试失败: ' + escapeHtml(e.message || String(e)) + '</p></div>';
        showToast('门禁命中测试失败', 'error');
    }
}
window.testGateMatch = testGateMatch;
