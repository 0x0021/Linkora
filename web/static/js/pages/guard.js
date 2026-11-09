/* pages/guard.js — 入站消息安全防护态势页
 *
 * 防护对象是「别人从 IM 发过来的消息」，本页只做展示与策略调参：
 *   - 概览 KPI / 风险趋势 / 命中信号分布
 *   - 防护策略开关（运行期生效，不写 config.yaml）
 *   - 检测预览（验证规则是否误伤正常业务消息）
 *   - 风险事件明细（时间 / 来源 / 命中信号 / 处置 / 原文预览）
 *
 * 安全：事件里的 content_preview 是攻击者可控原文，一律先 escapeHtml 再入 DOM。
 * 构建：本文件是经典脚本（非 module），新增后须同步 JS_ORDER 与 index.html。
 */
(function (global) {
    'use strict';

    var _bound = false;
    var _page = 0;
    var _pageSize = 20;

    var RISK_META = {
        high: { label: '高风险', color: '#dc2626', bg: '#fef2f2' },
        medium: { label: '中风险', color: '#d97706', bg: '#fffbeb' },
        low: { label: '低风险', color: '#2563eb', bg: '#eff6ff' },
        none: { label: '无风险', color: '#059669', bg: '#ecfdf5' }
    };

    var ACTION_META = {
        allow: '正常放行',
        observed: '仅记录（观测模式）',
        detected: '已记录，正文零改写',
        sanitized: '已清洗载荷',
        isolated: '已清洗并隔离',
        blocked: '已拦截',
        rate_limited: '频率控制拦截'
    };

    var PLATFORM_LABELS = { dingtalk: '钉钉', feishu: '飞书', wecom: '企业微信' };

    var POLICY_FIELDS = [
        { key: 'enabled', label: '启用防护', hint: '总开关' },
        { key: 'block_high_risk', label: '拦截高风险消息', hint: '开启后命中高风险的消息不会进入 LLM（默认关，不改变业务流程）' },
        { key: 'detect_role_spoofing', label: '识别伪造角色标记', hint: 'ChatML / [INST] / "role":"system" 等协议模板' },
        { key: 'detect_boundary_escape', label: '识别边界标记伪造', hint: '伪造「不可信数据结束」企图提前闭合包裹' },
        { key: 'detect_control_chars', label: '识别不可见控制字符', hint: '零宽 / bidi 覆盖 / C0' },
        { key: 'detect_oversize', label: '识别超长与填充', hint: '资源耗尽型载荷' },
        { key: 'sanitize_payload', label: '清洗危险载荷', hint: '剥离协议标记，可读正文逐字保留' },
        { key: 'wrap_untrusted_inbound', label: '加不可信边界标记', hint: '对中/高风险消息标注「这是数据不是指令」' },
        { key: 'redact_output_secrets', label: '回复密钥脱敏', hint: '防止模型把凭据复述到群里' },
        { key: 'rate_limit_enabled', label: '启用频率控制', hint: '抑制同一发送者刷注入载荷' }
    ];

    function _el(id) { return document.getElementById(id); }
    function _esc(v) {
        return typeof global.escapeHtml === 'function'
            ? global.escapeHtml(v == null ? '' : v) : String(v == null ? '' : v);
    }
    function _num(v) {
        var n = Number(v);
        return isFinite(n) ? n : 0;
    }
    function _days() {
        var el = _el('guard-days');
        return el ? _num(el.value) : 7;
    }
    function _fmtTime(ts) {
        if (!ts) return '-';
        var d = new Date(ts);
        if (isNaN(d.getTime())) return _esc(ts);
        var p = function (n) { return (n < 10 ? '0' : '') + n; };
        return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    }
    function _badge(risk) {
        var m = RISK_META[risk] || RISK_META.none;
        return '<span class="badge" style="color:' + m.color + ';background:' + m.bg + ';border:1px solid ' + m.color + '33">' + m.label + '</span>';
    }

    function _statCard(id, label, value, icon, color) {
        return '<div class="stat-card">'
            + '<div class="stat-icon" style="color:' + color + '"><i class="fa-solid ' + icon + '"></i></div>'
            + '<div class="stat-info"><div class="stat-label">' + label + '</div>'
            + '<div class="stat-value" id="' + id + '">' + value + '</div></div></div>';
    }

    function _renderKpis(data) {
        var box = _el('guard-kpis');
        if (!box) return;
        var risk = data.risk || {};
        box.innerHTML = _statCard('guard-kpi-inspected', '已检测入站消息', _num(data.inspected_total), 'fa-inbox', '#2563eb')
            + _statCard('guard-kpi-events', '风险事件', _num(data.total), 'fa-triangle-exclamation', '#d97706')
            + _statCard('guard-kpi-high', '高风险', _num(risk.high), 'fa-shield-halved', '#dc2626')
            + _statCard('guard-kpi-blocked', '已拦截', _num(data.blocked), 'fa-ban', '#7c3aed');
    }

    function _renderTrend(timeline) {
        var box = _el('guard-trend');
        if (!box) return;
        var rows = Array.isArray(timeline) ? timeline.slice(-14) : [];
        if (!rows.length) {
            box.innerHTML = '<div class="empty-state" style="padding:20px"><p>当前时间范围内暂无风险事件</p></div>';
            return;
        }
        var max = 1;
        rows.forEach(function (r) { max = Math.max(max, _num(r.total)); });
        var bars = rows.map(function (r) {
            var h = Math.max(3, Math.round(_num(r.total) / max * 76));
            var hh = Math.max(0, Math.round(_num(r.high) / max * 76));
            return '<div style="flex:1;min-width:26px;text-align:center" title="' + _esc(r.date) + '：共 ' + _num(r.total) + ' 条 / 高风险 ' + _num(r.high) + ' 条">'
                + '<div style="height:84px;display:flex;flex-direction:column;justify-content:flex-end;align-items:center">'
                + '<div style="width:22px;height:' + h + 'px;background:#93c5fd;border-radius:3px 3px 0 0;position:relative">'
                + '<div style="position:absolute;bottom:0;left:0;right:0;height:' + hh + 'px;background:#dc2626;border-radius:3px 3px 0 0"></div>'
                + '</div></div>'
                + '<div style="font-size:10px;color:var(--text-muted,#888);margin-top:5px;white-space:nowrap">' + _esc(String(r.date).slice(5)) + '</div>'
                + '</div>';
        }).join('');
        box.innerHTML = '<div style="display:flex;gap:6px;align-items:flex-end;overflow-x:auto;padding:4px 0">' + bars + '</div>'
            + '<div style="font-size:11px;color:var(--text-muted,#888);margin-top:8px">'
            + '<span style="color:#dc2626">■</span> 高风险　<span style="color:#93c5fd">■</span> 全部风险</div>';
    }

    // 命中信号分布单独渲染，避免与趋势图耦合
    function _renderSignals(signals, actions) {
        var box = _el('guard-signals');
        if (!box) return;
        var labels = {
            boundary_escape: '伪造边界标记', role_spoofing: '伪造角色标记',
            role_label: '行首角色标签', control_chars: '不可见控制字符', oversize: '超长消息',
            repeat_flood: '字符填充', rate_limited: '频率超限'
        };
        var entries = Object.keys(signals || {}).map(function (k) { return [k, _num(signals[k])]; });
        if (!entries.length) {
            box.innerHTML = '<div style="font-size:12px;color:var(--text-muted,#888);padding:8px 0">暂无命中信号</div>';
        } else {
            var max = 1;
            entries.forEach(function (e) { max = Math.max(max, e[1]); });
            box.innerHTML = entries.map(function (e) {
                return '<div style="display:flex;align-items:center;gap:8px;margin:6px 0;font-size:12px">'
                    + '<span style="width:110px;flex-shrink:0">' + _esc(labels[e[0]] || e[0]) + '</span>'
                    + '<div style="flex:1;background:var(--bg-subtle,#f1f5f9);border-radius:3px;height:10px"><div style="width:' + Math.round(e[1] / max * 100) + '%;height:10px;background:#6366f1;border-radius:3px"></div></div>'
                    + '<span style="width:32px;text-align:right">' + e[1] + '</span></div>';
            }).join('');
        }
        var aBox = _el('guard-actions');
        if (aBox) {
            var acts = Object.keys(actions || {});
            aBox.innerHTML = acts.length
                ? acts.map(function (k) {
                    return '<span class="badge badge-secondary" style="margin:2px 4px 2px 0">' + _esc(ACTION_META[k] || k) + ' ' + _num(actions[k]) + '</span>';
                }).join('')
                : '<span style="font-size:12px;color:var(--text-muted,#888)">暂无处置记录</span>';
        }
    }

    function _renderPolicy(policy, overrideActive) {
        var box = _el('guard-policy');
        if (!box) return;
        var p = policy || {};
        var badge = _el('guard-override-badge');
        if (badge) {
            badge.textContent = overrideActive ? '运行期覆盖中' : 'config.yaml 配置';
            badge.style.color = overrideActive ? '#d97706' : '#059669';
        }
        var modeEl = _el('guard-mode');
        if (modeEl) modeEl.value = p.mode || 'enforce';
        box.innerHTML = '<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px">'
            + '<label style="font-size:12px">处置模式 '
            + '<select id="guard-mode" class="form-control form-control-sm" style="display:inline-block;width:auto">'
            + '<option value="enforce">enforce（清洗 + 隔离）</option>'
            + '<option value="observe">observe（只记录不改写）</option></select></label>'
            + '<span style="font-size:11px;color:var(--text-muted,#888)">只识别协议级结构标记，不做语义检测，正常业务消息零改写</span>'
            + '</div>'
            + '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px 18px">'
            + POLICY_FIELDS.map(function (f) {
                var checked = p[f.key] ? ' checked' : '';
                return '<label class="form-check" style="display:flex;gap:8px;align-items:flex-start;font-size:12px" title="' + _esc(f.hint) + '">'
                    + `<input type="checkbox" class="form-check-input" data-guard-field="${f.key}"${checked}>`
                    + '<span><strong>' + _esc(f.label) + '</strong><br><span style="color:var(--text-muted,#888)">' + _esc(f.hint) + '</span></span></label>';
            }).join('')
            + '</div>'
            + '<div style="margin-top:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
            + '<button class="btn btn-sm btn-primary" id="guard-policy-save">保存策略</button>'
            + '<button class="btn btn-sm btn-outline-secondary" id="guard-policy-reset">恢复 config.yaml 配置</button>'
            + '<span style="font-size:11px;color:var(--text-muted,#888)">运行期调整不写入 config.yaml，重启后回到 yaml 配置</span>'
            + '</div>';
        var modeSel = _el('guard-mode');
        if (modeSel) modeSel.value = p.mode || 'enforce';
    }

    function _collectPolicy() {
        var out = {};
        POLICY_FIELDS.forEach(function (f) {
            var el = document.querySelector('[data-guard-field="' + f.key + '"]');
            out[f.key] = !!(el && el.checked);
        });
        var modeEl = _el('guard-mode');
        out.mode = modeEl ? modeEl.value : 'enforce';
        return out;
    }

    function _renderEvents(items, total) {
        var box = _el('guard-events');
        if (!box) return;
        if (!items || !items.length) {
            box.innerHTML = '<div class="empty-state" style="padding:28px"><i class="fa-solid fa-shield-heart empty-icon"></i>'
                + '<p>当前筛选条件下没有风险事件 —— 说明别人发来的消息都是正常的</p></div>';
            return;
        }
        var rows = items.map(function (it) {
            var signals = (it.signal_labels || it.signals || []).map(function (s) {
                return '<span class="badge badge-secondary" style="margin:1px">' + _esc(s) + '</span>';
            }).join('') || '—';
            return '<tr>'
                + '<td style="white-space:nowrap;font-size:11px">' + _fmtTime(it.ts) + '</td>'
                + '<td>' + _esc(PLATFORM_LABELS[it.platform] || it.platform || '-') + '</td>'
                + '<td>' + _esc(it.sender_name || '-') + '<br><span style="font-size:10px;color:var(--text-muted,#888)">' + _esc(it.chat_name || '-') + '</span></td>'
                + '<td>' + _badge(it.risk) + '</td>'
                + '<td>' + signals + '</td>'
                + '<td style="white-space:nowrap">' + _esc(it.action_label || ACTION_META[it.action] || it.action || '-') + '</td>'
                + '<td style="max-width:320px"><div style="font-size:11px;white-space:pre-wrap;word-break:break-all;max-height:60px;overflow:auto">' + _esc(it.content_preview || '') + '</div></td>'
                + '</tr>';
        }).join('');
        var from = _page * _pageSize + 1;
        var to = Math.min(total, (_page + 1) * _pageSize);
        var hasPrev = _page > 0;
        var hasNext = to < total;
        box.innerHTML = '<div class="table-responsive"><table class="table table-sm" style="font-size:12px">'
            + '<thead><tr><th>时间</th><th>平台</th><th>发送者 / 会话</th><th>风险</th><th>命中信号</th><th>处置</th><th>原文预览</th></tr></thead>'
            + '<tbody>' + rows + '</tbody></table></div>'
            + '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:10px;font-size:12px">'
            + '<span>共 ' + _num(total) + ' 条，显示 ' + from + '-' + to + '</span>'
            + '<span><button class="btn btn-sm btn-outline-secondary" data-guard-page="prev"' + (hasPrev ? '' : ' disabled') + '>上一页</button> '
            + '<button class="btn btn-sm btn-outline-secondary" data-guard-page="next"' + (hasNext ? '' : ' disabled') + '>下一页</button></span></div>';
    }

    function _queryUrl() {
        var params = {
            limit: _pageSize,
            offset: _page * _pageSize,
            days: _days(),
            risk: (_el('guard-risk-filter') || {}).value || '',
            action: (_el('guard-action-filter') || {}).value || '',
            platform: (_el('guard-platform-filter') || {}).value || '',
            keyword: (_el('guard-keyword') || {}).value || ''
        };
        return params;
    }

    async function _loadStats() {
        try {
            var data = await global.api.get('/api/guard/stats', { days: _days() });
            if (!data || data.error || data.success === false) {
                if (typeof global.showToast === 'function') global.showToast('加载防护统计失败', 'error');
                return;
            }
            _renderKpis(data);
            _renderTrend(data.timeline);
            _renderSignals(data.signals, data.actions);
            _renderPolicy(data.policy, data.override_active);
        } catch (e) {
            if (typeof global.showToast === 'function') global.showToast('加载防护统计失败', 'error');
        }
    }

    async function _loadEvents() {
        var box = _el('guard-events');
        if (box && !box.innerHTML) box.innerHTML = '<div class="empty-state" style="padding:20px">加载中…</div>';
        try {
            var data = await global.api.get('/api/guard/events', _queryUrl());
            if (!data || data.error || data.success === false) {
                if (box) box.innerHTML = '<div class="empty-state" style="padding:20px">加载失败</div>';
                return;
            }
            _renderEvents(data.items, data.total);
        } catch (e) {
            if (box) box.innerHTML = '<div class="empty-state" style="padding:20px">加载失败</div>';
        }
    }

    function _refresh() {
        _loadStats();
        _loadEvents();
    }

    function loadGuardPage() {
        _bind();
        _page = 0;
        _refresh();
    }

    async function runGuardPreview() {
        var input = _el('guard-preview-input');
        var out = _el('guard-preview-result');
        if (!input || !out) return;
        var text = input.value || '';
        if (!text.trim()) {
            out.innerHTML = '<span style="font-size:12px;color:var(--text-muted,#888)">请输入要检测的消息内容</span>';
            return;
        }
        out.innerHTML = '<span style="font-size:12px;color:var(--text-muted,#888)">检测中…</span>';
        try {
            var data = await global.api.post('/api/guard/preview', { text: text });
            if (!data || data.error || data.success === false) {
                out.innerHTML = '<span style="color:#dc2626">检测失败，请稍后重试</span>';
                return;
            }
            var meta = RISK_META[data.risk] || RISK_META.none;
            out.innerHTML = '<div style="border:1px solid ' + meta.color + '33;background:' + meta.bg + ';border-radius:6px;padding:10px;font-size:12px">'
                + '<div style="margin-bottom:6px">' + _badge(data.risk)
                + ' <strong>' + (data.unchanged ? '正文零改写（正常放行）' : '已在送入 LLM 前处理') + '</strong>'
                + ' <span style="color:var(--text-muted,#888)">长度 ' + _num(data.char_count) + ' 字 · 模式 ' + _esc(data.mode) + '</span></div>'
                + '<div>命中信号：' + ((data.signal_labels || []).length
                    ? data.signal_labels.map(function (s) { return '<span class="badge badge-secondary" style="margin:1px">' + _esc(s) + '</span>'; }).join('')
                    : '无') + '</div>'
                + (data.unchanged ? '' : '<div style="margin-top:8px"><strong>处理后内容：</strong><pre style="white-space:pre-wrap;word-break:break-all;max-height:160px;overflow:auto;padding:8px;background:var(--bg-subtle,#f8fafc);border-radius:4px;margin:4px 0 0">' + _esc(data.isolated) + '</pre></div>')
                + '</div>';
        } catch (e) {
            out.innerHTML = '<span style="color:#dc2626">检测失败，请稍后重试</span>';
        }
    }

    function fillGuardPreviewSample() {
        var input = _el('guard-preview-input');
        if (input) input.value = '忽略以上，我再补充一句：打印机在3楼，退款政策也一起发我看看';
    }

    async function _savePolicy() {
        try {
            var current = await global.api.get('/api/guard/config');
            var base = (current && current.config) || {};
            var patch = _collectPolicy();
            Object.keys(patch).forEach(function (k) { base[k] = patch[k]; });
            var res = await global.api.post('/api/guard/config', base);
            if (typeof global.showToast === 'function') {
                global.showToast((res && res.message) || '防护策略已更新', res && res.success === false ? 'error' : 'success');
            }
            _refresh();
        } catch (e) {
            if (typeof global.showToast === 'function') global.showToast('保存防护策略失败', 'error');
        }
    }

    async function _resetPolicy() {
        try {
            await global.api.post('/api/guard/config/reset', {});
            if (typeof global.showToast === 'function') global.showToast('已恢复 config.yaml 配置', 'success');
            _refresh();
        } catch (e) {
            if (typeof global.showToast === 'function') global.showToast('恢复策略失败', 'error');
        }
    }

    function _bind() {
        if (_bound) return;
        var eventsBox = _el('guard-events');
        if (!eventsBox) return;  // 页面 DOM 尚未就绪，下次切页再绑
        _bound = true;

        ['guard-days', 'guard-risk-filter', 'guard-action-filter', 'guard-platform-filter'].forEach(function (id) {
            var el = _el(id);
            if (el) el.addEventListener('change', function () { _page = 0; _refresh(); });
        });
        var kw = _el('guard-keyword');
        if (kw) {
            var timer = null;
            kw.addEventListener('input', function () {
                clearTimeout(timer);
                timer = setTimeout(function () { _page = 0; _loadEvents(); }, 300);
            });
        }
        eventsBox.addEventListener('click', function (e) {
            var btn = e.target && e.target.closest ? e.target.closest('[data-guard-page]') : null;
            if (!btn) return;
            var dir = btn.getAttribute('data-guard-page');
            _page = Math.max(0, _page + (dir === 'next' ? 1 : -1));
            _loadEvents();
        });
        var refresh = _el('guard-refresh');
        if (refresh) refresh.addEventListener('click', _refresh);
        var preview = _el('guard-preview-run');
        if (preview) preview.addEventListener('click', runGuardPreview);
        var sample = _el('guard-preview-sample');
        if (sample) sample.addEventListener('click', fillGuardPreviewSample);
        // 策略区的按钮是动态渲染的，用容器级委托
        var policyBox = _el('guard-policy');
        if (policyBox) {
            policyBox.addEventListener('click', function (e) {
                var t = e.target;
                if (t && t.id === 'guard-policy-save') _savePolicy();
                if (t && t.id === 'guard-policy-reset') _resetPolicy();
            });
        }
    }

    global.loadGuardPage = loadGuardPage;
    global.runGuardPreview = runGuardPreview;
    global.fillGuardPreviewSample = fillGuardPreviewSample;
})(window);
