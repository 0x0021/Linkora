// ============ pages/config.js ============
// 由 app.js 按页拆分（P2-13），逻辑未改动

// ============ Config ============
// null-safe 赋值：元素不存在时静默跳过，避免单个缺失字段中断整个配置回填
function _setVal(id, v) {
    const el = document.getElementById(id);
    if (el) el.value = (v === null || v === undefined) ? '' : v;
}
function _setChk(id, v) {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
}

// 配置项内联帮助文案（id → 15字以内中文说明）
const CFG_TOOLTIPS = {
    // 通用设置
    'cfg-poll-interval': '群消息轮询间隔',
    'cfg-poll-reply-cooldown': '单聊回复冷却间隔',
    'cfg-rules-enabled': '启用规则引擎优先匹配',
    'cfg-tools-enabled': 'LLM可调用外部工具',
    // 轮询高级
    'cfg-poll-unread-count': '轮询未读会话上限',
    'cfg-poll-msgs-per-conv': '每个会话拉取消息数',
    'cfg-poll-skip-types': '跳过指定类型的消息',
    'cfg-poll-skip-patterns': '跳过含特定正则的通知(窄签名)',
    'cfg-poll-ai-tag': 'AI消息携带标记',
    'cfg-poll-mark-read': '处理后标为已读',
    'cfg-image-ocr-enabled': '截图自动OCR转文字',
    'cfg-image-temp-dir': 'OCR临时图片目录',
    'cfg-poll-merge': '多会话消息合并窗口',
    'cfg-poll-history-window': '历史消息拉取窗口',
    'cfg-poll-max-msg-ids': '已处理消息ID缓存上限',
    'cfg-poll-list-all-window': '全量列表示窗口(分钟)',
    'cfg-poll-list-all-first-run': '首次全量拉取窗口(分钟)',
    'cfg-poll-empty-protection': '空轮询保护间隔(分钟)',
    // LLM 配置
    'cfg-llm-provider': '大模型服务提供商',
    'cfg-llm-model': '使用的模型名称',
    'cfg-llm-max-tokens': '单次最大生成Token数',
    'cfg-llm-temperature': '生成随机性(0~2)',
    'cfg-llm-max-rounds': '工具调用最大轮数',
    'cfg-llm-converge-rounds': '工具收敛后触发轮次',
    'cfg-llm-model-pool': '候选模型池列表',
    'cfg-llm-fallback-model-pool': '主模型失败后降级池',
    // LLM 高级
    'cfg-llm-daily-chars': '每日闲聊截断字符数',
    'cfg-llm-tech-chars': '技术问题截断字符数',
    'cfg-llm-hard-trunc': '硬截断上限字符数',
    // Embedding
    'cfg-embedding-topk': '相似搜索Top K',
    // 工具路由与限频
    'cfg-tools-routing-mode': '工具路由模式(smart/manual)',
    'cfg-tools-semantic-routing': '启用语义路由',
    'cfg-tools-semantic-threshold': '语义路由相似度阈值',
    'cfg-rate-send-msg': '每小时发消息限额',
    'cfg-rate-create-todo': '每小时创建待办限额',
    'cfg-rate-web-search': '每小时联网搜索限额',
    'cfg-rate-get-weather': '每小时查天气限额',
    'cfg-rate-get-unread': '每小时查未读限额',
    'cfg-rate-get-attendance': '每小时查考勤限额',
    // 规则引擎
    'cfg-intent-filter-enabled': '启用意图过滤',
    'cfg-intent-thank-max': '纯感谢消息最大长度',
    'cfg-intent-ack-max': '纯确认消息最大长度',
    'cfg-intent-biz-threshold': '业务内容比例阈值',
    'cfg-rules-keyword-denylist': '全局关键词黑名单',
    // 死信队列
    'cfg-dlq-enabled': '启用死信队列',
    // 技能引擎
    'cfg-skills-enabled': '启用技能引擎',
    'cfg-skills-auto-activate': '自动激活匹配技能',
    'cfg-skills-semantic-routing': '技能引擎语义路由',
    'cfg-skills-semantic-threshold': '技能匹配相似度阈值',
    'cfg-skills-combo-enabled': '启用技能组合',
    'cfg-skills-combo-gap': '技能组合间隔阈值',
    // 会话摘要
    'cfg-summary-enabled': '启用会话摘要',
    'cfg-summary-max-msgs': '摘要最大消息数',
    'cfg-summary-interval': '摘要生成间隔(小时)',
    'cfg-summary-ratio': '摘要压缩比例',
    // LLM 节流
    'cfg-throttle-enabled': 'LLM请求节流开关',
    // RAG 自动注入
    'cfg-rag-auto-inject': '自动注入知识库',
    'cfg-rag-intent-only': '仅意图消息注入',
    'cfg-rag-min-sim': '知识库最小相似度',
    'cfg-rag-max-results': '知识库最大召回数',
    // 记忆管理
    'cfg-memory-cleanup-enabled': '启用记忆清理',
    'cfg-memory-max-age': '记忆最大保留天数',
    'cfg-memory-min-similarity': '去重最小相似度',
    'cfg-memory-check-interval': '记忆清理检查间隔',
    'cfg-memory-retrieval-similarity': '记忆检索最小相似度',
    // 日志
    'cfg-logging-level': '日志输出级别',
    // 存储
    'cfg-storage-decisions-retention': '决策记录保留天数',
    'cfg-storage-messages-retention': '消息记录保留天数',
    // 安全
    'cfg-safety-fallback': '安全兜底回复文本',
    'cfg-safety-sensitive-words': '敏感词黑名单',
    // 系统提示词
    'cfg-system-prompt': 'LLM系统提示词模板',
};

/** 为配置页标签注入 ? 帮助图标 */
function injectConfigTooltips() {
    Object.entries(CFG_TOOLTIPS).forEach(([id, tip]) => {
        const el = document.getElementById(id);
        if (!el) return;
        // 找到同组 label
        const wrapper = el.closest('.form-group, .toggle-item');
        if (!wrapper) return;
        // 防止重复注入
        if (wrapper.querySelector('.cfg-help-icon')) return;
        const label = wrapper.querySelector(':scope > label, .toggle-item > label:first-of-type');
        if (!label) return;
        const icon = document.createElement('span');
        icon.className = 'cfg-help-icon';
        icon.textContent = '?';
        icon.title = tip;
        icon.setAttribute('aria-label', tip);
        label.appendChild(icon);
    });
}

/** 新增一行模型单价编辑框（标识 / 输入单价 / 输出单价）。 */
function addModelPriceRow(name = '', input = '', output = '') {
    const list = document.getElementById('cfg-model-pricing-list');
    if (!list) return;
    const row = document.createElement('div');
    row.className = 'model-price-row';
    const safeName = String(name).replace(/"/g, '&quot;');
    row.innerHTML =
        '<input type="text" class="form-control mp-name" placeholder="模型标识，如 gpt-4o" value="' + safeName + '">' +
        '<input type="number" class="form-control mp-input" step="0.0001" min="0" placeholder="输入单价" value="' + input + '">' +
        '<input type="number" class="form-control mp-output" step="0.0001" min="0" placeholder="输出单价" value="' + output + '">' +
        '<button type="button" class="btn btn-sm btn-danger" title="删除" onclick="this.closest(\'.model-price-row\').remove()"><i class="fa-solid fa-trash"></i></button>';
    list.appendChild(row);
}
window.addModelPriceRow = addModelPriceRow;

async function loadConfigPage() {
    initSettingsNav();
    try {
    const data = await api.getConfig();
    if (!data) return;

    // DWS 配置（钉钉）— 钉钉面板仅保留提示，字段直接编辑 config.yaml

    // 轮询配置
    _setVal('cfg-poll-interval', data.poller?.interval_seconds || 5);
    _setVal('cfg-poll-merge', data.poller?.merge_window_seconds || 60);
    _setVal('cfg-poll-history-window', data.poller?.history_window || 20);
    _setVal('cfg-poll-unread-count', data.poller?.unread_conversation_count || 50);
    _setVal('cfg-poll-max-msg-ids', data.poller?.max_processed_msg_ids || 500);
    // 注意：cfg-poll-ttl / processed_msg_ttl_seconds 已在 M2 修复中移除（TTL 移交 DB 层
    // sqlite_store.cleanup_processed_msgs），不需要也不应再回填。
    _setVal('cfg-poll-list-all-window', data.poller?.list_all_time_window_minutes || 30);
    _setVal('cfg-poll-list-all-first-run', data.poller?.list_all_first_run_minutes || 5);
    _setVal('cfg-poll-msgs-per-conv', data.poller?.messages_per_conversation || 20);
    _setVal('cfg-poll-reply-cooldown', data.poller?.reply_cooldown_seconds || 10);
    _setVal('cfg-poll-empty-protection', data.poller?.empty_poll_protection_minutes ?? 5);
    _setVal('cfg-poll-skip-types', (data.poller?.skip_msg_types || []).join('\n'));
    _setVal('cfg-poll-skip-patterns', (data.poller?.skip_notification_patterns || []).join('\n'));
    _setChk('cfg-image-ocr-enabled', data.poller?.image_ocr_enabled !== false);
    _setVal('cfg-image-temp-dir', data.poller?.image_temp_dir || './data/tmp_images');

    // LLM 配置
    _setVal('cfg-llm-provider', data.llm?.provider || 'openai-compatible');
    _setVal('cfg-llm-model', data.llm?.model || '');
    _setVal('cfg-llm-max-tokens', data.llm?.max_tokens || 1024);
    _setVal('cfg-llm-temperature', data.llm?.temperature || 0.7);
    _setVal('cfg-llm-max-rounds', data.llm?.max_tool_rounds || 5);
    _setVal('cfg-llm-converge-rounds', data.llm?.converge_after_tool_rounds ?? 3);
    _setVal('cfg-llm-model-pool', (data.llm?.model_pool || []).join('\n'));
    _setVal('cfg-llm-fallback-model-pool', (data.llm?.fallback_model_pool || []).join('\n'));
    // LLM 高级配置
    _setVal('cfg-llm-daily-chars', data.llm?.advanced?.max_chars_daily_chat || 80);
    _setVal('cfg-llm-tech-chars', data.llm?.advanced?.max_chars_tech_issue || 150);
    _setVal('cfg-llm-hard-trunc', data.llm?.advanced?.hard_truncation_chars || 200);

    // 模型价格配置（覆盖/补充内置价目表）
    const mpList = document.getElementById('cfg-model-pricing-list');
    if (mpList) {
        mpList.innerHTML = '';
        const mp = (data.llm && data.llm.model_pricing) || {};
        const entries = Object.entries(mp);
        if (entries.length === 0) {
            addModelPriceRow();
        } else {
            entries.forEach(([name, price]) => {
                addModelPriceRow(name, price && price.input != null ? price.input : 0,
                                       price && price.output != null ? price.output : 0);
            });
        }
    }

    // Embedding 配置
    _setVal('cfg-embedding-topk', data.embedding?.top_k || 5);

    // 功能开关
    _setChk('cfg-tools-enabled', data.tools?.enabled || false);
    _setChk('cfg-rules-enabled', data.rules?.enabled || true);

    // 工具路由与限频
    _setVal('cfg-tools-routing-mode', data.tools?.tool_routing_mode || 'smart');
    _setChk('cfg-tools-semantic-routing', data.tools?.semantic_routing !== false);
    _setVal('cfg-tools-semantic-threshold', data.tools?.semantic_tool_threshold ?? 0.42);
    // 工具限频
    const rl = data.tools?.rate_limit || {};
    _setVal('cfg-rate-send-msg', rl.send_message?.per_hour || 30);
    _setVal('cfg-rate-create-todo', rl.create_todo?.per_hour || 20);
    _setVal('cfg-rate-web-search', rl.web_search?.per_hour || 50);
    _setVal('cfg-rate-get-weather', rl.get_weather?.per_hour || 30);
    _setVal('cfg-rate-get-unread', rl.get_unread?.per_hour || 60);
    _setVal('cfg-rate-get-attendance', rl.get_attendance?.per_hour || 20);

    // 规则引擎
    _setChk('cfg-intent-filter-enabled', data.rules?.intent_filter?.enabled !== false);
    _setVal('cfg-intent-thank-max', data.rules?.intent_filter?.pure_thank_max_length || 20);
    _setVal('cfg-intent-ack-max', data.rules?.intent_filter?.pure_ack_max_length || 10);
    _setVal('cfg-intent-biz-threshold', data.rules?.intent_filter?.business_ratio_threshold || 0.3);
    _setVal('cfg-rules-keyword-denylist', (data.rules?.keyword_denylist || []).join('\n'));

    // 死信队列
    _setChk('cfg-dlq-enabled', data.dead_letter?.enabled !== false);

    // 技能引擎
    _setChk('cfg-skills-enabled', data.skills?.enabled !== false);
    _setChk('cfg-skills-auto-activate', data.skills?.auto_activate !== false);
    _setChk('cfg-skills-semantic-routing', data.skills?.semantic_routing !== false);
    _setVal('cfg-skills-semantic-threshold', data.skills?.semantic_skill_threshold ?? 0.40);
    _setChk('cfg-skills-combo-enabled', data.skills?.combo_enabled !== false);
    _setVal('cfg-skills-combo-gap', data.skills?.combo_gap ?? 0.12);

    // 会话摘要
    _setChk('cfg-summary-enabled', data.memory?.conversation_summary?.enabled !== false);
    _setVal('cfg-summary-max-msgs', data.memory?.conversation_summary?.max_messages_per_conversation || 50);
    _setVal('cfg-summary-interval', data.memory?.conversation_summary?.summary_interval_hours || 24);
    _setVal('cfg-summary-ratio', data.memory?.conversation_summary?.summary_ratio || 0.4);

    // LLM 节流
    _setChk('cfg-throttle-enabled', data.llm_throttle?.enabled !== false);

    // RAG 自动注入
    _setChk('cfg-rag-auto-inject', data.llm?.advanced?.rag_auto_inject !== false);
    _setChk('cfg-rag-intent-only', data.llm?.advanced?.rag_intent_only !== false);
    _setVal('cfg-rag-min-sim', data.llm?.advanced?.rag_min_similarity || 0.6);
    _setVal('cfg-rag-max-results', data.llm?.advanced?.rag_max_results || 1);

    _setChk('cfg-poll-ai-tag', data.poller?.ai_tag_enabled !== false);
    _setChk('cfg-poll-mark-read', data.poller?.mark_read_after_process !== false);

    // 记忆管理配置
    _setChk('cfg-memory-cleanup-enabled', data.memory?.cleanup?.enabled !== false);
    _setVal('cfg-memory-max-age', data.memory?.cleanup?.max_age_days || 90);
    _setVal('cfg-memory-min-similarity', data.memory?.cleanup?.min_similarity_threshold || 0.3);
    _setVal('cfg-memory-check-interval', data.memory?.cleanup?.check_interval_days || 7);
    _setVal('cfg-memory-retrieval-similarity', data.memory?.retrieval?.min_similarity || 0.6);

    // 日志配置
    _setVal('cfg-logging-level', data.logging?.level || 'info');

    // 存储配置
    _setVal('cfg-storage-decisions-retention', data.storage?.decisions_retention_days ?? 30);
    _setVal('cfg-storage-messages-retention', data.storage?.messages_retention_days ?? 90);

    // 安全配置
    _setVal('cfg-safety-fallback', data.safety?.default_fallback || '抱歉，我暂时无法回答这个问题。');
    _setVal('cfg-safety-media-fallback', data.safety?.media_fallback_text || '');
    _setVal('cfg-safety-sensitive-words', (data.safety?.sensitive_words || []).join('\n'));

    // 系统提示词（已从统一配置返回，无需单独请求）
    if (data.llm?.system_prompt) {
        _setVal('cfg-system-prompt', data.llm.system_prompt);
    }

    // 注入 ? 帮助图标
    injectConfigTooltips();
    } catch (e) {
        console.error('loadConfigPage failed:', e);
        showToast('配置加载失败', 'error');
    }
}

async function saveConfig() {
    const btn = document.getElementById('btn-save-config');
    const status = document.getElementById('save-status');
    const data = {
        // 轮询配置（仅收集前端实际存在的控件；HTML 中已移除的字段不再读取，
        // 避免 getElementById 返回 null 导致 .value 抛 TypeError 使保存整页崩溃）
        poller_interval: parseInt(document.getElementById('cfg-poll-interval').value) || 5,
        poller_unread_conversation_count: parseInt(document.getElementById('cfg-poll-unread-count').value) || undefined,
        poller_messages_per_conversation: parseInt(document.getElementById('cfg-poll-msgs-per-conv').value) || undefined,
        poller_reply_cooldown_seconds: parseInt(document.getElementById('cfg-poll-reply-cooldown').value) || undefined,
        poller_skip_msg_types: document.getElementById('cfg-poll-skip-types').value.split('\n').map(w => w.trim()).filter(w => w) || undefined,
        poller_skip_notification_patterns: document.getElementById('cfg-poll-skip-patterns').value.split('\n').map(w => w.trim()).filter(w => w) || undefined,
        poller_image_ocr_enabled: document.getElementById('cfg-image-ocr-enabled').checked,
        // LLM 配置
        llm_provider: document.getElementById('cfg-llm-provider').value.trim() || undefined,
        llm_model: document.getElementById('cfg-llm-model').value.trim() || undefined,
        llm_max_tokens: parseInt(document.getElementById('cfg-llm-max-tokens').value) || undefined,
        llm_temperature: parseFloat(document.getElementById('cfg-llm-temperature').value) || undefined,
        llm_max_tool_rounds: parseInt(document.getElementById('cfg-llm-max-rounds').value) || undefined,
        llm_converge_after_tool_rounds: parseInt(document.getElementById('cfg-llm-converge-rounds').value) || undefined,
        llm_model_pool: document.getElementById('cfg-llm-model-pool').value.split('\n').map(w => w.trim()).filter(w => w) || undefined,
        // 模型价格配置（收集为 {name: {input, output}} 字典）
        model_pricing: (() => {
            const mp = {};
            const rows = document.querySelectorAll('#cfg-model-pricing-list .model-price-row');
            rows.forEach(row => {
                const name = row.querySelector('.mp-name').value.trim();
                if (!name) return;
                const inp = parseFloat(row.querySelector('.mp-input').value);
                const out = parseFloat(row.querySelector('.mp-output').value);
                mp[name] = {
                    input: isNaN(inp) ? 0 : inp,
                    output: isNaN(out) ? 0 : out,
                };
            });
            return mp;
        })(),
        // 系统提示词（并入统一保存，不再需要单独按钮）
        llm_system_prompt: document.getElementById('cfg-system-prompt').value,
        // LLM 高级配置
        llm_advanced_max_chars_daily_chat: parseInt(document.getElementById('cfg-llm-daily-chars').value) || undefined,
        llm_advanced_max_chars_tech_issue: parseInt(document.getElementById('cfg-llm-tech-chars').value) || undefined,
        llm_advanced_hard_truncation_chars: parseInt(document.getElementById('cfg-llm-hard-trunc').value) || undefined,
        // Embedding 配置
        embedding_top_k: parseInt(document.getElementById('cfg-embedding-topk').value) || undefined,
        // 功能开关
        tools_enabled: document.getElementById('cfg-tools-enabled').checked,
        rules_enabled: document.getElementById('cfg-rules-enabled').checked,
        // 记忆管理配置
        memory_cleanup_enabled: document.getElementById('cfg-memory-cleanup-enabled').checked,
        memory_cleanup_max_age_days: parseInt(document.getElementById('cfg-memory-max-age').value) || undefined,
        memory_cleanup_min_similarity_threshold: parseFloat(document.getElementById('cfg-memory-min-similarity').value) || undefined,
        memory_cleanup_check_interval_days: parseInt(document.getElementById('cfg-memory-check-interval').value) || undefined,
        memory_retrieval_min_similarity: parseFloat(document.getElementById('cfg-memory-retrieval-similarity').value) || undefined,
        // 日志配置
        logging_level: document.getElementById('cfg-logging-level').value.trim() || undefined,
        // 存储配置
        storage_decisions_retention_days: parseInt(document.getElementById('cfg-storage-decisions-retention').value) || undefined,
        storage_messages_retention_days: parseInt(document.getElementById('cfg-storage-messages-retention').value) || undefined,
        // 安全配置
        safety_default_fallback: document.getElementById('cfg-safety-fallback').value.trim() || undefined,
        safety_media_fallback_text: document.getElementById('cfg-safety-media-fallback').value.trim() || undefined,
        safety_sensitive_words: document.getElementById('cfg-safety-sensitive-words').value.split('\n').map(w => w.trim()).filter(w => w) || undefined,

        // 工具路由与限频
        tool_routing_mode: document.getElementById('cfg-tools-routing-mode').value.trim() || 'smart',
        tools_semantic_routing: document.getElementById('cfg-tools-semantic-routing').checked,
        tools_semantic_tool_threshold: parseFloat(document.getElementById('cfg-tools-semantic-threshold').value) || undefined,
        tool_rate_limits: {
            send_message: { per_hour: parseInt(document.getElementById('cfg-rate-send-msg').value) || undefined },
            create_todo: { per_hour: parseInt(document.getElementById('cfg-rate-create-todo').value) || undefined },
            web_search: { per_hour: parseInt(document.getElementById('cfg-rate-web-search').value) || undefined },
            get_weather: { per_hour: parseInt(document.getElementById('cfg-rate-get-weather').value) || undefined },
            get_unread: { per_hour: parseInt(document.getElementById('cfg-rate-get-unread').value) || undefined },
            get_attendance: { per_hour: parseInt(document.getElementById('cfg-rate-get-attendance').value) || undefined },
        },
        // 规则引擎
        intent_filter_enabled: document.getElementById('cfg-intent-filter-enabled').checked,
        intent_filter_pure_thank_max_length: parseInt(document.getElementById('cfg-intent-thank-max').value) || undefined,
        intent_filter_pure_ack_max_length: parseInt(document.getElementById('cfg-intent-ack-max').value) || undefined,
        intent_filter_business_ratio_threshold: parseFloat(document.getElementById('cfg-intent-biz-threshold').value) || undefined,
        keyword_denylist: document.getElementById('cfg-rules-keyword-denylist').value.split('\n').map(w => w.trim()).filter(w => w) || undefined,
        // 死信队列
        dlq_enabled: document.getElementById('cfg-dlq-enabled').checked,
        // 技能引擎
        skills_enabled: document.getElementById('cfg-skills-enabled').checked,
        skills_auto_activate: document.getElementById('cfg-skills-auto-activate').checked,
        skills_semantic_routing: document.getElementById('cfg-skills-semantic-routing').checked,
        skills_semantic_skill_threshold: parseFloat(document.getElementById('cfg-skills-semantic-threshold').value) || undefined,
        skills_combo_enabled: document.getElementById('cfg-skills-combo-enabled').checked,
        skills_combo_gap: parseFloat(document.getElementById('cfg-skills-combo-gap').value) || undefined,
        // 会话摘要
        conversation_summary_enabled: document.getElementById('cfg-summary-enabled').checked,
        conversation_summary_max_messages: parseInt(document.getElementById('cfg-summary-max-msgs').value) || undefined,
        conversation_summary_interval_hours: parseInt(document.getElementById('cfg-summary-interval').value) || undefined,
        conversation_summary_ratio: parseFloat(document.getElementById('cfg-summary-ratio').value) || undefined,
        // LLM 节流
        llm_throttle_enabled: document.getElementById('cfg-throttle-enabled').checked,
        // RAG 自动注入
        rag_auto_inject: document.getElementById('cfg-rag-auto-inject').checked,
        rag_intent_only: document.getElementById('cfg-rag-intent-only').checked,
        rag_min_similarity: parseFloat(document.getElementById('cfg-rag-min-sim').value) || undefined,
        rag_max_results: parseInt(document.getElementById('cfg-rag-max-results').value) || undefined,
        // 高级轮询参数
        poller_ai_tag: document.getElementById('cfg-poll-ai-tag').checked,
        poller_mark_read: document.getElementById('cfg-poll-mark-read').checked,
    };
    Object.keys(data).forEach(key => {
        if (data[key] === undefined) delete data[key];
    });
    if (btn) btn.disabled = true;
    if (status) { status.className = 'save-status show loading'; status.textContent = '正在保存…'; }
    try {
    const result = await api.updateConfig(data);
    if (result && result.success) {
        if (status) { status.className = 'save-status show success'; status.textContent = '✓ 已保存'; setTimeout(() => { status.className = 'save-status'; }, 3000); }
        showToast(result.message || '配置保存成功');
    } else {
        if (status) { status.className = 'save-status show error'; status.textContent = '✗ 保存失败'; setTimeout(() => { status.className = 'save-status'; }, 5000); }
        showToast(result?.message || '保存失败', 'error');
    }
    } catch (e) {
        console.error('saveConfig failed:', e);
        showToast('配置保存失败', 'error');
        if (status) { status.className = 'save-status show error'; status.textContent = '✗ 保存失败'; setTimeout(() => { status.className = 'save-status'; }, 5000); }
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function restoreDefaultConfig() {
    if (!confirm('确定恢复所有配置为默认值？当前自定义配置将被覆盖。')) return;
    const status = document.getElementById('save-status');
    if (status) { status.className = 'save-status show loading'; status.textContent = '正在恢复…'; }
    try {
    // 调用 API 加载默认配置
    const result = await api.fetch('/api/config/default', 'POST');
    const json = result;
    if (json && json.success) {
        // 重新加载配置到表单
        await loadConfigPage();
        if (status) { status.className = 'save-status show success'; status.textContent = '✓ 已恢复默认'; setTimeout(() => { status.className = 'save-status'; }, 3000); }
        showToast('已恢复默认配置');
    } else {
        if (status) { status.className = 'save-status show error'; status.textContent = '✗ 恢复失败'; setTimeout(() => { status.className = 'save-status'; }, 5000); }
        showToast(json.message || '恢复失败', 'error');
    }
    } catch (e) {
        console.error('restoreDefaultConfig failed:', e);
        showToast('恢复默认配置失败', 'error');
        if (status) { status.className = 'save-status show error'; status.textContent = '✗ 恢复失败'; setTimeout(() => { status.className = 'save-status'; }, 5000); }
    }
}

function closeDocViewModal() {
    document.getElementById('doc-view-modal').classList.remove('active');
}


// ============ 目标组织（只读展示当前组织）============
async function refreshOrgSelector() {
    const el = document.getElementById('cfg-current-org');
    if (!el) return;
    try {
        const data = await api.fetch('/api/orgs');
        if (!data || data.error) { el.textContent = '（未登录）'; return; }
        const current = data.current || {};
        el.textContent = current.corp_name || '(未知组织)';
    } catch (e) {
        el.textContent = '（获取失败）';
    }
}


// ============ 设置页左侧分类导航 + 分页切换 ============
// 点击左侧导航项 → 右侧只显示对应面板（分页模式），不再滚动定位。
// 首次进入默认显示第一个面板；搜索过滤可用面板集合。
const SETTINGS_CATEGORIES = [
    { key: 'general',      title: '通用设置',   icon: 'fa-gear',                 panels: ['通用设置'] },
    { key: 'model',        title: '模型与提示', icon: 'fa-robot',               panels: ['LLM 模型', '嵌入模型', '模型价格配置', '系统提示词'] },
    { key: 'polling',      title: '采集与过滤', icon: 'fa-satellite-dish',      panels: ['轮询高级', '死信队列', '规则引擎'] },
    { key: 'intelligence', title: '智能引擎',   icon: 'fa-wand-magic-sparkles', panels: ['LLM 节流与限速', '工具与限频', '技能引擎'] },
    { key: 'data',         title: '数据与存储', icon: 'fa-database',            panels: ['记忆、日志与存储', '安全与降级'] },
];
let _settingsNavReady = false;
let _activeConfigSlug = null;  // 当前显示的面板 slug

function initSettingsNav() {
    const list = document.getElementById('settingsNavList');
    if (!list || _settingsNavReady) return;
    const content = document.getElementById('settingsContent');
    if (!content) return;
    const panels = Array.from(content.querySelectorAll('.panel.config-section'));
    if (!panels.length) return;

    // 为每个面板生成稳定 id，并建立「标题 → 面板」映射
    const titleToPanel = {};
    panels.forEach((p, i) => {
        const h3 = p.querySelector('.panel-header h3');
        const title = h3 ? h3.textContent.trim() : ('section-' + i);
        const slug = 'cfg-' + title.replace(/[^\w一-龥]/g, '').slice(0, 16);
        p.id = slug;
        p.dataset.configSlug = slug;
        // 初始全部隐藏
        p.style.display = 'none';
        titleToPanel[title] = { el: p, slug };
    });

    // 渲染分类标题 + 条目
    const frag = document.createDocumentFragment();
    SETTINGS_CATEGORIES.forEach(cat => {
        const catEl = document.createElement('div');
        catEl.className = 'settings-nav-cat';
        catEl.textContent = cat.title;
        frag.appendChild(catEl);
        cat.panels.forEach(title => {
            const info = titleToPanel[title];
            if (!info) return;
            const a = document.createElement('a');
            a.className = 'settings-nav-item';
            a.href = '#' + info.slug;
            a.dataset.target = info.slug;
            a.dataset.search = (cat.title + ' ' + title).toLowerCase();
            a.innerHTML = '<i class="fa-solid ' + cat.icon + '"></i><span>' + title + '</span>';
            a.addEventListener('click', (e) => {
                e.preventDefault();
                switchConfigPanel(info.slug);
            });
            frag.appendChild(a);
        });
    });
    list.appendChild(frag);

    // 搜索过滤（按「分类 + 设置项」文本匹配）
    const search = document.getElementById('settingsSearch');
    if (search) {
        search.addEventListener('input', () => {
            const q = search.value.trim().toLowerCase();
            const items = list.querySelectorAll('.settings-nav-item');
            let visible = 0;
            items.forEach(it => {
                const hit = !q || it.dataset.search.includes(q);
                it.style.display = hit ? '' : 'none';
                if (hit) visible++;
            });
            // 隐藏没有可见条目的分类标题
            list.querySelectorAll('.settings-nav-cat').forEach(catEl => {
                let next = catEl.nextElementSibling;
                let hasItem = false;
                while (next && !next.classList.contains('settings-nav-cat')) {
                    if (next.classList.contains('settings-nav-item') && next.style.display !== 'none') hasItem = true;
                    next = next.nextElementSibling;
                }
                catEl.style.display = (q && !hasItem) ? 'none' : '';
            });
            let empty = list.querySelector('.settings-nav-empty');
            if (!q) { if (empty) empty.remove(); }
            else if (visible === 0) {
                if (!empty) {
                    empty = document.createElement('div');
                    empty.className = 'settings-nav-empty';
                    empty.textContent = '无匹配设置项';
                    list.appendChild(empty);
                }
            } else if (empty) empty.remove();

            // 搜索时自动跳转到第一个可见项
            if (q && visible > 0) {
                const firstVisible = list.querySelector('.settings-nav-item[style*=""], .settings-nav-item:not([style])');
                if (firstVisible && firstVisible.dataset.target !== _activeConfigSlug) {
                    switchConfigPanel(firstVisible.dataset.target);
                }
            }
        });
    }

    _settingsNavReady = true;

    // 默认显示第一个面板
    const firstItem = list.querySelector('.settings-nav-item');
    if (firstItem) {
        switchConfigPanel(firstItem.dataset.target);
    }
}

/** 切换到指定配置面板（分页模式） */
function switchConfigPanel(slug) {
    if (!slug) return;
    _activeConfigSlug = slug;

    // 更新导航高亮
    const items = document.querySelectorAll('#settingsNavList .settings-nav-item');
    items.forEach(it => it.classList.toggle('active', it.dataset.target === slug));

    // 切换面板显隐（带淡入动画）
    const content = document.getElementById('settingsContent');
    if (!content) return;
    content.querySelectorAll('.panel.config-section').forEach(p => {
        if (p.dataset.configSlug === slug) {
            p.style.display = '';
            p.classList.add('config-panel-visible');
            // 重置动画
            p.style.animation = 'none';
            void p.offsetHeight; // force reflow
            p.style.animation = '';
        } else {
            p.style.display = 'none';
            p.classList.remove('config-panel-visible');
        }
    });
}
window.switchConfigPanel = switchConfigPanel;

