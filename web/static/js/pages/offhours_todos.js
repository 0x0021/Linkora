// ============ pages/offhours_todos.js ============
// 工作时间外未回复消息 → 待办：规则 / 未回复消息 / 建单记录 / 手动触发

let _otdTab = 'unanswered';
let _otdStatus = 'pending';
let _otdPage = 1;
const _OTD_PAGE_SIZE = 20;
let _otdTotal = 0;
let _otdBusy = false;

function _otdEsc(s) {
    if (s === null || s === undefined) return '';
    var d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

function _otdStatusLabel(s) {
    return {
        pending: '待处理', todo_created: '已建待办', notified_only: '仅通知',
        ignored: '已忽略', replied_later: '已回复', todo_failed: '建单失败', all: '全部',
    }[s] || s || '-';
}

function _otdReasonLabel(r) {
    return { off_hours: '工作时间外', weekend: '非工作日', holiday: '节假日' }[r] || r || '-';
}

function _otdStatusClass(s) {
    if (s === 'pending') return 'badge-warn';
    if (s === 'todo_created' || s === 'replied_later') return 'badge-ok';
    if (s === 'todo_failed') return 'badge-err';
    return 'badge-muted';
}

/** 加载规则 + 统计 + 当前 tab 列表 */
async function loadOffhoursTodosPage() {
    var container = document.getElementById('offhours-todos-content');
    if (!container) return;
    container.innerHTML = '<div style="padding:24px;text-align:center;color:#94a3b8;"><i class="fa-solid fa-spinner fa-spin"></i><p style="margin:6px 0 0;font-size:12px;">加载中…</p></div>';

    try {
        var results = await Promise.all([
            api.fetch('/api/offhours-todos/rules'),
            api.fetch('/api/offhours-todos/stats'),
        ]);
        _otdRenderRules(results[0] && results[0].rules ? results[0].rules : {});
        _otdRenderStats(results[1] && results[1].stats ? results[1].stats : {});
        await _otdLoadList();
    } catch (err) {
        container.innerHTML = '<div class="alert alert-error" style="margin:12px;"><strong>加载失败</strong><p style="margin:4px 0 0;color:#666;font-size:12px;">' + _otdEsc(err && err.message ? err.message : err) + '</p></div>';
    }
}

function _otdRenderRules(rules) {
    var el = document.getElementById('otd-rules');
    if (!el) return;
    var rows = [
        ['开关', rules.enabled ? '<span class="badge badge-ok">已开启</span>' : '<span class="badge badge-muted">已关闭（Web 可在系统配置中开启）</span>'],
        ['工作时段', _otdEsc(rules.workdays || '1,2,3,4,5') + ' · ' + _otdEsc(rules.work_start || '09:00') + ' — ' + _otdEsc(rules.work_end || '18:00') + ' · ' + _otdEsc(rules.timezone || 'Asia/Shanghai')],
        ['节假日', rules.holidays ? _otdEsc(rules.holidays) : '未配置'],
        ['回看窗口', _otdEsc(rules.lookback_hours || 24) + ' 小时 · 每 ' + _otdEsc(rules.scan_interval_minutes || 15) + ' 分钟扫描'],
        ['判定依据', '未读 <b>或</b> 消息之后无真人回复（AI 回复不算）'],
        ['自动建单', rules.auto_create ? '开启' : '关闭（仅记录，需手动建单）'],
        ['通知策略', (rules.notify_sender ? '回执对方' : '不回执对方') + ' · ' + (rules.notify_owner ? '通知本人' : '不通知本人')],
        ['消息来源', '平台侧实时拉取 + 本地会话库（自动补齐未同步的未读消息）'],
        ['留言过滤', '仅真人留言建单；系统推送（工作汇报日志）、纯确认词（好的/收到/ok）自动跳过'],
    ];
    var html = '<div class="otd-rule-grid">';
    for (var i = 0; i < rows.length; i++) {
        html += '<div class="otd-rule-item"><span class="otd-rule-key">' + rows[i][0] + '</span><span class="otd-rule-val">' + rows[i][1] + '</span></div>';
    }
    html += '</div>';
    el.innerHTML = html;
}

function _otdRenderStats(stats) {
    var el = document.getElementById('otd-stats');
    if (!el) return;
    var platforms = Object.keys(stats || {});
    var s = platforms.length ? (stats[platforms[0]] || {}) : {};
    var html = '<div class="otd-stat-card"><div class="otd-stat-title">当前平台概览</div>';
    html += '<div class="otd-stat-nums"><span class="otd-num warn">' + (s.pending || 0) + '</span><span class="otd-num-label">待处理</span>';
    html += '<span class="otd-num ok">' + (s.todo_created || 0) + '</span><span class="otd-num-label">已建单</span>';
    html += '<span class="otd-num muted">' + (s.replied_later || 0) + '</span><span class="otd-num-label">已回复</span></div></div>';
    el.innerHTML = html;
}

async function _otdLoadList() {
    var container = document.getElementById('offhours-todos-content');
    if (!container) return;
    var url;
    if (_otdTab === 'unanswered') {
        url = '/api/offhours-todos/unanswered?status=' + encodeURIComponent(_otdStatus)
            + '&limit=' + _OTD_PAGE_SIZE + '&offset=' + ((_otdPage - 1) * _OTD_PAGE_SIZE);
    } else {
        url = '/api/offhours-todos/records?limit=' + _OTD_PAGE_SIZE
            + '&offset=' + ((_otdPage - 1) * _OTD_PAGE_SIZE);
    }
    var data = await api.fetch(url);
    if (!data || data.error) {
        container.innerHTML = '<div class="alert alert-error" style="margin:12px;">加载列表失败：' + _otdEsc(data && data.error ? data.error : '未知错误') + '</div>';
        return;
    }
    _otdTotal = data.total || 0;
    var items = data.items || [];
    var html = _otdRenderToolbar();
    html += '<div class="otd-table-wrap"><table class="otd-table"><thead><tr>';
    if (_otdTab === 'unanswered') {
        html += '<th>会话 / 发送人</th><th>收到时间</th><th>原因</th><th>内容摘要</th><th>状态</th><th style="text-align:right">操作</th>';
    } else {
        html += '<th>待办标题</th><th>类型</th><th>回执 / 通知</th><th>创建时间</th>';
    }
    html += '</tr></thead><tbody>';

    if (!items.length) {
        var colspan = _otdTab === 'unanswered' ? 6 : 4;
        html += '<tr><td colspan="' + colspan + '" style="text-align:center;padding:28px;color:#94a3b8;"><i class="fa-solid fa-inbox" style="font-size:20px;"></i><p style="margin:6px 0 0;font-size:12px;">暂无数据</p></td></tr>';
    } else {
        for (var i = 0; i < items.length; i++) {
            var it = items[i];
            if (_otdTab === 'unanswered') {
                var who = it.sender_name || it.sender_id || '未知';
                var chat = it.chat_name || it.chat_id || '';
                html += '<tr>';
                html += '<td><div class="otd-cell-main">' + _otdEsc(who) + '</div><div class="otd-cell-sub">' + _otdEsc(chat) + '</div></td>';
                html += '<td class="otd-cell-sub">' + _otdEsc(String(it.msg_timestamp || '').replace('T', ' ').slice(0, 16)) + '</td>';
                html += '<td>' + _otdEsc(_otdReasonLabel(it.reason)) + '</td>';
                html += '<td class="otd-cell-preview">' + _otdEsc(String(it.content_preview || '').slice(0, 80)) + '</td>';
                html += '<td><span class="badge ' + _otdStatusClass(it.status) + '">' + _otdEsc(_otdStatusLabel(it.status)) + '</span></td>';
                html += '<td style="text-align:right;white-space:nowrap;">';
                if (it.status === 'pending' || it.status === 'todo_failed') {
                    // data-* 属性值一律模板字符串插值：'+变量+' 在 esbuild minify 重排后会取到错误作用域
                    html += `<button class="otd-btn primary" data-action="_otdCreate" data-args='["@el"]' data-key="${_otdEsc(it.dedupe_key)}" title="建待办"><i class="fa-solid fa-plus"></i> 建单</button>`;
                    html += `<button class="otd-btn" data-action="_otdIgnore" data-args='["@el"]' data-key="${_otdEsc(it.dedupe_key)}" title="忽略"><i class="fa-solid fa-xmark"></i></button>`;
                } else {
                    html += '<span class="otd-cell-sub">—</span>';
                }
                html += '</td></tr>';
            } else {
                html += '<tr>';
                html += '<td><div class="otd-cell-main">' + _otdEsc(it.title) + '</div><div class="otd-cell-sub">' + _otdEsc(it.todo_id || '无平台任务 ID') + '</div></td>';
                html += '<td>' + (it.todo_type === 'notify_only' ? '<span class="badge badge-warn">仅通知</span>' : '<span class="badge badge-ok">平台待办</span>') + '</td>';
                html += '<td class="otd-cell-sub">' + (it.sender_notified ? '已回执对方' : '未回执') + ' / ' + (it.owner_notified ? '已通知本人' : '汇总通知') + '</td>';
                html += '<td class="otd-cell-sub">' + _otdEsc(String(it.created_at || '').replace('T', ' ').slice(0, 16)) + '</td>';
                html += '</tr>';
            }
        }
    }
    html += '</tbody></table></div>';
    html += '<div id="otd-pagination" class="otd-pagination"></div>';
    container.innerHTML = html;
    renderPager('otd-pagination', { total: _otdTotal, page: _otdPage, pageSize: _OTD_PAGE_SIZE }, function (p) {
        _otdPage = p;
        _otdLoadList();
    });
}

function _otdRenderToolbar() {
    var tabs = '<div class="otd-tabs">'
        + `<button class="otd-tab${_otdTab === 'unanswered' ? ' active' : ''}" data-action="_otdSwitchTab" data-args='["@el"]' data-tab="unanswered">未回复消息</button>`
        + `<button class="otd-tab${_otdTab === 'records' ? ' active' : ''}" data-action="_otdSwitchTab" data-args='["@el"]' data-tab="records">待办创建记录</button>`
        + '</div>';
    var filters = '<div class="otd-filters">';
    if (_otdTab === 'unanswered') {
        filters += '<select data-action="_otdSetStatus" data-args=\'["@el"]\'>';
        var opts = [['pending', '待处理'], ['todo_created', '已建待办'], ['notified_only', '仅通知'], ['replied_later', '已回复'], ['ignored', '已忽略'], ['all', '全部']];
        for (var i = 0; i < opts.length; i++) {
            filters += '<option value="' + opts[i][0] + '"' + (_otdStatus === opts[i][0] ? ' selected' : '') + '>' + opts[i][1] + '</option>';
        }
        filters += '</select>';
    }
    filters += '<span style="font-size:12px;color:#94a3b8;">共 ' + _otdTotal + ' 条</span></div>';
    var actions = '<div class="otd-actions">'
        + '<button class="otd-btn primary" data-action="_otdScan" ' + (_otdBusy ? 'disabled' : '') + '><i class="fa-solid fa-rotate"></i> 立即扫描</button>'
        + '<button class="otd-btn" data-action="_otdSync" ' + (_otdBusy ? 'disabled' : '') + '><i class="fa-solid fa-arrows-rotate"></i> 同步状态</button>'
        + '<button class="otd-btn" data-action="loadOffhoursTodosPage"><i class="fa-solid fa-refresh"></i> 刷新</button>'
        + '</div>';
    return '<div class="otd-toolbar">' + tabs + '<div class="otd-toolbar-right">' + filters + actions + '</div></div>';
}

function _otdSwitchTab(el) {
    _otdTab = el.dataset.tab || 'unanswered';
    _otdPage = 1;
    _otdLoadList();
}

function _otdSetStatus(el) { _otdStatus = el.value || 'pending'; _otdPage = 1; _otdLoadList(); }

async function _otdScan() {
    if (_otdBusy) return;
    if (!window.confirm('立即扫描当前平台并自动创建待办？重复扫描不会重复建单。')) return;
    _otdBusy = true;
    showToast('正在扫描…', 'info');
    try {
        var res = await api.post('/api/offhours-todos/scan', { create_todos: true, dry_run: false });
        if (res && res.error) { showToast('扫描失败：' + res.error, 'error'); return; }
        var r = (res && res.result) || {};
        showToast('扫描完成：工作时间外 ' + (r.offhours || 0) + ' 条，建单 ' + (r.todos_created || 0) + ' 条，仅通知 ' + (r.notify_only || 0) + ' 条', 'success');
        loadOffhoursTodosPage();
    } catch (err) {
        showToast('扫描失败：' + (err && err.message ? err.message : err), 'error');
    } finally {
        _otdBusy = false;
    }
}

async function _otdSync() {
    if (_otdBusy) return;
    _otdBusy = true;
    showToast('正在同步状态…', 'info');
    try {
        var res = await api.post('/api/offhours-todos/sync', {});
        if (res && res.error) { showToast('同步失败：' + res.error, 'error'); return; }
        showToast('同步完成', 'success');
        loadOffhoursTodosPage();
    } catch (err) {
        showToast('同步失败：' + (err && err.message ? err.message : err), 'error');
    } finally {
        _otdBusy = false;
    }
}

async function _otdCreate(el) {
    var key = el && el.dataset ? el.dataset.key : '';
    if (!key) return;
    showToast('正在创建待办…', 'info');
    try {
        var res = await api.post('/api/offhours-todos/create', { dedupe_key: key });
        if (res && res.error) { showToast('建单失败：' + res.error, 'error'); return; }
        showToast(res && res.kind === 'notify_only' ? '已给对方发送提醒（该平台不支持个人待办）' : '待办已创建并通知对方', 'success');
        loadOffhoursTodosPage();
    } catch (err) {
        showToast('建单失败：' + (err && err.message ? err.message : err), 'error');
    }
}

async function _otdIgnore(el) {
    var key = el && el.dataset ? el.dataset.key : '';
    if (!key) return;
    try {
        var res = await api.post('/api/offhours-todos/ignore', { dedupe_key: key });
        if (res && res.error) { showToast('忽略失败：' + res.error, 'error'); return; }
        showToast('已忽略', 'success');
        loadOffhoursTodosPage();
    } catch (err) {
        showToast('忽略失败：' + (err && err.message ? err.message : err), 'error');
    }
}

// 显式挂全局：浏览器经典 script 下顶层函数本就是 window 属性，这里额外暴露
// 是为了 vitest（ESM 转换后顶层函数不再自动全局）与调试台可直接调用。
window.loadOffhoursTodosPage = loadOffhoursTodosPage;
window._otdSwitchTab = _otdSwitchTab;
window._otdSetStatus = _otdSetStatus;
window._otdScan = _otdScan;
window._otdSync = _otdSync;
window._otdCreate = _otdCreate;
window._otdIgnore = _otdIgnore;
