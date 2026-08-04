// ============ pages/dead_letters.js ============
// Dead letter queue management: list, replay, discard

let _dlqStatus = 'all';
let _dlqPage = 1;
const _DLQ_PAGE_SIZE = 20;
let _dlqSelected = {}; // 批量选择状态：{id: true}
let _dlqTotal = 0;
let _dlqItems = [];

function _dlqStatusLabel(s) {
    return { pending: '待处理', replayed: '已重放', discarded: '已丢弃', all: '全部' }[s] || s;
}

function _dlqStageLabel(stage) {
    var map = {
        message_in: '消息接收', intent: '意图识别', tool_exposure: '工具调用',
        skill_routing: '技能路由', llm_inference: 'LLM推理', reply: '回复发送',
    };
    return map[stage] || stage;
}

function _esc(s) {
    if (!s) return '';
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

/** Client-side filter: search sender name/id, content preview, and error message */
function _dlqFilterItems(items) {
    var input = document.getElementById('dlq-search');
    var query = (input ? input.value : '').trim();
    if (!query) return items;
    var q = query.toLowerCase();
    return items.filter(function(item) {
        return ((item.sender_name || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.sender_id || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.content || '').toLowerCase().indexOf(q) >= 0) ||
               ((item.error || '').toLowerCase().indexOf(q) >= 0);
    });
}

async function loadDeadLettersPage() {
    var container = document.getElementById('deadletters-content');
    if (!container) return;

    container.innerHTML = '<div class="dlq-empty"><i class="fa-solid fa-spinner fa-spin" style="color:#94a3b8;"></i><p>加载中\u2026</p></div>';

    var tabs = ['pending', 'replayed', 'discarded'].map(function(s) {
        return '<button class="dlq-tab' + (_dlqStatus === s ? ' active' : '') + '" data-status="' + s + '" onclick="_dlqSwitchStatus(\'' + s + '\')">' + _dlqStatusLabel(s) + '</button>';
    }).join('');
    var allBtn = '<button class="dlq-tab' + (_dlqStatus === 'all' ? ' active' : '') + '" data-status="all" onclick="_dlqSwitchStatus(\'all\')">全部</button>';

    try {
        var data = await api.fetch('/api/dead-letters?status=' + _dlqStatus + '&limit=' + _DLQ_PAGE_SIZE + '&offset=' + ((_dlqPage - 1) * _DLQ_PAGE_SIZE));
        if (!data || data.error) {
            container.innerHTML = '<div class="alert alert-error" style="margin:12px;"><strong>加载失败</strong><p style="margin:4px 0 0 0;color:#666;font-size:12px;">' + _esc(data ? data.error : '未知错误') + '</p></div>';
            return;
        }

        var items = data.items || [];
        _dlqItems = items;
        items = _dlqFilterItems(items);
        _dlqTotal = data.total || 0;
        var statsEl = document.getElementById('dlq-stats');
        if (statsEl) statsEl.innerHTML = '<i class="fa-solid fa-list-ul" style="font-size:11px;opacity:.5;"></i> 共 <span class="count">' + (_dlqTotal) + '</span> 条';

        // Build table (always shown, even when empty)
        var html = '<div class="dlq-tabs">' + allBtn + tabs + '</div>';
        html += '<div class="dlq-table-wrap"><table class="dlq-table"><thead><tr>';
        html += '<th style="width:32px"><input type="checkbox" class="batch-checkbox" onclick="_dlqToggleAll(this)" title="全选"></th>';
        html += '<th>ID</th>';
        html += '<th>时间</th>';
        html += '<th style="max-width:90px">发送者</th>';
        html += '<th style="max-width:110px">阶段</th>';
        html += '<th style="max-width:240px">内容预览</th>';
        html += '<th style="max-width:180px">错误信息</th>';
        html += '<th>状态</th>';
        html += '<th style="max-width:130px;text-align:right">操作</th>';
        html += '</tr></thead><tbody>';

        if (items.length === 0) {
            var emptyIcon = _dlqStatus === 'pending' ? 'fa-circle-check' : _dlqStatus === 'replayed' ? 'fa-rotate-left' : 'fa-circle-xmark';
            var emptyColor = _dlqStatus === 'pending' ? '#16a34a' : _dlqStatus === 'replayed' ? '#2563eb' : '#94a3b8';
            html += '<tr class="dlq-empty-row"><td colspan="9"><div class="dlq-table-empty"><i class="fa-solid ' + emptyIcon + '" style="color:' + emptyColor + ';"></i><p>暂无' + _dlqStatusLabel(_dlqStatus) + '死信消息</p></div></td></tr>';
            html += '</tbody></table></div>';
            container.innerHTML = html;
            return;
        }

        var errorStages = ['llm_inference', 'reply'];
        var warnStages = ['intent', 'tool_exposure', 'skill_routing'];

        for (var i = 0; i < items.length; i++) {
            var item = items[i];
            var ts = (item.created_at || '').replace('T', ' ').slice(0, 19);
            var content = item.content || '';
            // 长文本固定字符数截断（50 字），短文本保持原长
            var preview = content.length > 50 ? content.slice(0, 50) + '\u2026' : content;
            var errMsg = item.error || '';
            // 错误信息固定字符数截断（40 字）
            var errPreview = errMsg.length > 40 ? errMsg.slice(0, 40) + '\u2026' : errMsg;

            var statusHtml;
            if (item.status === 'pending') {
                statusHtml = '<span class="dlq-status-pending"><i class="fa-solid fa-clock" style="font-size:9px;"></i> 待处理</span>';
            } else if (item.status === 'replayed') {
                statusHtml = '<span class="dlq-status-replayed"><i class="fa-solid fa-rotate-left" style="font-size:9px;"></i> 已重放</span>';
            } else {
                statusHtml = '<span class="dlq-status-discarded"><i class="fa-solid fa-ban" style="font-size:9px;"></i> 已丢弃</span>';
            }

            var actions;
            if (item.status === 'pending') {
                actions = '<div class="dlq-actions">' +
                    '<button class="btn-replay" onclick="_dlqReplay(' + item.id + ', this)"><i class="fa-solid fa-rotate"></i> 重放</button>' +
                    '<button class="btn-discard" onclick="_dlqDiscard(' + item.id + ', this)"><i class="fa-solid fa-trash"></i></button>' +
                    '</div>';
            } else if (item.status === 'replayed') {
                // 截断到「月-日 时:分」(11字)，避免操作列被撑宽
                var replayTs = item.replayed_at ? item.replayed_at.replace('T', ' ').slice(5, 16) : '';
                var replayInfo = replayTs ? '于 ' + replayTs : '';
                if (item.replay_note) replayInfo += ' · ' + _esc(item.replay_note.slice(0, 16));
                actions = '<span style="font-size:11px;color:var(--text-tertiary);white-space:nowrap;">' + replayInfo + '</span>';
            } else {
                actions = '<span style="font-size:11px;color:var(--text-tertiary);">手动丢弃</span>';
            }

            var stageCls = (errorStages.indexOf(item.stage) >= 0) ? 'stage-error'
                : (warnStages.indexOf(item.stage) >= 0) ? 'stage-warn' : 'stage-ok';

            html += '<tr data-id="' + item.id + '">';
            html += '<td><input type="checkbox" class="batch-checkbox" data-dlq-id="' + item.id + '" onclick="_dlqOnCheck(this)" ' + (_dlqSelected[item.id] ? 'checked' : '') + '></td>';
            html += '<td class="dlq-id-cell">#' + item.id + '</td>';
            html += '<td style="font-size:12px;color:#64748b;" title="' + ts + '">' + ts + '</td>';
            html += '<td style="font-size:12.5px;max-width:90px;" title="' + _esc(item.sender_name || item.sender_id || '') + '">' + _esc((item.sender_name || item.sender_id || '\u2014').slice(0, 14)) + '</td>';
            html += '<td style="max-width:110px"><span class="dlq-stage-badge ' + stageCls + '">' + _dlqStageLabel(item.stage) + '</span></td>';
            html += '<td style="font-size:12.5px;max-width:240px;color:#334155;" title="' + _esc(content) + '">' + _esc(preview) + '</td>';
            html += '<td class="dlq-error-cell" title="' + _esc(errMsg) + '">' + _esc(errPreview) + '</td>';
            html += '<td>' + statusHtml + '</td>';
            html += '<td style="max-width:130px">' + actions + '</td>';
            html += '</tr>';
        }

        html += '</tbody></table></div>';
        html += '<div id="deadletters-pagination" class="intent-pagination"></div>';
        container.innerHTML = html;
        renderDeadLettersPager();
        _dlqUpdateBatchBar();

    } catch (e) {
        container.innerHTML = '<div class="alert alert-error" style="margin:12px;"><strong>请求异常</strong><p style="margin:4px 0 0 0;color:#666;font-size:12px;">' + _esc(e.message || String(e)) + '</p></div>';
    }
}

function _dlqSwitchStatus(status) {
    _dlqStatus = status;
    _dlqPage = 1;
    loadDeadLettersPage();
}

function renderDeadLettersPager() {
    renderPager('deadletters-pagination', {
        total: _dlqTotal, page: _dlqPage, pageSize: _DLQ_PAGE_SIZE,
    }, function (p) {
        _dlqPage = p;
        loadDeadLettersPage();
    });
}

async function _dlqReplay(id, btn) {
    if (!confirm('确认重放 #' + id + '？')) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }
    try {
        var res = await api.fetch('/api/dead-letters/' + id + '/replay', 'POST');
        if (res && res.success) {
            showToast('#' + id + ' 重放成功', 'success');
            loadDeadLettersPage();
        } else {
            showToast((res && res.detail) || (res && res.error) || '重放失败', 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-rotate"></i> 重放'; }
        }
    } catch (e) {
        showToast('重放异常: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-rotate"></i> 重放'; }
    }
}

async function _dlqDiscard(id, btn) {
    if (!confirm('确认丢弃 #' + id + '？丢弃后不可恢复。')) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="color:#ef4444;"></i>'; }
    try {
        var res = await api.fetch('/api/dead-letters/' + id + '/discard', 'POST');
        if (res && res.success) {
            showToast('#' + id + ' 已丢弃', 'success');
            loadDeadLettersPage();
        } else {
            showToast((res && res.detail) || (res && res.error) || '丢弃失败', 'error');
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-trash"></i>'; }
        }
    } catch (e) {
        showToast('丢弃异常: ' + e.message, 'error');
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-trash"></i>'; }
    }
}

async function exportDeadLettersCSV() {
    try {
        await api.exportDeadLetters('all', 10000);
    } catch (e) {
        showToast('导出失败：' + (e && e.message ? e.message : e), 'error');
    }
}

let _dlqBatchRunning = false;
async function _dlqBatchReplay() {
    if (_dlqBatchRunning) return;
    try {
        var data = await api.fetch('/api/dead-letters?status=pending&limit=1');
        var pendingTotal = (data && data.total) || 0;
        if (pendingTotal === 0) {
            showToast('没有待处理的死信', 'info');
            return;
        }
    } catch (e) { /* 忽略，进入确认 */ }

    if (!confirm('确认重放全部待处理死信？此操作不可撤销。')) return;
    _dlqBatchRunning = true;

    var btn = document.querySelector('button[onclick="_dlqBatchReplay()"]');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 重放中...'; }
    showToast('正在批量重放死信...', 'info');

    try {
        var res = await api.fetch('/api/dead-letters/batch-replay', 'POST');
        if (res && res.success) {
            showToast('重放完成: 成功 ' + res.replayed + '，失败 ' + res.failed, res.failed > 0 ? 'warn' : 'success');
        } else {
            showToast((res && res.detail) || '批量重放失败', 'error');
        }
    } catch (e) {
        showToast('批量重放异常: ' + (e && e.message ? e.message : e), 'error');
    } finally {
        _dlqBatchRunning = false;
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-forward-fast"></i> 全部重放'; }
        loadDeadLettersPage();
    }
}

// ── 批量选择 ──────────────────────────────────────────────────────
function _dlqOnCheck(cb) {
    var id = cb.getAttribute('data-dlq-id');
    if (cb.checked) { _dlqSelected[id] = true; } else { delete _dlqSelected[id]; }
    _dlqUpdateBatchBar();
}

function _dlqToggleAll(cb) {
    var items = _dlqItems;
    if (cb.checked) { for (var i = 0; i < items.length; i++) { _dlqSelected[items[i].id] = true; } }
    else { _dlqSelected = {}; }
    loadDeadLettersPage();
}

function _dlqDeselectAll() {
    _dlqSelected = {};
    loadDeadLettersPage();
}

function _dlqUpdateBatchBar() {
    var count = Object.keys(_dlqSelected).length;
    var bar = document.getElementById('dlq-batch-bar');
    var countEl = document.getElementById('dlq-batch-count');
    if (!bar) return;
    bar.style.display = count > 0 ? 'flex' : 'none';
    if (countEl) countEl.textContent = '已选 ' + count + ' 项';
}

async function _dlqBatchDiscard() {
    var ids = Object.keys(_dlqSelected);
    if (ids.length === 0) return;
    if (!confirm('确定丢弃选中的 ' + ids.length + ' 条死信？')) return;
    var btn = document.querySelector('#dlq-batch-bar .btn-danger');
    if (btn) { btn.disabled = true; btn.textContent = '丢弃中...'; }
    var success = 0;
    for (var i = 0; i < ids.length; i++) {
        try {
            var res = await api.post('/api/dead-letters/' + ids[i] + '/discard');
            if (res && !res.error) success++;
        } catch(e) {}
    }
    if (btn) { btn.disabled = false; btn.textContent = '批量丢弃'; }
    showToast('完成: ' + success + '/' + ids.length + ' 条已丢弃', success === ids.length ? 'success' : 'warning');
    _dlqSelected = {};
    loadDeadLettersPage();
}

