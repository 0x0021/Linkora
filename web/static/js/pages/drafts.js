// ============ pages/drafts.js ============
// Draft review page: list, approve, edit, discard AI-generated draft replies
// ES module: exports init/cleanup, also attaches global helpers for existing page system
// 平台筛选已统一由顶部全局 platform-switcher 接管（api.js _withPlatform 自动追加 ?platform=）

let _draftStatus = 'all';
let _draftPage = 1;
const _DRAFT_PAGE_SIZE = 20;
let _draftTotal = 0;
let _draftItems = [];
let _draftExpandedId = null;
let _draftSelected = {}; // 批量选择状态
let _draftEditId = null;

const DRAFT_STATUS_LABELS = {
    all: '全部',
    pending: '待处理',
    approved: '已审批',
    discarded: '已丢弃',
};

// ── Stats ──
async function loadDraftStats() {
    try {
        // 平台筛选由 api._withPlatform() 自动追加 ?platform=
        const data = await api.fetch('/api/drafts?status=all&limit=1');
        if (!data || data.error) return;
        setText('draft-stat-pending', data.pending_count || 0);
        setText('draft-stat-approved', data.approved_count || 0);
        setText('draft-stat-discarded', data.discarded_count || 0);
        // Update badge
        const badge = document.getElementById('draft-pending-badge');
        if (badge) {
            const cnt = data.pending_count || 0;
            badge.textContent = cnt;
            badge.style.display = cnt > 0 ? 'inline-flex' : 'none';
        }
    } catch (e) {
        // silent
    }
}

// ── Truncate helpers ──
function _draftTruncate(s, max) {
    if (!s) return '\u2014';
    return s.length > max ? s.slice(0, max) + '\u2026' : s;
}

function _draftStatusPill(status) {
    if (status === 'pending') {
        return '<span class="draft-status-pending"><i class="fa-solid fa-clock" style="font-size:9px;"></i> \u5f85\u5904\u7406</span>';
    } else if (status === 'approved') {
        return '<span class="draft-status-approved"><i class="fa-solid fa-circle-check" style="font-size:9px;"></i> \u5df2\u5ba1\u6279</span>';
    } else {
        return '<span class="draft-status-discarded"><i class="fa-solid fa-ban" style="font-size:9px;"></i> \u5df2\u4e22\u5f03</span>';
    }
}

/** Client-side filter: search sender, conversation, user message, and AI reply */
function _draftFilterItems(items) {
    var input = document.getElementById('draft-search');
    var query = (input ? input.value : '').trim();
    if (!query) return items;
    var q = query.toLowerCase();
    return items.filter(function(item) {
        return ((item.sender_name || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.sender_id || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.conversation_name || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.conversation_id || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.user_message || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.ai_reply || '').toLowerCase().indexOf(q) >= 0);
    });
}

function _draftConfidenceBadge(score) {
    if (score === null || score === undefined) return '<span style="color:#94a3b8;">\u2014</span>';
    var pct = (score * 100).toFixed(0);
    var cls = score >= 0.8 ? 'draft-conf-high' : score >= 0.5 ? 'draft-conf-mid' : 'draft-conf-low';
    return '<span class="draft-conf-badge ' + cls + '">' + pct + '%</span>';
}

// ── Action handlers ──
async function _draftApprove(id, btn) {
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }
    try {
        const res = await api.fetch('/api/drafts/' + id + '/approve', 'POST');
        if (res && res.success) {
            showToast('\u8349\u7a3f #' + id + ' \u5df2\u6279\u51c6\u53d1\u9001', 'success');
            loadDraftsPage();
        } else {
            showToast((res && res.detail) || (res && res.error) || '\u6279\u51c6\u5931\u8d25', 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-circle-check"></i> \u6279\u51c6\u53d1\u9001'; }
        }
    } catch (e) {
        showToast('\u6279\u51c6\u5f02\u5e38: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-circle-check"></i> \u6279\u51c6\u53d1\u9001'; }
    }
}

async function _draftDiscard(id, btn) {
    if (!confirm('\u786e\u8ba4\u4e22\u5f03\u8349\u7a3f #' + id + '\uff1f\u4e22\u5f03\u540e\u4e0d\u53ef\u6062\u590d\u3002')) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#ef4444;"></i>'; }
    try {
        const res = await api.fetch('/api/drafts/' + id + '/discard', 'POST');
        if (res && res.success) {
            showToast('\u8349\u7a3f #' + id + ' \u5df2\u4e22\u5f03', 'success');
            loadDraftsPage();
        } else {
            showToast((res && res.detail) || (res && res.error) || '\u4e22\u5f03\u5931\u8d25', 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-trash"></i>'; }
        }
    } catch (e) {
        showToast('\u4e22\u5f03\u5f02\u5e38: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-trash"></i>'; }
    }
}

function _draftShowEditModal(id) {
    _draftEditId = id;
    var cached = _draftDetailCache[id];
    var originalText = cached ? cached.ai_reply : '';
    const modal = document.getElementById('draft-edit-modal');
    document.getElementById('draft-edit-textarea').value = originalText || '';
    document.getElementById('draft-edit-id').textContent = '#' + id;
    modal.classList.add('active');
    setTimeout(function () {
        document.getElementById('draft-edit-textarea').focus();
    }, 100);
}

function _draftCloseEditModal() {
    document.getElementById('draft-edit-modal').classList.remove('active');
    _draftEditId = null;
}

async function _draftSubmitEdit() {
    if (_draftEditId === null) return;
    const text = document.getElementById('draft-edit-textarea').value.trim();
    if (!text) {
        showToast('\u56de\u590d\u5185\u5bb9\u4e0d\u80fd\u4e3a\u7a7a', 'error');
        return;
    }
    const btn = document.getElementById('draft-edit-submit-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> \u53d1\u9001\u4e2d...'; }
    try {
        const res = await api.fetch('/api/drafts/' + _draftEditId + '/edit', 'POST', { final_reply: text });
        if (res && res.success) {
            showToast('\u8349\u7a3f #' + _draftEditId + ' \u5df2\u7f16\u8f91\u5e76\u53d1\u9001', 'success');
            _draftCloseEditModal();
            loadDraftsPage();
        } else {
            showToast((res && res.detail) || (res && res.error) || '\u7f16\u8f91\u53d1\u9001\u5931\u8d25', 'error');
        }
    } catch (e) {
        showToast('\u7f16\u8f91\u5f02\u5e38: ' + e.message, 'error');
    }
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i> \u7f16\u8f91\u5e76\u53d1\u9001'; }
}

// ── Row expand / collapse ──
function _draftToggleExpand(id, tr) {
    if (_draftExpandedId === id) {
        _draftCollapseExpand();
        return;
    }
    _draftCollapseExpand();
    _draftExpandedId = id;
    // highlight row
    if (tr) tr.classList.add('draft-row-expanded');
    // insert detail row
    const detail = _draftBuildDetailRow(id);
    if (tr && detail) {
        tr.insertAdjacentHTML('afterend', detail);
    }
}

function _draftCollapseExpand() {
    if (_draftExpandedId === null) return;
    const expandedTr = document.querySelector('.draft-row-expanded');
    if (expandedTr) expandedTr.classList.remove('draft-row-expanded');
    const detailRow = document.querySelector('.draft-detail-row');
    if (detailRow) detailRow.remove();
    _draftExpandedId = null;
}

function _draftBuildDetailRow(id) {
    var cache = _draftDetailCache[id];
    if (!cache) return '<tr class="draft-detail-row"><td colspan="9"><div class="draft-detail-panel"><p style="color:#94a3b8;">\u8be6\u60c5\u52a0\u8f7d\u4e2d\u2026</p></div></td></tr>';
    var d = cache;
    var html = '<tr class="draft-detail-row"><td colspan="9"><div class="draft-detail-panel">';
    html += '<div class="draft-detail-section"><h4><i class="fa-solid fa-message"></i> \u5b8c\u6574\u7528\u6237\u6d88\u606f</h4><div class="draft-detail-text">' + escapeHtml(d.user_message || '') + '</div></div>';
    html += '<div class="draft-detail-section"><h4><i class="fa-solid fa-robot"></i> \u5b8c\u6574 AI \u62df\u56de\u590d</h4><div class="draft-detail-text">' + escapeHtml(d.ai_reply || '') + '</div></div>';
    if (d.rag_chunks && d.rag_chunks.length > 0) {
        html += '<div class="draft-detail-section"><h4><i class="fa-solid fa-database"></i> RAG \u5339\u914d\u77e5\u8bc6\u7247\u6bb5</h4>';
        for (var i = 0; i < d.rag_chunks.length; i++) {
            var chunk = d.rag_chunks[i];
            html += '<div class="draft-rag-chunk"><div class="draft-rag-chunk-meta">' + escapeHtml(chunk.source || '\u672a\u77e5\u6765\u6e90') + ' \u00b7 \u76f8\u4f3c\u5ea6: ' + ((chunk.similarity || 0) * 100).toFixed(1) + '%</div><div class="draft-rag-chunk-text">' + escapeHtml(chunk.text || '').slice(0, 500) + '</div></div>';
        }
        html += '</div>';
    }
    if (d.confidence_detail) {
        html += '<div class="draft-detail-section"><h4><i class="fa-solid fa-chart-simple"></i> \u7f6e\u4fe1\u5ea6\u8be6\u60c5</h4><div class="draft-detail-text">' + escapeHtml(d.confidence_detail) + '</div></div>';
    }
    html += '</div></td></tr>';
    return html;
}

// ── Detail cache ──
var _draftDetailCache = {};

function _draftPrimeDetailCache(items) {
    for (var i = 0; i < items.length; i++) {
        var item = items[i];
        _draftDetailCache[item.draft_id] = {
            user_message: item.user_message || '',
            ai_reply: item.ai_reply || '',
            rag_chunks: item.rag_chunks || [],
            confidence_detail: item.confidence_detail || '',
        };
    }
}

// ── Main page loader ──
async function loadDraftsPage() {
    var container = document.getElementById('drafts-content');
    if (!container) return;

    container.innerHTML = '<div class="draft-empty"><i class="fa-solid fa-spinner fa-spin" style="color:#94a3b8;"></i><p>\u52a0\u8f7d\u4e2d\u2026</p></div>';

    // Build tab bar
    var tabsHtml = '';
    ['all', 'pending', 'approved', 'discarded'].forEach(function (s) {
        tabsHtml += '<button class="draft-tab' + (_draftStatus === s ? ' active' : '') + '" data-status="' + s + '" onclick="_draftSwitchStatus(\'' + s + '\')">' + DRAFT_STATUS_LABELS[s] + '</button>';
    });

    try {
        // 平台筛选由 api._withPlatform() 自动追加 ?platform=
        var url = '/api/drafts?status=' + _draftStatus + '&limit=' + _DRAFT_PAGE_SIZE + '&offset=' + ((_draftPage - 1) * _DRAFT_PAGE_SIZE);
        var data = await api.fetch(url);
        if (!data || data.error) {
            container.innerHTML = '<div class="alert alert-error" style="margin:12px;"><strong>\u52a0\u8f7d\u5931\u8d25</strong><p style="margin:4px 0 0 0;color:#666;font-size:12px;">' + escapeHtml(data ? data.error : '\u672a\u77e5\u9519\u8bef') + '</p></div>';
            return;
        }

        var items = data.items || [];
        _draftItems = items;
        items = _draftFilterItems(items);
        _draftTotal = data.total || 0;
        _draftPrimeDetailCache(items);

        // Update stats bar
        setText('draft-stat-pending', data.pending_count || 0);
        setText('draft-stat-approved', data.approved_count || 0);
        setText('draft-stat-discarded', data.discarded_count || 0);
        var badge = document.getElementById('draft-pending-badge');
        if (badge) {
            var cnt = data.pending_count || 0;
            badge.textContent = cnt;
            badge.style.display = cnt > 0 ? 'inline-flex' : 'none';
        }

        // Build table (always shown, even when empty)
        var html = '<div class="draft-tabs">' + tabsHtml + '</div>';
        html += '<div class="draft-table-wrap"><table class="draft-table"><colgroup>'
            + '<col class="c-col-check"><col class="c-col-platform"><col class="c-col-sender"><col class="c-col-conv">'
            + '<col><col><col class="c-col-rag"><col class="c-col-time"><col class="c-col-actions">' // 用户消息/AI拟回复吃剩余
            + '</colgroup><thead><tr>';
        html += '<th style="width:32px"><input type="checkbox" class="batch-checkbox" onclick="_draftToggleAll(this)" title="全选"></th>';
        html += '<th>\u5e73\u53f0</th>';
        html += '<th style="min-width:60px">\u53d1\u9001\u8005</th>';
        html += '<th style="min-width:70px">\u4f1a\u8bdd</th>';
        html += '<th style="min-width:100px">\u7528\u6237\u6d88\u606f</th>';
        html += '<th style="min-width:100px">AI \u62df\u56de\u590d</th>';
        html += '<th>RAG \u7f6e\u4fe1\u5ea6</th>';
        html += '<th>\u521b\u5efa\u65f6\u95f4</th>';
        html += '<th style="min-width:150px;text-align:right">\u64cd\u4f5c</th>';
        html += '</tr></thead><tbody>';

        // Empty state: placeholder row inside table
        if (items.length === 0) {
            var emptyIcon = _draftStatus === 'pending' ? 'fa-file-pen' : _draftStatus === 'approved' ? 'fa-circle-check' : 'fa-circle-xmark';
            var emptyColor = _draftStatus === 'pending' ? '#f59e0b' : _draftStatus === 'approved' ? '#16a34a' : '#94a3b8';
            html += '<tr class="draft-empty-row"><td colspan="9"><div class="draft-table-empty"><i class="fa-solid ' + emptyIcon + '" style="color:' + emptyColor + ';"></i><p>\u6682\u65e0' + DRAFT_STATUS_LABELS[_draftStatus] + '\u8349\u7a3f</p></div></td></tr>';
        } else {

        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var ts = (item.created_at || '').replace('T', ' ').slice(0, 19);
            var platform = item.platform || '\u2014';
            var sender = (item.sender_name || item.sender_id || '\u2014').slice(0, 14);
            var conversation = (item.conversation_name || item.conversation_id || '\u2014').slice(0, 16);
            var userMsgPreview = _draftTruncate(item.user_message, 60);
            var aiReplyPreview = _draftTruncate(item.ai_reply, 80);
            var confidenceHtml = _draftConfidenceBadge(item.rag_confidence);

            var actions;
            if (item.status === 'pending') {
                actions = '<div class="draft-actions">' +
                    '<button class="btn-draft-approve" onclick="event.stopPropagation();_draftApprove(\'' + item.draft_id + '\', this)"><i class="fa-solid fa-circle-check"></i> \u6279\u51c6\u53d1\u9001</button>' +
                    '<button class="btn-draft-edit" onclick="event.stopPropagation();_draftShowEditModal(\'' + item.draft_id + '\')"><i class="fa-solid fa-pen-to-square"></i> \u7f16\u8f91\u5e76\u53d1\u9001</button>' +
                    '<button class="btn-draft-discard" onclick="event.stopPropagation();_draftDiscard(\'' + item.draft_id + '\', this)"><i class="fa-solid fa-trash"></i></button>' +
                    '</div>';
            } else if (item.status === 'approved') {
                actions = '<span style="font-size:11px;color:var(--text-tertiary);">\u5df2\u53d1\u9001</span>';
            } else {
                actions = '<span style="font-size:11px;color:var(--text-tertiary);">\u5df2\u4e22\u5f03</span>';
            }

            html += '<tr class="draft-row" data-id="' + item.draft_id + '">';
            html += '<td><input type="checkbox" class="batch-checkbox" data-draft-id="' + item.draft_id + '" onclick="_draftOnCheck(this)" ' + (_draftSelected[item.draft_id] ? 'checked' : '') + '></td>';
            html += '<td style="font-size:12px;color:#64748b;">' + escapeHtml(platform) + '</td>';
            html += '<td style="font-size:12.5px;min-width:60px;" title="' + escapeHtml(item.sender_name || item.sender_id || '') + '">' + escapeHtml(sender) + '</td>';
            html += '<td style="font-size:12.5px;min-width:70px;" title="' + escapeHtml(item.conversation_name || item.conversation_id || '') + '">' + escapeHtml(conversation) + '</td>';
            html += '<td style="font-size:12.5px;min-width:100px;color:#334155;" title="' + escapeHtml(item.user_message || '') + '">' + escapeHtml(userMsgPreview) + '</td>';
            html += '<td style="font-size:12.5px;min-width:100px;color:#334155;" title="' + escapeHtml(item.ai_reply || '') + '">' + escapeHtml(aiReplyPreview) + '</td>';
            html += '<td>' + confidenceHtml + '</td>';
            html += '<td style="font-size:12px;color:#64748b;" title="' + ts + '">' + ts + '</td>';
            html += '<td style="min-width:150px">' + actions + '</td>';
            html += '</tr>';
        }
        } // end else

        html += '</tbody></table></div>';
        // 统一翻页条（与死信/文档列表一致）
        html += '<div id="drafts-pagination" class="intent-pagination"></div>';
        container.innerHTML = html;
        renderDraftsPager();
        _draftUpdateBatchBar();

    } catch (e) {
        container.innerHTML = '<div class="alert alert-error" style="margin:12px;"><strong>\u8bf7\u6c42\u5f02\u5e38</strong><p style="margin:4px 0 0 0;color:#666;font-size:12px;">' + escapeHtml(e.message || String(e)) + '</p></div>';
    }
}

function _draftSwitchStatus(status) {
    _draftStatus = status;
    _draftPage = 1;
    _draftExpandedId = null;
    loadDraftsPage();
}

function renderDraftsPager() {
    renderPager('drafts-pagination', {
        total: _draftTotal, page: _draftPage, pageSize: _DRAFT_PAGE_SIZE,
    }, function (p) {
        _draftGoPage(p);
    });
}

function _draftGoPage(p) {
    _draftPage = p;
    _draftExpandedId = null;
    loadDraftsPage();
    // scroll to top of drafts content
    var container = document.getElementById('drafts-content');
    if (container) container.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── ES module exports ──
export function init() {
    _draftStatus = 'all';
    _draftPage = 1;
    _draftExpandedId = null;
    _draftEditId = null;
    loadDraftsPage();
}

export function cleanup() {
    _draftCollapseExpand();
    _draftCloseEditModal();
    _draftStatus = 'all';
    _draftPage = 1;
    _draftExpandedId = null;
    _draftEditId = null;
    _draftDetailCache = {};
}

// ── Global exposure for existing app.js page switching system ──
window.loadDraftsPage = loadDraftsPage;
window.loadDraftStats = loadDraftStats;
window._draftSwitchStatus = _draftSwitchStatus;
window._draftGoPage = _draftGoPage;
window._draftToggleExpand = _draftToggleExpand;
window._draftApprove = _draftApprove;
window._draftDiscard = _draftDiscard;
window._draftShowEditModal = _draftShowEditModal;
window._draftCloseEditModal = _draftCloseEditModal;
window._draftSubmitEdit = _draftSubmitEdit;

// ===================== 批量操作 =====================

function _draftOnCheck(cb) {
    var id = cb.getAttribute('data-draft-id');
    if (cb.checked) _draftSelected[id] = true; else delete _draftSelected[id];
    _draftUpdateBatchBar();
}
window._draftOnCheck = _draftOnCheck;

function _draftToggleAll(cb) {
    var items = _draftItems || [];
    if (cb.checked) {
        items.forEach(function(i) { _draftSelected[i.draft_id] = true; });
    } else {
        _draftSelected = {};
    }
    loadDraftsPage(); // re-render to sync all checkboxes
}
window._draftToggleAll = _draftToggleAll;

function _draftDeselectAll() {
    _draftSelected = {};
    loadDraftsPage();
}
window._draftDeselectAll = _draftDeselectAll;

function _draftUpdateBatchBar() {
    var bar = document.getElementById('draft-batch-bar');
    if (!bar) return;
    var count = Object.keys(_draftSelected).length;
    document.getElementById('draft-batch-count').textContent = count;
    bar.style.display = count > 0 ? 'flex' : 'none';
}

async function _draftBatchApprove() {
    var ids = Object.keys(_draftSelected);
    if (ids.length === 0) return;
    if (!confirm('确认批准发送选中的 ' + ids.length + ' 条草稿？')) return;
    try {
        await api.post('/api/drafts/batch-approve', { ids: ids });
        _draftSelected = {};
        toast('已批准发送 ' + ids.length + ' 条草稿');
        _draftPage = 1;
        loadDraftsPage();
    } catch (e) {
        toast('批量批准失败: ' + (e.message || e));
    }
}
window._draftBatchApprove = _draftBatchApprove;

async function _draftBatchReject() {
    var ids = Object.keys(_draftSelected);
    if (ids.length === 0) return;
    if (!confirm('确认拒绝 ' + ids.length + ' 条草稿（标记为已丢弃）？')) return;
    try {
        await api.post('/api/drafts/batch-reject', { ids: ids });
        _draftSelected = {};
        toast('已拒绝 ' + ids.length + ' 条草稿');
        _draftPage = 1;
        loadDraftsPage();
    } catch (e) {
        toast('批量拒绝失败: ' + (e.message || e));
    }
}
window._draftBatchReject = _draftBatchReject;
