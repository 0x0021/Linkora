// ============ pages/simulate.js ============
// 模拟测试面板：不实际发送消息，验证 LLM 技能调用和回复效果

let _simulateSamples = [];
let _simHistory = [];

/**
 * 轻量级 Markdown → HTML 渲染器（仅支持常用语法，自动转义防止 XSS）。
 * 处理流程：先对纯文本做 HTML 转义，再把 markdown 标记替换为安全标签。
 */
function renderMarkdown(text) {
    if (!text) return '';

    const escape = (s) => s
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

    // 保护代码块与行内代码，避免被其它规则破坏
    const codeBlocks = [];
    const inlineCodes = [];

    let html = escape(text)
        .replace(/```([\s\S]*?)```/g, function (_, code) {
            codeBlocks.push('<pre class="md-code-block"><code>' + code.replace(/^\n|\n$/g, '') + '</code></pre>');
            return '\u0000CB' + (codeBlocks.length - 1) + '\u0000';
        })
        .replace(/`([^`\n]+)`/g, function (_, code) {
            inlineCodes.push('<code class="md-inline-code">' + code + '</code>');
            return '\u0000IC' + (inlineCodes.length - 1) + '\u0000';
        });

    // 标题
    html = html.replace(/^###### (.*$)/gim, '<h6 class="md-h">$1</h6>');
    html = html.replace(/^##### (.*$)/gim, '<h5 class="md-h">$1</h5>');
    html = html.replace(/^#### (.*$)/gim, '<h4 class="md-h">$1</h4>');
    html = html.replace(/^### (.*$)/gim, '<h3 class="md-h">$1</h3>');
    html = html.replace(/^## (.*$)/gim, '<h2 class="md-h">$1</h2>');
    html = html.replace(/^# (.*$)/gim, '<h1 class="md-h">$1</h1>');

    // 加粗 **text** 或 __text__
    html = html.replace(/\*\*([^\*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');

    // 斜体 *text* 或 _text_（不与加粗重叠）
    html = html.replace(/(^|[^*])\*([^*\n]+)\*([^*]|$)/g, '$1<em>$2</em>$3');
    html = html.replace(/(^|[^_])_([^_\n]+)_([^_]|$)/g, '$1<em>$2</em>$3');

    // 删除线 ~~text~~
    html = html.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

    // 引用
    html = html.replace(/^&gt; (.*$)/gim, '<blockquote class="md-blockquote">$1</blockquote>');

    // 列表项识别（标记类型，保留换行）
    html = html.replace(/^(\s*)[-*+] (.*$)/gim, function (_, indent, item) {
        return '<li class="md-li" data-list="ul">' + item + '</li>';
    });
    html = html.replace(/^(\s*)\d+\. (.*$)/gim, function (_, indent, item) {
        return '<li class="md-li" data-list="ol">' + item + '</li>';
    });

    // 段落与换行
    const blocks = html.split(/\n\n+/);
    html = blocks.map(function (block) {
        block = block.trim();
        if (!block) return '';
        if (/^<(h[1-6]|pre|blockquote)/i.test(block)) return block;

        // 含有代码块占位符的块直接恢复，不包段落
        if (/\u0000CB\d+\u0000/.test(block)) {
            return block.replace(/\u0000CB(\d+)\u0000/g, function (_, i) { return codeBlocks[i]; });
        }

        // 按行拆分，把相邻 li 收集成 ul/ol
        const lines = block.split('\n');
        const out = [];
        let listBuffer = [];
        let listType = null;

        function flushList() {
            if (listBuffer.length === 0) return;
            const tag = listType === 'ol' ? 'ol' : 'ul';
            out.push('<' + tag + ' class="md-' + tag + '">' + listBuffer.join('') + '</' + tag + '>');
            listBuffer = [];
            listType = null;
        }

        lines.forEach(function (line) {
            const liMatch = line.match(/^<li class="md-li" data-list="(ul|ol)">(.*)<\/li>$/);
            if (liMatch) {
                if (listType && listType !== liMatch[1]) flushList();
                listType = liMatch[1];
                listBuffer.push('<li class="md-li">' + liMatch[2] + '</li>');
            } else {
                flushList();
                if (line.trim()) out.push(line);
            }
        });
        flushList();

        // 非列表的连续行包成段落；列表保持独立
        const parts = [];
        let paraLines = [];
        out.forEach(function (piece) {
            if (/^<(ul|ol)/i.test(piece)) {
                if (paraLines.length) {
                    parts.push('<p class="md-p">' + paraLines.join('<br>') + '</p>');
                    paraLines = [];
                }
                parts.push(piece);
            } else {
                paraLines.push(piece);
            }
        });
        if (paraLines.length) {
            parts.push('<p class="md-p">' + paraLines.join('<br>') + '</p>');
        }
        return parts.join('');
    }).join('\n');

    // 恢复行内代码占位符
    html = html.replace(/\u0000IC(\d+)\u0000/g, function (_, i) { return inlineCodes[i]; });

    return html;
}

async function loadSimulatePage() {
    // 立即把右栏替换为「状态 banner + 3 张固定占位卡」的标准结构，
    // 避免初次加载或 API 失败时仅显示裸 banner，底部 3 张卡（路由/工具/证据）被裁。
    _initSimulateResultShell();

    try {
        const [samplesRes, statusRes, historyRes] = await Promise.all([
            api.get('/api/simulate/sample-messages'),
            api.get('/api/simulate/status'),
            api.get('/api/simulate/history'),
        ]);

        if (samplesRes && samplesRes.samples) {
            _simulateSamples = samplesRes.samples;
            renderSampleMessages();
        }

        if (statusRes && statusRes.data) {
            renderSimStatus(statusRes.data);
        }

        if (historyRes && historyRes.history) {
            renderSimHistory(historyRes.history);
        }
    } catch (e) {
        console.error('加载模拟测试页面失败:', e);
    }
}

async function refreshSimStatus() {
    try {
        const res = await api.get('/api/simulate/status');
        if (res && res.data) {
            renderSimStatus(res.data);
        }
    } catch (e) {
        console.error('刷新状态失败:', e);
    }
}

function renderSimStatus(data) {
    const llm = data.llm || {};
    const skills = data.skills || {};
    const system = data.system || {};

    const llmStatusEl = document.getElementById('sim-llm-status');
    const llmIconEl = document.getElementById('sim-llm-icon');
    if (llmStatusEl && llmIconEl) {
        llmStatusEl.textContent = llm.available ? '正常' : '不可用';
        const chipEl = llmIconEl.parentElement;
        chipEl.className = 'sim-status-chip ' + (llm.available ? 'success' : 'error');
    }

    const skillCountEl = document.getElementById('sim-skill-count');
    if (skillCountEl) {
        skillCountEl.textContent = skills.count || 0;
    }

    const modelNameEl = document.getElementById('sim-model-name');
    if (modelNameEl) {
        modelNameEl.textContent = llm.active_model || (llm.models && llm.models[0]) || '--';
    }

    const versionEl = document.getElementById('sim-version');
    if (versionEl) {
        versionEl.textContent = system.version || '--';
    }
}

function renderSampleMessages() {
    const container = document.getElementById('simulate-samples');
    if (!container) return;

    const icons = {
        '天气查询': 'fa-cloud-sun',
        '知识问答': 'fa-circle-question',
        '闲聊': 'fa-comments',
        '工具调用': 'fa-screwdriver-wrench',
        '复杂查询': 'fa-chart-line',
    };

    container.innerHTML = _simulateSamples.map((sample, idx) => {
        const icon = icons[sample.name] || 'fa-play';
        return `
            <button class="sim-sample-btn" onclick="useSample(${idx})">
                <i class="fa-solid ${icon}"></i> ${escapeHtml(sample.name)}
            </button>
        `;
    }).join('');
}

function renderSimHistory(history) {
    if (history) _simHistory = history;
    var items = _simHistory;

    // Client-side filter from search input
    var input = document.getElementById('sim-search');
    var query = (input ? input.value : '').trim().toLowerCase();
    if (query) {
        items = items.filter(function(item) {
            return ((item.content || '').toLowerCase().indexOf(query) >= 0) ||
                   ((item.sender_name || '').toLowerCase().indexOf(query) >= 0) ||
                   ((item.skill_name || '').toLowerCase().indexOf(query) >= 0);
        });
    }

    const container = document.getElementById('sim-history-list');
    if (!container) return;

    if (!items || items.length === 0) {
        container.innerHTML = '<div class="sim-history-empty">暂无测试记录</div>';
        return;
    }

    container.innerHTML = items.map(item => {
        const time = item.timestamp ? new Date(item.timestamp).toLocaleTimeString('zh-CN') : '';
        return `
            <div class="sim-history-item" onclick="useHistory('${escapeHtml(item.content)}', '${escapeHtml(item.sender_name || '')}')">
                <div class="sim-history-content">${escapeHtml(item.content)}</div>
                <div class="sim-history-meta">
                    <span><i class="fa-solid fa-user"></i> ${escapeHtml(item.sender_name || '测试用户')}</span>
                    <span><i class="fa-solid fa-clock"></i> ${time}</span>
                    ${item.skill_name ? `<span><i class="fa-solid fa-wrench"></i> ${escapeHtml(item.skill_name)}</span>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

async function clearSimHistory() {
    _sim_history = [];
    renderSimHistory([]);
    showToast('测试历史已清空', 'success');
}

function useSample(idx) {
    const sample = _simulateSamples[idx];
    if (!sample) return;

    document.getElementById('simulate-content').value = sample.content;
    document.getElementById('simulate-sender').value = sample.sender_name;
    updateSimAvatar(sample.sender_name);
}

function useHistory(content, senderName) {
    document.getElementById('simulate-content').value = content;
    document.getElementById('simulate-sender').value = senderName;
    updateSimAvatar(senderName);
}

function updateSimAvatar(name) {
    const avatar = document.getElementById('sim-avatar');
    if (!avatar) return;
    const ch = (name || '测').trim().charAt(0) || '测';
    avatar.textContent = ch;
}

function _setResultStatus(status) {
    const dot = document.getElementById('sim-result-status-dot');
    if (dot) dot.className = 'sim-result-status-dot ' + status;
}

// 3 张附属卡片的 HTML 已在 index.html 模板里固定渲染（本函数已废弃，保留空函数占位以防旧引用报错）
function _placeholderCards() {
    return '';
}

// 拼装右栏结果区：本函数已废弃，3 张卡固定在 HTML 模板里、JS 只更新内容
function _buildResultShell(statusBanner) {
    return statusBanner || '';
}

// 空状态提示 HTML（仅「等待执行」占位，不再附带回复卡骨架）。
// 设计原则：「等待执行」空状态与「AI 回复」结果卡互斥——
// 空状态可见时结果插槽隐藏，有执行结果时空状态隐藏，二者永不并存。
function _idleEmptyHtml(title = '等待执行', text = '发送测试消息后，执行结果将在此展示', icon = 'fa-terminal', iconColor = '') {
    const colorStyle = iconColor ? ` style="color:${iconColor};"` : '';
    return `
        <div class="sim-result-empty">
            <div class="sim-result-empty-icon"${colorStyle}><i class="fa-solid ${icon}"></i></div>
            <div class="sim-result-empty-title">${escapeHtml(title)}</div>
            <div class="sim-result-empty-text">${escapeHtml(text)}</div>
        </div>
    `;
}

// 「AI 回复」占位 HTML：让 AI 回复卡在 idle/清空/错误状态下一直显示，保持布局稳定
// （3 张固定卡始终贴底不漂移）。是「AI 未返回文本内容」语义的视觉占位。
function _idleReplyCardHtml(text = '发送测试消息后，AI 回复将在此展示') {
    return `
        <div class="sim-card sim-card-reply">
            <div class="sim-card-header">
                <div class="sim-card-icon sim-icon-robot"><i class="fa-solid fa-robot"></i></div>
                <div class="sim-card-title">AI 回复</div>
            </div>
            <div class="sim-card-body">
                <div class="sim-chat-bubble sim-chat-empty md-content">
                    <div class="sim-chat-empty-text">${escapeHtml(text)}</div>
                </div>
            </div>
        </div>
    `;
}

// 显示「等待执行」空状态，并清空/隐藏结果插槽（首次加载、清空时调用）
function _showIdleState() {
    const emptyEl = document.getElementById('sim-empty-state');
    const slotEl = document.getElementById('sim-result-slot');
    // idle 状态：把结果插槽填入占位 AI 回复卡，让「AI 回复」卡骨架始终在 DOM
    // （与下方 3 张固定卡形成稳定的 1+3 布局，不再闪烁/跳动）。
    if (slotEl) { slotEl.style.display = ''; slotEl.innerHTML = _idleReplyCardHtml(); }
    if (emptyEl) emptyEl.style.display = 'none';
}

// 隐藏「等待执行」空状态，把执行结果（loading / alert / AI 回复卡等）填入结果插槽
function _showResultSlot(html) {
    const emptyEl = document.getElementById('sim-empty-state');
    const slotEl = document.getElementById('sim-result-slot');
    if (emptyEl) emptyEl.style.display = 'none';
    if (slotEl) { slotEl.style.display = ''; slotEl.innerHTML = html; }
}

// ============ 「3 张固定卡」的就地更新 helpers ============
// HTML 模板已经把 .sim-result-content（banner 区）+ .sim-card-dashboard（3 卡）写死，
// JS 只更新各区内容，不再重建卡片结构。这样 3 张卡始终在 DOM 里、位置永不漂移。

// 更新 banner 区域：把执行结果填入结果插槽，并自动隐藏「等待执行」空状态
// （loading / alert / AI 回复卡 等传入的都是「有内容」的状态）
function _setSimulateBanner(html) {
    _showResultSlot(html);
}

// 重置 3 张卡片到 placeholder 静态提示
function _resetSimulateCards() {
    const placeholders = {
        route: '<div class="sim-card-static-hint"><i class="fa-regular fa-circle-dot"></i><span>对话开始后将显示智能体的路由决策</span></div>',
        tools: '<div class="sim-card-static-hint"><i class="fa-regular fa-circle-dot"></i><span>对话中智能体调用过的工具将列在此处</span></div>',
        evidence: '<div class="sim-card-static-hint"><i class="fa-regular fa-circle-dot"></i><span>回复引用过的证据将在对话后汇总显示</span></div>',
    };
    for (const [type, html] of Object.entries(placeholders)) {
        const el = document.getElementById(`sim-card-body-${type}`);
        if (el) el.innerHTML = html;
    }
}

// 更新指定卡片 body
function _setCardBody(type, html) {
    const el = document.getElementById(`sim-card-body-${type}`);
    if (el) el.innerHTML = html;
}

// 路由详情卡片 body
function _buildRouteCardBody(res) {
    return `
        <div class="sim-route-grid">
            <div class="sim-route-item">
                <div class="sim-route-icon-wrap"><i class="fa-solid fa-compass"></i></div>
                <div class="sim-route-info">
                    <span class="sim-route-label">路由模式</span>
                    <span class="sim-route-value">${escapeHtml(_routingModeLabel(res.routing_mode))}</span>
                </div>
            </div>
            <div class="sim-route-item">
                <div class="sim-route-icon-wrap"><i class="fa-solid fa-target"></i></div>
                <div class="sim-route-info">
                    <span class="sim-route-label">命中技能</span>
                    <span class="sim-route-value ${res.skill_name ? '' : 'muted'}">${res.skill_name ? escapeHtml(res.skill_name) : '未命中'}</span>
                </div>
            </div>
            <div class="sim-route-item">
                <div class="sim-route-icon-wrap"><i class="fa-solid fa-code-branch"></i></div>
                <div class="sim-route-info">
                    <span class="sim-route-label">技能来源</span>
                    <span class="sim-route-value ${res.skill_source ? '' : 'muted'}">${res.skill_source ? escapeHtml(res.skill_source) : '—'}</span>
                </div>
            </div>
            <div class="sim-route-item">
                <div class="sim-route-icon-wrap"><i class="fa-solid fa-gauge-high"></i></div>
                <div class="sim-route-info">
                    <span class="sim-route-label">置信度</span>
                    <span class="sim-route-value">${escapeHtml(_formatConfidence(res.confidence))}</span>
                    <div class="sim-confidence-wrap">
                        <div class="sim-confidence-bar">
                            <div class="sim-confidence-fill ${_confidenceClass(res.confidence)}" style="width:${typeof res.confidence === 'number' ? Math.round(res.confidence * 100) : 0}%"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

// 调用工具卡片 body
function _buildToolsCardBody(res) {
    if (res.routed_tools && res.routed_tools.length > 0) {
        return `
            <div class="sim-tools-flow">
                ${res.routed_tools.map((t, i) => `
                    <div class="sim-tool-node">
                        <div class="sim-tool-icon"><i class="fa-solid fa-wrench"></i></div>
                        <div class="sim-tool-name">${escapeHtml(t)}</div>
                    </div>
                    ${i < res.routed_tools.length - 1 ? '<div class="sim-tool-arrow"><i class="fa-solid fa-arrow-right"></i></div>' : ''}
                `).join('')}
            </div>
        `;
    }
    return '<div class="sim-card-static-hint"><i class="fa-regular fa-circle-dot"></i><span>本次对话未调用任何工具</span></div>';
}

// 证据来源卡片 body
function _buildEvidenceCardBody(res) {
    if (res.evidence_source) {
        return `<div class="sim-evidence-tag">${escapeHtml(res.evidence_source)}</div>`;
    }
    return '<div class="sim-card-static-hint"><i class="fa-regular fa-circle-dot"></i><span>本次回复未引用证据来源</span></div>';
}

// AI 回复卡片（放在 banner 区域，紧跟在 alert 后面）
function _buildReplyCardHtml(res) {
    const replyText = (res.text || '').trim();
    return `
        <div class="sim-card sim-card-reply">
            <div class="sim-card-header">
                <div class="sim-card-icon sim-icon-robot"><i class="fa-solid fa-robot"></i></div>
                <div class="sim-card-title">AI 回复</div>
            </div>
            <div class="sim-card-body">
                <div class="sim-chat-bubble ${replyText ? '' : 'sim-chat-empty'} md-content">
                    ${replyText ? renderMarkdown(replyText) : '<div class="sim-chat-empty-text">AI 未返回文本内容</div>'}
                </div>
            </div>
        </div>
    `;
}

// 首次加载页面时：3 张固定卡已在 HTML 模板里，只需确保 banner 区是 idle 空状态
// （保留函数签名以兼容外部调用）
function _initSimulateResultShell() {
    _showIdleState();
    // 卡片 body 在 HTML 模板里已经是 placeholder，无需再 reset
}

function clearSimulate() {
    document.getElementById('simulate-content').value = '';
    // 卡片结构在 HTML 模板里固定，只更新 banner 和卡片数据
    _showIdleState();
    _resetSimulateCards();
    document.getElementById('simulate-status').innerHTML = '';
    const timing = document.getElementById('sim-result-timing');
    if (timing) timing.style.display = 'none';
    _setResultStatus('idle');
}

async function sendSimulatedMessage() {
    const content = document.getElementById('simulate-content').value.trim();
    const senderName = document.getElementById('simulate-sender').value.trim();
    const enableStream = document.getElementById('simulate-stream').checked;

    if (!content) {
        showToast('请输入消息内容', 'warning');
        return;
    }

    const resultArea = document.getElementById('simulate-result');
    const statusArea = document.getElementById('simulate-status');
    const timingEl = document.getElementById('sim-result-timing');

    _setResultStatus('running');
    if (timingEl) {
        timingEl.style.display = '';
        timingEl.textContent = '执行中...';
    }
    // 只更新 banner 区，3 张固定卡保持原状
    _setSimulateBanner(`
        <div class="sim-result-loading">
            <div class="sim-result-loading-spinner"></div>
            <div class="sim-result-loading-text">AI 正在思考中…</div>
        </div>
    `);
    statusArea.innerHTML = '';

    const startTime = Date.now();

    try {
        const res = await api.post('/api/simulate/message', {
            content,
            sender_name: senderName || '测试用户',
            enable_stream: enableStream,
        }, { timeoutMs: 120000 });

        const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
        if (timingEl) timingEl.textContent = elapsed + 's';

        if (!res) {
            _setResultStatus('error');
            _showResultSlot(_idleEmptyHtml('请求失败', '无响应', 'fa-triangle-exclamation', 'var(--brand-danger)'));
            _resetSimulateCards();
            return;
        }

        if (res.success) {
            _setResultStatus('success');
            renderSimulateResult(res);

            await api.post('/api/simulate/history', {
                content: content,
                sender_name: senderName || '',
                result_text: res.text || '',
                routing_mode: res.routing_mode || '',
                skill_name: res.skill_name || '',
            });

            const historyRes = await api.get('/api/simulate/history');
            if (historyRes && historyRes.history) {
                renderSimHistory(historyRes.history);
            }
        } else {
            _setResultStatus('error');
            const detail = typeof res.detail === 'object'
                ? JSON.stringify(res.detail)
                : (res.detail || '处理失败');
            _showResultSlot(_idleEmptyHtml('执行失败', detail, 'fa-circle-xmark', 'var(--brand-danger)'));
            _resetSimulateCards();
        }
    } catch (e) {
        const elapsed = ((Date.now() - startTime) / 1000).toFixed(2);
        if (timingEl) timingEl.textContent = elapsed + 's';
        _setResultStatus('error');

        const msg = e.message || String(e);
        const isTimeout = msg.includes('timeout') || msg.includes('超时') || msg.includes('abort');
        _showResultSlot(_idleEmptyHtml(
                isTimeout ? '请求超时' : '请求异常',
                isTimeout ? 'LLM 模型可能正在重试中，请稍后再试' : msg,
                isTimeout ? 'fa-clock' : 'fa-bolt',
                'var(--brand-danger)'
            ));
        _resetSimulateCards();
    }
}

function _confidenceClass(val) {
    if (val === null || val === undefined) return 'low';
    if (val >= 0.7) return 'high';
    if (val >= 0.4) return 'mid';
    return 'low';
}

function _formatConfidence(val) {
    if (val === null || val === undefined) return '—';
    if (typeof val === 'number') return (val * 100).toFixed(0) + '%';
    return String(val);
}

function _routingModeLabel(mode) {
    const labels = {
        'smart': '智能路由',
        'rule': '规则匹配',
        'skill': '技能路由',
        'streaming': '流式输出',
        'fallback': '兜底回复',
    };
    return labels[mode] || mode || '—';
}

function renderSimulateResult(res) {
    // 3 张固定卡已在 HTML 模板里就地渲染，仅更新 banner 区域 + 卡片 body 数据
    const statusArea = document.getElementById('simulate-status');

    // 1) banner 区：可选 alert + AI 回复卡
    let bannerHtml = '';
    if (res.already_sent) {
        bannerHtml += `
            <div class="sim-alert sim-alert-info">
                <div class="sim-alert-icon"><i class="fa-solid fa-circle-info"></i></div>
                <div class="sim-alert-text">此消息已被处理过（去重命中）</div>
            </div>
        `;
    }
    bannerHtml += _buildReplyCardHtml(res);
    _setSimulateBanner(bannerHtml);

    // 2) 3 张卡片 body 数据
    _setCardBody('route', _buildRouteCardBody(res));
    _setCardBody('tools', _buildToolsCardBody(res));
    _setCardBody('evidence', _buildEvidenceCardBody(res));

    // 工具卡 badge（按需显示）
    const toolsCard = document.querySelector('.sim-card-tools .sim-card-header');
    if (toolsCard) {
        const existingBadge = toolsCard.querySelector('.sim-card-badge');
        if (existingBadge) existingBadge.remove();
        if (res.routed_tools && res.routed_tools.length > 0) {
            const badge = document.createElement('div');
            badge.className = 'sim-card-badge';
            badge.textContent = res.routed_tools.length;
            toolsCard.appendChild(badge);
        }
    }

    statusArea.innerHTML = '';
}
