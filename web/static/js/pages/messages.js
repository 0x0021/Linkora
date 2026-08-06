// ============ pages/messages.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ Messages & Conversations ============
// 批量模式
let _msgBatchMode = false;
let _msgSelected = {};

// ===== 消息内容格式化渲染 =====
// 将钉钉/飞书固定格式（card / 图片占位 / markdown 链接等）渲染为视觉效果

/**
 * 渲染单条消息内容，将钉钉/飞书原始格式转为 HTML。
 * 处理：<card>卡片、<[图片...]>占位、[text](url)链接、换行。
 * 返回 HTML 字符串（安全转义 + 受控标签）。
 *
 * @param {string} raw - 消息内容字符串
 * @param {Object<string,string>} [imagePathMap] - 可选的「飞书卡片 image_key → 本地相对路径」映射，
 *        来自后端 image_path 字段（JSON 格式）。命中映射时把 🖼️ Image(img_key:xxx) 渲染为
 *        真图 <img>，未命中才降级为带说明的占位符。
 */
function renderMsgContent(raw, imagePathMap) {
    const s = (raw || '').trim();
    if (!s) return '';

    // 0) 兼容历史 OCR 格式：「【图片内容】\n{ocr_text}」（旧版后端未用 <card> 包裹）。
    //    直接按卡片渲染，历史消息刷新即结构化，无需重新 OCR。
    const legacyOcr = s.match(/^(.*?)\n?【图片内容】\n([\s\S]*)$/);
    if (legacyOcr) {
        const prefix = legacyOcr[1].trim();
        const ocrBody = legacyOcr[2].trim();
        let html = prefix ? _renderText(prefix) : '';
        html += `<div class="msg-card"><div class="msg-card-title">📋 图片内容</div><div class="msg-card-body">${_renderCardBody(ocrBody)}</div></div>`;
        return _cardSelfCheck(s) + html;
    }

    // 1) <card title="...">...</card>  → 卡片组件（支持前缀普通文本 + 多 card 块混合）
    const cardRe = /<card\s+title="([^"]*)"(?:\s+[^>]*)?>([\s\S]*?)<\/card>/g;
    const cardMatches = [];
    let cm;
    while ((cm = cardRe.exec(s)) !== null) cardMatches.push(cm);
    if (cardMatches.length > 0) {
        let html = '';
        let lastIndex = 0;
        cardRe.lastIndex = 0;
        while ((cm = cardRe.exec(s)) !== null) {
            const before = s.slice(lastIndex, cm.index).trim();
            if (before) html += _renderText(before);
            const title = escapeHtml(cm[1]);
            const body = _renderCardBody(cm[2].trim(), imagePathMap);
            html += `<div class="msg-card"><div class="msg-card-title">📋 ${title}</div><div class="msg-card-body">${body}</div></div>`;
            lastIndex = cardRe.lastIndex;
        }
        const after = s.slice(lastIndex).trim();
        if (after) html += _renderText(after);
        return _cardSelfCheck(s) + html;
    }

    // 2) <[图片识别中...]> / <[图片...]>  → 图片占位
    const imgMatch = s.match(/^<\[([^\]]*)\]>\s*$/);
    if (imgMatch) {
        const label = imgMatch[1] || '图片';
        return `<div class="msg-img-placeholder"><i class="fa-solid fa-image"></i><span>${escapeHtml(label)}</span></div>`;
    }

    // 3) 卡片代码块自检：扫描未渲染的原始文本中是否有卡片块，
    //    遇到格式异常/路径不一致等则在渲染结果上方插入 ⚠️ 警告。
    let cardCheckHtml = '';
    if (typeof CardValidator !== 'undefined') {
        try {
            const scan = CardValidator.scanText(s);
            if (scan.totalWarnings > 0) {
                const allWarns = [];
                scan.blocks.forEach(function (b) {
                    if (b.warnings.length > 0) {
                        allWarns.push('[' + b.cardType + '] ' + b.warnings.join('; '));
                    }
                });
                cardCheckHtml = CardValidator.renderWarnings(allWarns);
            }
        } catch (e) {
            console.debug('CardValidator 自检异常:', e);
        }
    }

    // 4) 普通文本：markdown 链接 + 换行 + 转义
    return cardCheckHtml + _renderText(s);
}

/** 卡片代码块自检（抽出避免重复）：基于原始 s 扫描未渲染 card 块的格式异常 */
function _cardSelfCheck(s) {
    if (typeof CardValidator === 'undefined') return '';
    try {
        const scan = CardValidator.scanText(s);
        if (scan.totalWarnings === 0) return '';
        const allWarns = [];
        scan.blocks.forEach(function (b) {
            if (b.warnings.length > 0) {
                allWarns.push('[' + b.cardType + '] ' + b.warnings.join('; '));
            }
        });
        return CardValidator.renderWarnings(allWarns);
    } catch (e) {
        console.debug('CardValidator 自检异常:', e);
        return '';
    }
}

/** 渲染 card 内部 body：先全局提取所有链接，再按行处理结构 */
function _renderCardBody(body, imagePathMap) {
    if (!body) return '';
    imagePathMap = imagePathMap || null;

    // ---- Pass 0: 飞书消息卡片内嵌图片占位 + clickable 转链接 ----
    // 飞书第三方 bot（飞书智能助手、飞行社等）发的图受飞书 IM 资源隔离限制：
    // image_key 归属 bot 自己的 app，linkora 的 app 凭证无法跨 app 下载
    // （错误码 99992361 open_id cross app，永久不可达）。统一渲染为带说明的
    // 占位符，让用户一眼知道"图存在但 linkora 拉不到"，不是 linkora 的 bug。
    // clickable 是带 url 的容器，转成 <a> 按钮保留跳转语义。
    const imgPlaceholder =
        '<div class="msg-img-placeholder msg-img-unavailable">' +
        '<i class="fa-solid fa-image"></i>' +
        '<div class="msg-img-meta">' +
        '<span class="msg-img-label">图片</span>' +
        '<small class="msg-img-hint">该图为第三方 bot 发送，受飞书跨 app 资源隔离限制，linkora 无法下载</small>' +
        '</div></div>';
    // 命中 imagePathMap 时渲染真图（与既有 chat-image 缩略图视觉一致）
    const renderImg = (key) => {
        const rel = imagePathMap && imagePathMap[key];
        if (rel) {
            const fullUrl = '/api/image/' + rel;
            return `<img src="${escapeHtml(imgTokUrl(fullUrl))}" class="msg-card-img" alt="卡片图片" loading="lazy" decoding="async" onclick="openImageLightbox(this.src)">`;
        }
        return imgPlaceholder;
    };
    let html = body
        // clickable url="..." → <a>，保留跳转语义（属性可有可无、顺序不固定）
        .replace(/<clickable[^>]*\burl\s*=\s*"([^"]+)"[^>]*>([\s\S]*?)<\/clickable>/g,
            (_m, url, inner) => {
                const cleanUrl = escapeHtml(url.replace(/\s+/g, ''));
                // 提取 clickable 内部纯文本（去除 img_key 等标签），作为按钮文字
                const label = inner.replace(/\S*\((?:img_key|file_key|IMG_KEY)[^)]*\)/g, '')
                                   .replace(/\s+/g, ' ').trim() || '打开链接';
                return `<a class="msg-link-btn" href="${cleanUrl}" target="_blank" rel="noopener"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${escapeHtml(label)}</a>`;
            })
        // 兜底：清理孤立的 clickable 开始/结束标签（无 url 或解析失败）
        .replace(/<\/?clickable[^>]*>/g, '')
        // 任意 emoji/img 标记后接 (img_key|file_key|IMG_KEY:xxx) → 命中映射渲染 <img>，否则占位
        .replace(/\S*\s*\((img_key|file_key|IMG_KEY):([^\s)]+)\)/g, (_m, _kind, key) => renderImg(key));

    // ---- Pass 1: 全局扫描，把所有 [text](url) 替换为 <a> 按钮 ----
    // 飞书/钉钉长 URL 常被折行插入空白，用回调清洗
    html = html.replace(/\[([^\]]*)\]\(([^)]*?)\)/g, (_match, label, url) => {
        const cleanLabel = label.trim();
        let cleanUrl = url.replace(/\s+/g, '');   // 去除折行空格
        if (!cleanUrl || !cleanLabel) return _match;  // 格式不对就原样保留
        return `<a class="msg-link-btn" href="${escapeHtml(cleanUrl)}" target="_blank" rel="noopener"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${escapeHtml(cleanLabel)}</a>`;
    });

    // ---- Pass 2: 按行处理结构（KV 对 / 普通文本行）----
    // 此时链接已变成 <a> 标签，安全地 split 而不会破坏它们
    const lines = html.split('\n').map(l => l.trim()).filter(Boolean);
    html = lines.map(line => {
        // 已渲染的图片占位符直接透传，避免再套一层 text-line
        if (line.includes('msg-img-placeholder')) return line;
        // 水平线 --- → <hr>
        if (/^---+$/.test(line)) return '<hr class="msg-hr">';
        // key:value 对（跳过已含 HTML 标签的行）
        if (!line.includes('<') && /^([^:：]{2,})[:：](.+)$/.test(line)) {
            const m = line.match(/^([^:：]{2,})[:：](.+)$/);
            return `<div class="msg-kv"><span class="msg-kv-key">${escapeHtml(m[1])}</span><span class="msg-kv-val">${_renderInline(m[2])}</span></div>`;
        }
        return `<div class="msg-text-line">${_renderInline(line)}</div>`;
    }).join('');

    return html;
}

/** 行内格式化（bold / 无URL方括号标签 / emoji 透传）——输入已 escapeHtml */
function _renderInline(text) {
    let s = text;
    // **加粗** → <strong>
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // [标签]（无 URL 的方括号）→ 标签样式
    s = s.replace(/\[([^\]]+)\]/g, '<span class="msg-tag">$1</span>');
    return s;
}

/** 普通文本渲染：markdown 链接 + bold + 列表 + 分隔线 + HTML 转义 + 换行 */
function _renderText(text) {
    let s = escapeHtml(text);
    // [text](url) → <a>；URL 可能含换行空格（源数据折行），清洗之
    s = s.replace(/\[([^\]]*)\]\(([^)]*?)\)/g, (_match, label, url) => {
        const cleanUrl = url.replace(/\s+/g, '');
        if (!cleanUrl) return _match;
        // 协议白名单：仅允许 http/https/mailto，阻断 javascript:/data: 等 XSS 注入
        if (!/^(https?:|mailto:)/i.test(cleanUrl)) return _match;
        return `<a class="msg-inline-link" href="${cleanUrl}" target="_blank" rel="noopener">${label}</a>`;
    });
    // **加粗**
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // [标签]（无 URL）
    s = s.replace(/\[([^\]]+)\]/g, '<span class="msg-tag">$1</span>');
    // > 引用/列表前缀 → 缩进样式行
    s = s.replace(/^(&gt;|＞)(.*)$/gm, '<span class="msg-list-prefix">$1</span>$2');
    // \n → <br>
    s = s.replace(/\n/g, '<br>');
    // --- 水平线（独立成行的）
    s = s.replace(/(^<br>)?---+(<br>)?$/g, '<hr class="msg-hr">');
    return s;
}

// ===== 消息记录页辅助函数 =====
const _AVATAR_COLORS = [
    '#2563eb', '#7c3aed', '#0891b2', '#059669', '#d97706',
    '#dc2626', '#db2777', '#4f46e5', '#0d9488', '#ca8a04'
];
function avatarColor(seed) {
    let h = 0;
    const s = String(seed || '');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return _AVATAR_COLORS[h % _AVATAR_COLORS.length];
}
function truncateText(str, n) {
    str = str || '';
    return str.length > n ? str.slice(0, n) + '…' : str;
}
function formatDateSep(dateStr) {
    if (!dateStr) return '';
    const today = new Date().toISOString().slice(0, 10);
    const y = new Date(); y.setDate(y.getDate() - 1);
    const yesterday = y.toISOString().slice(0, 10);
    if (dateStr === today) return '今天';
    if (dateStr === yesterday) return '昨天';
    const d = new Date(dateStr);
    return `${d.getMonth() + 1}月${d.getDate()}日`;
}

let _activeChatId = null;

async function loadMessages() {
    const search = document.getElementById('msg-search')?.value || '';
    const convFilter = document.getElementById('msg-conv-filter');
    const currentVal = convFilter?.value || '';

    let conversations = [];
    try {
        const convData = await api.getConversations(100);
        conversations = (convData && convData.conversations) ? convData.conversations : [];
    } catch (e) {
        console.error('获取会话列表失败:', e);
        document.getElementById('msg-conversation-list').innerHTML =
            '<div class="empty-state"><div class="empty-icon">&#x26A0;</div><p>加载会话失败，请检查网络连接</p></div>';
        document.getElementById('msg-thread').innerHTML =
            '<div class="empty-state"><div class="empty-icon">&#x26A0;</div><p>无法加载消息列表</p></div>';
        return;
    }

    let convOptsHtml = '<option value="">全部会话</option>';
    conversations.forEach(c => {
        const label = escapeHtml(c.chat_name || c.chat_id);
        convOptsHtml += `<option value="${escapeHtml(c.chat_id)}" ${c.chat_id === currentVal ? 'selected' : ''}>${label}</option>`;
    });
    convFilter.innerHTML = convOptsHtml;

    const listContainer = document.getElementById('msg-conversation-list');
    const thread = document.getElementById('msg-thread');
    const threadTitle = document.getElementById('msg-thread-title');
    const threadMeta = document.getElementById('msg-thread-meta');
    if (!listContainer || !thread) return;

    // 键盘可达性：会话项为按钮语义，Enter/Space 触发点击（事件委托，幂等）
    if (!listContainer._kbdBound) {
        listContainer.addEventListener('keydown', (e) => {
            const t = e.target;
            if ((e.key === 'Enter' || e.key === ' ') && t && t.classList && t.classList.contains('conversation-item')) {
                e.preventDefault();
                t.click();
            }
        });
        listContainer._kbdBound = true;
    }

    const filteredConversations = conversations.filter(c => {
        const name = (c.chat_name || c.chat_id || '').toLowerCase();
        return name.includes(search.toLowerCase());
    });

    if (filteredConversations.length === 0) {
        listContainer.innerHTML = `<div class="empty-state"><div class="empty-icon">💬</div><p>暂无匹配会话</p></div>`;
        thread.innerHTML = `<div class="empty-state"><div class="empty-icon">💬</div><p>选择一个会话查看消息</p></div>`;
        setThreadHeader(null, 0);
        return;
    }

    let activeChatId = currentVal;
    if (!activeChatId || !conversations.some(c => c.chat_id === activeChatId)) {
        // 优先选有未读消息的最新会话，其次选最新会话，兜底选第一个
        const sorted = [...filteredConversations].sort((a, b) => {
            const aTime = a.last_message_at ? new Date(a.last_message_at).getTime() : 0;
            const bTime = b.last_message_at ? new Date(b.last_message_at).getTime() : 0;
            return bTime - aTime;
        });
        const unread = sorted.find(c => c.message_count > 0);
        activeChatId = (unread || sorted[0] || filteredConversations[0]).chat_id;
    }
    _activeChatId = activeChatId;
    if (convFilter) convFilter.value = activeChatId;

    const grouped = {
        single: filteredConversations.filter(c => c.chat_type === 'single'),
        group: filteredConversations.filter(c => c.chat_type === 'group'),
        other: filteredConversations.filter(c => c.chat_type !== 'single' && c.chat_type !== 'group'),
    };
    const groupOrder = [
        { key: 'single', title: '单聊' },
        { key: 'group', title: '群聊' },
        { key: 'other', title: '其他' },
    ];

    listContainer.innerHTML = groupOrder.map(group => {
        const items = grouped[group.key] || [];
        if (items.length === 0) return '';
        const groupIcon = group.key === 'single' ? '<i class="fa-solid fa-user" style="margin-right:4px;font-size:0.65rem;"></i>'
            : group.key === 'group' ? '<i class="fa-solid fa-user-group" style="margin-right:4px;font-size:0.65rem;"></i>'
            : '<i class="fa-solid fa-bell" style="margin-right:4px;font-size:0.65rem;"></i>';
        return `<div class="conversation-group">
            <div class="conversation-group-title">${groupIcon}${group.title} (${items.length})</div>
            ${items.map(c => {
                const initial = (c.chat_name || c.chat_id || '?').trim().charAt(0);
                const color = avatarColor(c.chat_id);
                const preview = truncateText(c.last_message_preview || '（无消息内容）', 22);
                const isOther = c.chat_type !== 'single' && c.chat_type !== 'group';
                const checkedAttr = _msgBatchMode && _msgSelected[c.chat_id] ? ' checked' : '';
                const batchCb = _msgBatchMode ? '<input type="checkbox" class="batch-checkbox" style="flex-shrink:0;margin-right:8px;" data-msg-chat-id="' + escapeHtml(c.chat_id) + '"' + checkedAttr + ' onclick="event.stopPropagation();_msgOnCheck(this)">' : '';
                const clickHandler = _msgBatchMode ? '_msgOnCheckCb(this)' : 'selectMessageConversation(this.dataset.chatId)';
                const avatarHtml = isOther
                    ? '<div class="conv-avatar conv-avatar-other"><i class="fa-solid fa-bell"></i></div>'
                    : `<div class="conv-avatar" style="background:${color}">${escapeHtml(initial)}</div>`;
                return `<div class="conversation-item ${c.chat_id === activeChatId ? 'active' : ''}" role="button" tabindex="0" data-chat-id="${escapeHtml(c.chat_id)}" onclick="${clickHandler}">
                    ${batchCb}${avatarHtml}
                    <div class="conv-main">
                        <div class="conv-row1">
                            <span class="conv-name">${escapeHtml(c.chat_name || c.chat_id)}</span>
                            <span class="conv-time">${formatTime(c.last_message_at)}</span>
                        </div>
                        <div class="conv-row2">
                            <span class="conv-preview">${escapeHtml(preview)}</span>
                            <span class="conv-badge">${c.message_count || 0}</span>
                        </div>
                    </div>
                </div>`;
            }).join('')}
        </div>`;
    }).join('');

    await renderThread(activeChatId, conversations);
}

async function renderThread(chatId, conversations) {
    const thread = document.getElementById('msg-thread');
    const selectedChat = conversations.find(c => c.chat_id === chatId) || null;
    // 先显示骨架
    thread.innerHTML = `<div class="thread-skeleton">
        ${Array.from({ length: 6 }).map(() => `
            <div class="sk-msg-row">
                <div class="skeleton skeleton-avatar"></div>
                <div class="skeleton-bubble">
                    <div class="skeleton skeleton-line" style="width:30%"></div>
                    <div class="skeleton skeleton-line" style="width:70%"></div>
                </div>
            </div>`).join('')}
    </div>`;

    try {
    const data = await api.getMessages(chatId);
    const messages = (data && data.messages) ? data.messages : [];
    setThreadHeader(selectedChat, messages.length);

    if (!messages.length) {
        thread.innerHTML = `<div class="empty-state"><div class="empty-icon">💬</div><p>暂无消息</p></div>`;
        return;
    }

    const currentUserName = escapeHtml(data.current_user_name || '我');
    const peerName = escapeHtml(selectedChat ? (selectedChat.chat_name || '-') : '-');
    const sorted = messages.slice().reverse();

    // ===== 合并预处理：将相邻的「图片消息 + 文字指令」合并为一条气泡渲染 =====
    // 场景：用户发图片（content=[图片识别中...]）紧接着发"识别图片内容"等短指令，
    // 后端存了两行但前端应该展示为一个带图+文的气泡。
    const MERGE_WINDOW_SEC = 90;
    const groups = [];
    let i = 0;
    while (i < sorted.length) {
        const cur = sorted[i];
        const isImageLike = (cur.msg_type === 'image') ||
            (cur.content && /\[图片识别中/.test(cur.content));
        if (isImageLike && i + 1 < sorted.length) {
            const nxt = sorted[i + 1];
            const gapSec = (new Date(nxt.timestamp || 0) - new Date(cur.timestamp || 0)) / 1000;
            // 同发送者 + 同角色：避免跨角色误合并（例如对方图片 + 机器人文字回复被并到同一气泡）。
            const sameSender = (cur.sender_id || cur.sender_name || '') === (nxt.sender_id || nxt.sender_name || '');
            const sameRole = (cur.role || 'user') === (nxt.role || 'user');
            const nextIsText = (nxt.msg_type !== 'image') && !/\[图片识别中/.test(nxt.content || '');
            if (sameSender && sameRole && nextIsText && gapSec > 0 && gapSec <= MERGE_WINDOW_SEC) {
                groups.push({ _merge: true, img: cur, text: nxt });
                i += 2;
                continue;
            }
        }
        groups.push({ _merge: false, msg: cur });
        i++;
    }

    let lastDate = '';
    let html = '';
    groups.forEach(g => {
        // 确定主消息元数据
        const m = g._merge ? g.img : g.msg;
        const dateStr = (m.timestamp || '').slice(0, 10);
        if (dateStr !== lastDate) {
            html += `<div class="chat-date-sep"><span>${formatDateSep(dateStr)}</span></div>`;
            lastDate = dateStr;
        }
        const role = m.role || 'assistant';
        // ★ P-1 修复：isMe 不再只看 role（role='assistant' 只是 AI 代发）。
        // 顺序：① 后端 direction 字段（权威） → ② sender_name/id 对比 current_user 字段（兜底） → ③ is_bot=1（AI 代发归"我"）
        const isMeByDirection = m.direction === 'out';
        const isMeBySender = (
            (data.current_user_name && (m.sender_name || '').trim() === data.current_user_name)
            || (data.current_user_id && (m.sender_id || '').trim() === data.current_user_id)
        );
        const isMeByBot = !!(m.is_bot);
        const isMe = isMeByDirection || isMeBySender || isMeByBot;
        const isBot = !!(m.is_bot);
        const isArchived = !!(m.is_archived);
        const roleClass = isMe ? 'user' : 'assistant';
        const archivedClass = isArchived ? 'archived' : '';
        const senderName = escapeHtml(m.sender_name || '未知');
        const displaySender = isMe ? currentUserName : senderName;
        const displayReceiver = isMe ? peerName : currentUserName;
        const initial = (displaySender || '?').trim().charAt(0);
        const color = avatarColor(isMe ? 'me-' + (data.current_user_name || 'me') : (m.sender_name || chatId));

        // aitag / 归档
        let aitagHtml = '';
        if (isBot && isMe) aitagHtml = '<span class="aitag aitag-bot"><i class="fa-solid fa-robot"></i> AI代发</span>';
        else if (isMe) aitagHtml = '<span class="aitag aitag-me"><i class="fa-solid fa-user"></i> 我</span>';
        else if (m.sender_name) aitagHtml = '<span class="aitag aitag-other"><i class="fa-solid fa-user-group"></i> 对方</span>';
        let archiveBadge = isArchived ? '<span class="archive-badge"><i class="fa-solid fa-box-archive"></i> 已归档</span>' : '';
        let skipBadge = (m.skip_reason) ? `<span class="skip-badge">[已跳过：${m.skip_reason === 'reply_single_only_when_unread' ? '单聊已读不回复' : escapeHtml(m.skip_reason)}]</span>` : '';

        // ---- 构造 bubble 内容 ----
        let mediaBadge = '';
        let imageHtml = '';
        let bubbleContent = '';

        if (g._merge) {
            // ★ 合并模式：图片 + 文字指令 → 一个气泡
            const imgUrl = g.img.image_url || '';
            const txtContent = renderMsgContent(g.text.content, g.text.image_path_map);
            // 图片部分
            if (imgUrl) {
                mediaBadge = '<span class="media-badge">📷 图片</span> ';
                imageHtml = `<div class="chat-image-wrap"><img src="${escapeHtml(imgTokUrl(imgUrl))}" class="chat-image" alt="对话图片" loading="lazy" decoding="async" onclick="openImageLightbox(this.src)"/></div>`;
            } else {
                // 无 image_url 时用优雅占位提示（后端 OCR 可能未回写 path）
                mediaBadge = '<span class="media-badge">📷 图片</span> ';
                imageHtml = '<div class="chat-image-placeholder"><i class="fa-solid fa-image"></i><span>图片（本地存储）</span></div>';
            }
            // 文字：如果有实际指令文本则追加；如果只是"识别图片内容"之类就省略或弱化
            const caption = (g.text.content || '').replace(/\s/g, '') === '识别图片内容' ? '' : txtContent;
            const ocrHint = (g.img.content && /\[图片识别中/.test(g.img.content)) ? '' : renderMsgContent(g.img.content, g.img.image_path_map);
            bubbleContent = `${mediaBadge}${ocrHint}${caption ? '<div class="merged-caption">' + caption + '</div>' : ''}`;
        } else {
            // ★ 普通单条消息
            const content = renderMsgContent(m.content, m.image_path_map);
            const isImageMsg = !!(m.image_url);
            if (isImageMsg) {
                mediaBadge = '<span class="media-badge">📷 图片</span> ';
                imageHtml = `<div class="chat-image-wrap"><img src="${escapeHtml(imgTokUrl(m.image_url))}" class="chat-image" alt="对话图片" loading="lazy" decoding="async" onclick="openImageLightbox(this.src)"/></div>`;
            } else if (m.msg_type === 'image') {
                // image 类型但没有 image_url 的兜底
                mediaBadge = '<span class="media-badge">📷 图片</span> ';
                imageHtml = '<div class="chat-image-placeholder"><i class="fa-solid fa-image"></i><span>图片（本地存储）</span></div>';
            }
            bubbleContent = `${mediaBadge}${content}`;
        }

        html += `<div class="chat-message ${roleClass} ${archivedClass}" data-text="${escapeHtml((m.content || '').toLowerCase())}">
            <div class="chat-avatar" style="background:${color}">${escapeHtml(initial)}</div>
            <div class="chat-bubble">
                <div class="chat-bubble-meta">
                    <span>${aitagHtml} ${displaySender} ${archiveBadge}${skipBadge}</span><span>→ ${displayReceiver}</span><span>${formatTime(m.timestamp)}</span>
                </div>
                <div class="chat-bubble-content">${bubbleContent}</div>
                ${imageHtml}
                ${feedbackHtml(m)}
            </div>
        </div>`;
    });
    thread.innerHTML = html;
    thread.scrollTop = thread.scrollHeight;
    // 绑定反馈按钮（评估闭环）
    thread.querySelectorAll('.chat-feedback .fb-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const wrap = btn.closest('.chat-feedback');
            const rating = parseInt(btn.dataset.rating, 10) || 0;
            submitFeedback(wrap.dataset.mid, wrap.dataset.cid, wrap.dataset.sid, rating, wrap);
        });
    });
    // 应用当前会话内搜索过滤
    filterThread();
    } catch (e) {
        console.error('renderThread failed:', e);
        thread.innerHTML = '<div class="empty-state"><div class="empty-icon">&#x26A0;</div><p>消息加载失败，请稍后重试</p></div>';
    }
}

// 评估闭环：AI 回复气泡的赞/踩控件
function feedbackHtml(m) {
    const isAi = (m.role === 'assistant') && (m.is_bot || m.sender_name);
    if (!isAi) return '';
    const mid = escapeHtml(m.msg_id || '');
    const cid = escapeHtml(m.chat_id || '');
    const sid = escapeHtml(m.sender_id || '');
    if (!mid) return '';
    return `<div class="chat-feedback" data-mid="${mid}" data-cid="${cid}" data-sid="${sid}">
        <button class="fb-btn" data-rating="1" title="回复有用">👍</button>
        <button class="fb-btn" data-rating="-1" title="回复无用">👎</button>
        <span class="fb-done" style="display:none">已反馈</span>
    </div>`;
}

async function submitFeedback(mid, cid, sid, rating, wrap) {
    try {
        await api.feedback({ message_id: mid, conversation_id: cid, sender_id: sid, rating });
        if (wrap) {
            wrap.querySelectorAll('.fb-btn').forEach(b => b.disabled = true);
            const done = wrap.querySelector('.fb-done');
            if (done) done.style.display = '';
        }
    } catch (e) {
        console.error('反馈提交失败', e);
    }
}

function setThreadHeader(chat, count) {
    const title = document.getElementById('msg-thread-title');
    const meta = document.getElementById('msg-thread-meta');
    if (title) title.textContent = chat ? (chat.chat_name || chat.chat_id) : '选择会话查看消息';
    if (meta) meta.textContent = `${count} 条消息`;
}

function filterThread() {
    const q = (document.getElementById('msg-thread-search')?.value || '').trim().toLowerCase();
    const thread = document.getElementById('msg-thread');
    if (!thread) return;
    thread.querySelectorAll('.chat-message').forEach(el => {
        const text = el.getAttribute('data-text') || '';
        el.style.display = (!q || text.includes(q)) ? '' : 'none';
    });
    // 隐藏没有匹配消息的日期分隔符
    let prevHidden = true;
    thread.querySelectorAll('.chat-date-sep').forEach(sep => {
        let next = sep.nextElementSibling;
        let anyVisible = false;
        while (next && !next.classList.contains('chat-date-sep')) {
            if (next.classList.contains('chat-message') && next.style.display !== 'none') anyVisible = true;
            next = next.nextElementSibling;
        }
        sep.style.display = anyVisible ? '' : 'none';
    });
}

// 同步轮询循环是否已在跑（防止「打开弹窗续接」与「刚启动」的循环叠加）
let _syncPolling = false;

// 把状态/日志渲染到进度区（进度条、百分比、实时日志面板）
function _renderSyncProgress(st, o) {
    const barEl = o.barEl, pctEl = o.pctEl, logEl = o.logEl, log = o.log;
    if (st && st.percent != null && barEl) {
        barEl.style.width = st.percent + '%';
        if (pctEl) pctEl.textContent = st.percent + '%';
    }
    if (logEl) {
        const lines = (log && Array.isArray(log.lines)) ? log.lines : null;
        logEl.textContent = lines ? lines.join('\n') : (st && st.progress ? st.progress : '等待同步开始…');
        logEl.scrollTop = logEl.scrollHeight;
    }
}

// 进行中显示「取消同步」、禁用「开始同步」；否则复位
function _showCancel(show) {
    const c = document.getElementById('sync-cancel-btn');
    const s = document.getElementById('sync-center-btn');
    if (c) c.style.display = show ? '' : 'none';
    if (s) s.disabled = !!show;
}

// 共享轮询循环：启动或「打开弹窗续接」都走这里。终态（done/error/cancelled）自动收尾。
async function _pollSyncLoop(opts, deadline) {
    if (_syncPolling) return;  // 已有循环在跑，避免叠加
    _syncPolling = true;
    const { btn, label = '开始同步', logEl, barEl, pctEl } = opts;
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    try {
        while (Date.now() < deadline) {
            await sleep(1500);
            const [st, log] = await Promise.all([
                api.syncHistoryStatus(),
                logEl ? api.syncHistoryLog(100) : Promise.resolve(null),
            ]);
            _renderSyncProgress(st, { barEl, pctEl, logEl, log });
            if (!st) continue;
            // 进行中：显示取消、按钮反映进度
            if (st.status === 'running' || st.status === 'starting') {
                _showCancel(true);
                if (btn && !barEl) btn.textContent = st.progress ? '同步中... ' + st.progress : '同步中...';
                continue;
            }
            // 终态
            _showCancel(false);
            if (st.status === 'done') {
                if (barEl) { barEl.style.width = '100%'; if (pctEl) pctEl.textContent = '100%'; }
                const r = st.result || {};
                const saved = r.saved || 0, total = r.total || 0, dup = r.skipped_dup || 0, fixed = r.fixed_direction || 0;
                const scope = st.scope || 'global';
                const rangeLabel = st.range || '';
                const who = scope === 'current' ? '当前会话' : '全局';
                const tail = rangeLabel ? `（${rangeLabel}）` : '';
                if (saved > 0) showToast(`已同步${who}${tail}，新增 ${saved} 条（拉取 ${total}，去重 ${dup}，修复 ${fixed}）`, 'success');
                else if (total > 0 || fixed > 0) showToast(`${who}${tail}：拉取 ${total} 条，去重 ${dup}，修复方向 ${fixed} 条`, 'warning');
                else showToast(`${who}${tail}没有可同步的消息`, 'warning');
                await loadMessages();
                return;
            }
            if (st.status === 'cancelled') {
                showToast('同步已取消', 'warning');
                return;
            }
            if (st.status === 'error') {
                showToast('历史同步失败：' + (st.error || '未知错误'), 'error');
                return;
            }
        }
        showToast('同步仍在后台进行（超时），请稍后刷新查看', 'warning');
    } catch (e) {
        showToast('历史同步异常：' + e, 'error');
    } finally {
        _syncPolling = false;
    }
}

async function runSyncJob(payload, btn, label, opts = {}) {
    const logEl = opts.logEl || null;
    const barEl = opts.barEl || null;
    const pctEl = opts.pctEl || null;
    if (btn) { btn.disabled = true; btn.textContent = '同步中...'; }
    try {
        // 立即返回 job_id（同步在独立子进程执行，不阻塞 UI）
        const start = await api.syncHistory(payload);
        if (!start || start.success !== true) {
            // 409 并发护栏 / 其他错误：detail 里含提示文案
            const msg = (start && start.detail)
                ? start.detail
                : (start && start.status === 'running')
                    ? '已有同步进行中，请等待完成或取消'
                    : '历史同步启动失败，请稍后重试';
            showToast(msg, 'warning');
            return;
        }
        // 显示进度区（同步中心）
        const prog = document.getElementById('sync-progress');
        if (prog) prog.style.display = 'block';
        if (barEl) { barEl.style.width = '0%'; if (pctEl) pctEl.textContent = '0%'; }
        // 启动即进入轮询（最多 15 分钟；超时仅停轮询，worker 仍在后台跑）
        await _pollSyncLoop({ btn, label, logEl, barEl, pctEl }, Date.now() + 15 * 60 * 1000);
    } catch (e) {
        showToast('历史同步异常：' + e, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = label || '同步'; }
    }
}

// 线程头部「同步本会话」按钮：只同步当前打开的会话（全部历史）
async function syncHistory() {
    if (!_activeChatId) {
        showToast('请先打开一个会话再同步', 'warning');
        return;
    }
    await runSyncJob(
        { scope: 'current', conversation_id: _activeChatId, range: 'all' },
        document.getElementById('sync-history-btn'),
        '同步本会话'
    );
}

// 左侧「同步中心」入口 + 模态框
function openSyncCenter() {
    const modal = document.getElementById('sync-center-modal');
    if (modal) modal.style.display = 'flex';
    _refreshSyncCenterOnOpen();
}

// 打开弹窗时立即重连可见性：读取最新状态/日志并渲染；若仍在进行中则自动续接轮询。
// 这样「关窗再开」不会失联——后台同步仍在跑，进度/日志重新可见。
async function _refreshSyncCenterOnOpen() {
    let st = null, log = null;
    try { st = await api.syncHistoryStatus(); } catch (e) { /* 忽略 */ }
    const prog = document.getElementById('sync-progress');
    const barEl = document.getElementById('sync-bar');
    const pctEl = document.getElementById('sync-pct');
    const logEl = document.getElementById('sync-log');
    if (!st || st.status === 'idle' || !st.status) {
        // 空闲：隐藏进度区、复位
        if (prog) prog.style.display = 'none';
        _showCancel(false);
        if (barEl) barEl.style.width = '0%';
        if (pctEl) pctEl.textContent = '0%';
        return;
    }
    // 有进行中或历史任务：展示进度区并渲染上次状态
    if (prog) prog.style.display = 'block';
    try { log = await api.syncHistoryLog(100); } catch (e) { /* 忽略 */ }
    _renderSyncProgress(st, { barEl, pctEl, logEl, log });
    if (st.status === 'running' || st.status === 'starting') {
        _showCancel(true);
        // 续接轮询（已有循环在跑则 _pollSyncLoop 直接返回，不会叠加）
        await _pollSyncLoop(
            { btn: document.getElementById('sync-center-btn'), label: '开始同步', logEl, barEl, pctEl },
            Date.now() + 15 * 60 * 1000,
        );
    } else {
        _showCancel(false);
    }
}

function closeSyncCenter() {
    // 仅隐藏弹窗，不中止轮询循环——后台同步继续，重开弹窗仍可见进度
    const modal = document.getElementById('sync-center-modal');
    if (modal) modal.style.display = 'none';
}

async function submitSyncCenter() {
    const rangeEl = document.getElementById('sync-range');
    const range = rangeEl ? rangeEl.value : '7';
    const types = [];
    if (document.getElementById('sync-type-single') && document.getElementById('sync-type-single').checked) types.push('single');
    if (document.getElementById('sync-type-group') && document.getElementById('sync-type-group').checked) types.push('group');
    // 同步进行中保持弹窗打开，实时显示进度条 + 日志
    const prog = document.getElementById('sync-progress');
    if (prog) prog.style.display = 'block';
    if (document.getElementById('sync-bar')) document.getElementById('sync-bar').style.width = '0%';
    if (document.getElementById('sync-pct')) document.getElementById('sync-pct').textContent = '0%';
    await runSyncJob(
        { scope: 'global', range, chat_types: types },
        document.getElementById('sync-center-btn'),
        '开始同步',
        {
            logEl: document.getElementById('sync-log'),
            barEl: document.getElementById('sync-bar'),
            pctEl: document.getElementById('sync-pct'),
        }
    );
}

// 取消进行中的同步（写取消标记 + kill worker），worker 在窗边界干净退出
async function cancelSyncJob() {
    try {
        const r = await api.syncHistoryCancel();
        if (r && r.cancelled) {
            showToast('已发送取消请求，同步将停止', 'info');
            _showCancel(false);
        } else {
            showToast('当前没有进行中的同步', 'warning');
        }
    } catch (e) {
        showToast('取消失败：' + e, 'error');
    }
}

function selectMessageConversation(chatId) {
    const filter = document.getElementById('msg-conv-filter');
    if (filter) {
        filter.value = chatId;
    }
    const ts = document.getElementById('msg-thread-search');
    if (ts) ts.value = '';
    loadMessages();
}

// 消息记录页定时刷新（每 30 秒）。仅在本页激活时轮询，离开页面即停止，避免跨页面泄漏。
let _msgRefreshTimer = null;
function startMessageRefresh() {
    if (_msgRefreshTimer) return;
    _msgRefreshTimer = setInterval(() => {
        if (document.visibilityState !== 'visible') return;
        loadMessages();
    }, 30000);
}
function stopMessageRefresh() {
    if (_msgRefreshTimer) {
        clearInterval(_msgRefreshTimer);
        _msgRefreshTimer = null;
    }
}
document.addEventListener('visibilitychange', () => {
    // 仅在消息页且页面可见时轮询，隐藏 tab 或离开消息页都停止
    if (document.visibilityState === 'visible' && currentPage === 'messages') {
        startMessageRefresh();
    } else {
        stopMessageRefresh();
    }
});
window.startMessageRefresh = startMessageRefresh;
window.stopMessageRefresh = stopMessageRefresh;


// ============ 消息分析面板 (P5-C) ============
let _msgAnalyticsChart = null;

function _msgRenderWordCloud(words) {
    const container = document.getElementById('msg-word-cloud-container');
    const skeleton = document.getElementById('msg-word-cloud-skeleton');
    if (!container) { console.error('[msg] word cloud container not found'); return; }
    if (!words || words.length === 0) {
        container.className = 'word-cloud';
        container.innerHTML = '<div class="word-cloud-empty"><p>暂无足够数据</p></div>';
        if (skeleton) skeleton.style.display = 'none';
        container.style.display = 'block';
        return;
    }
    if (skeleton) skeleton.style.display = 'none';
    container.style.display = 'block';
    container.className = 'word-cloud';
    const topWords = words.slice(0, 18);
    const haloPalette = [
        { color: '#6366f1', light: '#a5b4fc', base: '#4f46e5' },
        { color: '#8b5cf6', light: '#c4b5fd', base: '#6d28d9' },
        { color: '#3b82f6', light: '#93c5fd', base: '#1d4ed8' },
        { color: '#06b6d4', light: '#67e8f9', base: '#0e7490' },
        { color: '#f59e0b', light: '#fcd34d', base: '#b45309' },
        { color: '#ef4444', light: '#fca5a5', base: '#b91c1c' },
        { color: '#ec4899', light: '#f9a8d4', base: '#be185d' },
        { color: '#22c55e', light: '#86efac', base: '#15803d' },
    ];
    const getSize = (i) => i < 3 ? 'lg' : i < 8 ? 'md' : 'sm';
    let html = '<div class="kw-halo">';
    html += '<div class="halo-core"></div>';
    html += '<div class="halo-field">';
    topWords.forEach((w, i) => {
        const { color, light, base } = haloPalette[i % haloPalette.length];
        const size = getSize(i);
        const enterDelay = (i * 0.05).toFixed(2);
        const floatDelay = (Math.random() * 3).toFixed(2);
        html += `<span class="halo-tag size-${size}"
            style="--hl-color:${color};--hl-light:${light};--hl-base:${base};
            --enter-delay:${enterDelay}s;--float-delay:${floatDelay}s;"
            title="${escapeHtml(w.word)} · ${w.count} 次">
            ${escapeHtml(w.word)}<span class="tag-count">${w.count}</span></span>`;
    });
    html += '</div></div>';
    container.innerHTML = html;
    container.querySelectorAll('.halo-tag').forEach(tag => {
        tag.addEventListener('click', () => {
            const word = tag.childNodes[0].textContent.trim();
            switchPage('messages');
            const searchInput = document.getElementById('msg-search');
            if (searchInput) {
                searchInput.value = word;
                searchInput.dispatchEvent(new Event('input'));
            }
        });
    });
}

function _msgRenderMessageTrendChart(trend) {
    const ctx = document.getElementById('msg-chart-trend');
    if (!ctx) return;
    const skeleton = document.getElementById('msg-chart-trend-skeleton');
    // 空数据：显示空态，隐藏骨架屏
    if (!trend || trend.length === 0) {
        if (skeleton) skeleton.style.display = 'none';
        ChartCard.showEmpty(ctx.parentElement, '暂无趋势数据');
        ctx.style.display = 'none';
        return;
    }
    const ct = chartTheme();
    if (_msgAnalyticsChart) {
        _msgAnalyticsChart.destroy();
    }
    const labels = trend.map(d => d.day?.slice(5) || '');
    const data = trend.map(d => d.cnt || 0);
    const chart = new Chart(ctx, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                label: '消息数',
                data,
                borderColor: '#2563eb',
                backgroundColor: 'rgba(37, 99, 235, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.3,
                pointRadius: 3,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                y: { beginAtZero: true, ticks: { stepSize: 1, color: ct.tick }, grid: { color: ct.grid } },
                x: { grid: { display: false, color: ct.grid }, ticks: { color: ct.tick } }
            },
            animation: { duration: 800, easing: 'easeOutQuart' }
        }
    });
    _msgAnalyticsChart = chart;
    _msgStatsCharts.push(chart);
    if (skeleton) skeleton.style.display = 'none';
    ctx.style.display = 'block';
}

function _msgRenderMsgTypeChart(msgTypes) {
    const wrap = document.getElementById('msg-msgtype-chart-wrap');
    const skeleton = document.getElementById('msg-chart-types-skeleton');
    if (!wrap) return;
    // 空数据：显示空态，隐藏骨架屏
    if (!msgTypes || msgTypes.length === 0) {
        if (skeleton) skeleton.style.display = 'none';
        ChartCard.showEmpty(wrap, '暂无类型数据');
        return;
    }
    const items = [...msgTypes].sort((a, b) => (b.cnt || 0) - (a.cnt || 0));
    const total = items.reduce((s, d) => s + (d.cnt || 0), 0) || 1;
    const hint = document.getElementById('msgtype-total-hint');
    if (hint) hint.textContent = `共 ${total.toLocaleString('zh-CN')} 条`;
    const segs = items.map((d, i) => {
        const pal = _msgTypeColor(d.msg_type, i);
        const w = (d.cnt || 0) / total * 100;
        const grad = `linear-gradient(135deg, ${pal.light}, ${pal.base})`;
        return `<span class="mt-seg" data-idx="${i}" style="width:${w}%;background:${grad}"></span>`;
    }).join('');
    const rows = items.map((d, i) => {
        const pal = _msgTypeColor(d.msg_type, i);
        const w = (d.cnt || 0) / total * 100;
        const grad = `linear-gradient(135deg, ${pal.light}, ${pal.base})`;
        return `<div class="mt-row" data-idx="${i}" style="animation-delay:${(i * 0.06).toFixed(2)}s">
            <span class="mt-dot" style="background:${grad}"></span>
            <span class="mt-name">${escapeHtml(d.msg_type)}</span>
            <span class="mt-pct">${w.toFixed(1)}%</span>
            <div class="mt-bar"><i style="width:${w}%;background:${grad}"></i></div>
            <span class="mt-cnt">${(d.cnt || 0).toLocaleString('zh-CN')}</span>
        </div>`;
    }).join('');
    wrap.innerHTML = `<div class="mt-stack">${segs}</div><div class="mt-legend">${rows}</div>`;
    wrap.querySelectorAll('.mt-row').forEach(row => {
        const idx = row.getAttribute('data-idx');
        row.addEventListener('mouseenter', () => {
            wrap.querySelectorAll('.mt-seg').forEach(s =>
                s.classList.toggle('dim', s.getAttribute('data-idx') !== idx));
            const seg = wrap.querySelector(`.mt-seg[data-idx="${idx}"]`);
            if (seg) seg.classList.add('active');
        });
        row.addEventListener('mouseleave', () => {
            wrap.querySelectorAll('.mt-seg').forEach(s => s.classList.remove('dim', 'active'));
        });
    });
    if (skeleton) skeleton.style.display = 'none';
    wrap.style.display = 'flex';
}

function _msgRenderTopSenders(senders) {
    const container = document.getElementById('msg-top-senders-list');
    if (!container) return;
    if (!senders || senders.length === 0) {
        container.innerHTML = '<div class="empty-state" style="padding: 24px;"><p>暂无数据</p></div>';
        return;
    }
    const topSenders = senders.slice(0, 5);
    container.innerHTML = topSenders.map((s, i) => `
        <div class="kw-top-item">
            <span class="kw-top-rank ${i === 0 ? 'top1' : i === 1 ? 'top2' : i === 2 ? 'top3' : ''}">${i + 1}</span>
            <span class="kw-top-pattern" title="${escapeHtml(s.sender_name || '未知')}">${escapeHtml(s.sender_name || '未知')}</span>
            <span class="kw-top-count">${s.cnt ?? 0}</span>
        </div>
    `).join('');
}

async function loadMessagesAnalytics() {
    // 清理之前创建的 Chart 实例
    if (_msgAnalyticsChart) {
        _msgAnalyticsChart.destroy();
        _msgAnalyticsChart = null;
    }
    // 显示骨架屏
    const skIds = ['msg-word-cloud-skeleton', 'msg-chart-trend-skeleton', 'msg-chart-types-skeleton'];
    skIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = '';
    });
    try {
        const statsData = await api.getMessageStats(7);
        _msgRenderWordCloud(statsData.top_words || []);
        _msgRenderTopSenders(statsData.top_senders || []);
        // 推迟 canvas 图表的渲染——首次展开时再创建，避免 0×0 canvas
        _msgDeferredTrend = statsData.trend || null;
        _msgDeferredTypes = statsData.msg_types || null;
        if (_msgStatsExpanded) _msgRenderDeferredCharts();
    } catch (e) {
        console.error('[messages] 分析面板数据加载失败:', e);
        _msgDeferredTrend = null;
        _msgDeferredTypes = null;
        // 出错时隐藏所有骨架屏，显示错误占位
        ['msg-word-cloud-skeleton', 'msg-chart-trend-skeleton', 'msg-chart-types-skeleton'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.style.display = 'none';
        });
        const emptyHtml = '<div class="empty-state" style="padding: 24px;"><div class="empty-icon" style="font-size:2rem;">&#x26A0;</div><p>数据加载失败</p></div>';
        ['msg-word-cloud-container', 'msg-msgtype-chart-wrap', 'msg-top-senders-list'].forEach(id => {
            const el = document.getElementById(id);
            if (el) { el.innerHTML = emptyHtml; el.style.display = 'block'; }
        });
        const canvas = document.getElementById('msg-chart-trend');
        if (canvas) {
            canvas.style.display = 'none';
            const parent = canvas.parentElement;
            if (parent) {
                const existingErr = parent.querySelector('.msg-chart-error-placeholder');
                if (!existingErr) {
                    const errDiv = document.createElement('div');
                    errDiv.className = 'msg-chart-error-placeholder';
                    errDiv.innerHTML = emptyHtml;
                    parent.appendChild(errDiv);
                }
            }
        }
    }
}

window.loadMessagesAnalytics = loadMessagesAnalytics;

// ============ 消息分析面板折叠 ============
let _msgStatsExpanded = false;
let _msgDeferredTrend = null;
let _msgDeferredTypes = null;
let _msgStatsCharts = [];

function toggleMsgStats() {
    const content = document.getElementById('msg-stats-content');
    const btn = document.getElementById('msg-stats-toggle');
    if (!content) return;
    _msgStatsExpanded = !_msgStatsExpanded;
    content.classList.toggle('open');
    if (btn) btn.classList.toggle('expanded');
    // 首次展开：渲染延迟的图表
    if (_msgStatsExpanded && (_msgDeferredTrend || _msgDeferredTypes)) {
        _msgRenderDeferredCharts();
    }
    // 展开后让已存在的 Chart.js 实例重算尺寸
    if (_msgStatsExpanded) {
        setTimeout(() => {
            _msgStatsCharts.forEach(c => { try { c.resize(); } catch(_) {} });
        }, 100);
    }
}

function _msgRenderDeferredCharts() {
    if (_msgDeferredTrend) {
        _msgRenderMessageTrendChart(_msgDeferredTrend);
        _msgDeferredTrend = null;
    }
    if (_msgDeferredTypes) {
        _msgRenderMsgTypeChart(_msgDeferredTypes);
        _msgDeferredTypes = null;
    }
}

window.toggleMsgStats = toggleMsgStats;

// ── 导出消息记录为 CSV ─────────────────────────────────────────────
async function exportMessagesCSV() {
    try {
        const chatId = _activeChatId || '';
        await api.exportMessages(chatId, 5000);
    } catch (e) {
        showToast('导出失败：' + (e && e.message ? e.message : e), 'error');
    }
}

// ===================== 批量操作 =====================

function _msgToggleBatchMode() {
    _msgBatchMode = !_msgBatchMode;
    _msgSelected = {};
    var btn = document.getElementById('msg-batch-toggle');
    if (btn) btn.textContent = _msgBatchMode ? '退出批量' : '批量';
    var bar = document.getElementById('msg-batch-bar');
    if (bar) bar.style.display = _msgBatchMode ? 'flex' : 'none';
    loadMessages();
}
window._msgToggleBatchMode = _msgToggleBatchMode;

function _msgOnCheck(cb) {
    var id = cb.getAttribute('data-msg-chat-id');
    if (cb.checked) _msgSelected[id] = true; else delete _msgSelected[id];
    _msgUpdateBatchBar();
}

function _msgOnCheckCb(el) {
    // Clicking on the row itself toggles the checkbox when in batch mode
    var id = el.getAttribute('data-chat-id');
    if (_msgSelected[id]) delete _msgSelected[id]; else _msgSelected[id] = true;
    _msgUpdateBatchBar();
    loadMessages();
}
window._msgOnCheckCb = _msgOnCheckCb;

function _msgDeselectAll() {
    _msgSelected = {};
    loadMessages();
}
window._msgDeselectAll = _msgDeselectAll;

function _msgUpdateBatchBar() {
    var bar = document.getElementById('msg-batch-bar');
    if (!bar) return;
    var count = Object.keys(_msgSelected).length;
    var el = document.getElementById('msg-batch-count');
    if (el) el.textContent = count;
    bar.style.display = count > 0 ? 'flex' : 'none';
}

async function _msgBatchExport() {
    var ids = Object.keys(_msgSelected);
    if (ids.length === 0) return;
    try {
        for (var i = 0; i < ids.length; i++) {
            await api.exportMessages(ids[i], 5000);
        }
        toast('已导出 ' + ids.length + ' 个会话的消息');
    } catch (e) {
        toast('批量导出失败: ' + (e.message || e));
    }
}
window._msgBatchExport = _msgBatchExport;

async function _msgBatchDelete() {
    var ids = Object.keys(_msgSelected);
    if (ids.length === 0) return;
    if (!confirm('确认删除 ' + ids.length + ' 个会话的历史消息？此操作不可恢复！')) return;
    try {
        const res = await api.post('/api/messages/batch-delete', { chat_ids: ids });
        if (!res || res.error) {
            toast('批量删除失败: ' + (res && res.error ? res.error : '未知错误'));
            return;
        }
        _msgSelected = {};
        _msgBatchMode = false;
        toast('已删除 ' + ids.length + ' 个会话的消息');
        loadMessages();
    } catch (e) {
        toast('批量删除失败: ' + (e.message || e));
    }
}
window._msgBatchDelete = _msgBatchDelete;
