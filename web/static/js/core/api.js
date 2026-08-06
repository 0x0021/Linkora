class ApiClient {
    constructor(baseUrl = '') {
        this.baseUrl = baseUrl;
        this._auth = null;
        this._loadAuth();
        this._pendingRequests = new Map();
        this._requestCache = new Map();
        this._cacheTTL = 60000;
        this._retryConfig = {
            maxRetries: 2,
            retryDelay: 1000,
            retryOnStatus: [500, 502, 503, 504]
        };
    }

    _loadAuth() {
        try {
            const creds = localStorage.getItem('web_auth');
            if (creds) {
                this._auth = creds;
            }
        } catch (_) {
            // Safari 无痕/禁用存储时会抛 SecurityError，降级为未登录（不影响内存态运行）
        }
    }

    setAuth(username, password) {
        this._auth = 'Basic ' + btoa(unescape(encodeURIComponent(username + ':' + password)));
        try { localStorage.setItem('web_auth', this._auth); } catch (_) {}
    }

    clearAuth() {
        this._auth = null;
        try { localStorage.removeItem('web_auth'); } catch (_) {}
    }

    isAuthenticated() {
        return !!this._auth;
    }

    _getHeaders(extra = {}) {
        const headers = { ...extra };
        if (this._auth) {
            headers['Authorization'] = this._auth;
        }
        return headers;
    }

    getAuthHeaders() {
        return this._getHeaders();
    }

    _handle401(res) {
        if (res.status === 401) {
            this.clearAuth();
            window.dispatchEvent(new CustomEvent('web-auth-required'));
        }
    }

    // 全局错误反馈层：网络/超时/5xx 等不可恢复异常统一提示（复用 showToast 的 error 角色）。
    // showToast 由 app.js 在运行时挂载到 window，api.js 先加载不影响（调用发生在用户操作之后）。
    _notifyGlobalError(msg) {
        if (typeof window.showToast === 'function') {
            try { window.showToast(msg, 'error'); } catch (_) {}
        }
    }

    _downloadBlob(blob, filename) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    cancelRequest(requestId) {
        const controller = this._pendingRequests.get(requestId);
        if (controller) {
            controller.abort();
            this._pendingRequests.delete(requestId);
        }
    }

    clearCache() {
        this._requestCache.clear();
    }

    _generateRequestKey(url, method, body) {
        return `${method}:${url}:${body ? JSON.stringify(body) : ''}`;
    }

    _getCachedResponse(key) {
        const cached = this._requestCache.get(key);
        if (cached && Date.now() - cached.timestamp < this._cacheTTL) {
            return cached.data;
        }
        return null;
    }

    _cacheResponse(key, data) {
        this._requestCache.set(key, {
            data,
            timestamp: Date.now()
        });
    }

    // 多平台隔离：透明给所有数据请求追加 ?platform=<当前平台>（由 store 管理）。
    // 排除：平台列表本身（无平台上下文概念）、健康检查、已含 platform= 的 URL。
    _withPlatform(url) {
        if (!url || url.indexOf('platform=') !== -1) return url;
        if (url === '/api/platforms' || url === '/health') return url;
        if (url.indexOf('/api/platforms') === 0) return url;
        const p = (window.store && typeof window.store.getPlatform === 'function')
            ? window.store.getPlatform() : 'dingtalk';
        const sep = url.indexOf('?') === -1 ? '?' : '&';
        return url + sep + 'platform=' + encodeURIComponent(p);
    }

    async _fetchWithRetry(url, method = 'GET', body = null, retries = 0, timeoutMs = 30000) {
        const requestId = Math.random().toString(36).substr(2, 9);
        const controller = new AbortController();
        this._pendingRequests.set(requestId, controller);

        // 请求超时：默认 30s，可由调用方通过 options.timeoutMs 覆盖
        const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

        try {
            const opts = {
                method,
                headers: this._getHeaders(),
                signal: controller.signal
            };
            if (body) {
                opts.headers['Content-Type'] = 'application/json';
                opts.body = JSON.stringify(body);
            }

            const res = await fetch(`${this.baseUrl}${url}`, opts);

            if (res.status === 401) {
                this._handle401(res);
                return { error: 'unauthorized', status: 401 };
            }

            if (this._retryConfig.retryOnStatus.includes(res.status) && retries < this._retryConfig.maxRetries) {
                await new Promise(r => setTimeout(r, this._retryConfig.retryDelay * (retries + 1)));
                return this._fetchWithRetry(url, method, body, retries + 1);
            } else if (this._retryConfig.retryOnStatus.includes(res.status)) {
                this._notifyGlobalError(`服务端错误（${res.status}），请稍后重试`);
            }

            const ct = (res.headers.get('content-type') || '').toLowerCase();
            if (!ct.includes('application/json')) {
                console.warn(`[api] ${url} 返回非 JSON 内容类型: ${ct}, 状态: ${res.status}`);
                const text = await res.text().catch(() => '(无法读取)');
                console.warn(`[api] 响应文本前200字符: ${text.slice(0, 200)}`);
                return { error: 'non_json_response', status: res.status, _raw: text };
            }

            const json = await res.json();
            return json;
        } catch (e) {
            if (e.name === 'AbortError') {
                this._notifyGlobalError(`请求超时（${timeoutMs / 1000}s），请检查网络后重试`);
                return { error: 'timeout', message: `请求超时（${timeoutMs / 1000}s）` };
            }
            this._notifyGlobalError('网络请求失败，请检查网络连接');
            console.error('[api] fetch 异常:', url, e);
            return { error: 'network_error', message: e.message };
        } finally {
            clearTimeout(timeoutId);
            this._pendingRequests.delete(requestId);
        }
    }

    async fetch(url, method = 'GET', body = null, options = {}) {
        const { useCache = false, cacheTTL = this._cacheTTL, timeoutMs } = options;

        // 透明注入平台参数（含缓存键隔离，避免跨平台缓存串味）
        url = this._withPlatform(url);

        if (useCache) {
            const key = this._generateRequestKey(url, method, body);
            const cached = this._getCachedResponse(key);
            if (cached) {
                return cached;
            }
        }

        const result = await this._fetchWithRetry(url, method, body, 0, timeoutMs);

        if (useCache && result && !result.error) {
            const key = this._generateRequestKey(url, method, body);
            this._cacheResponse(key, result);
        }

        return result;
    }

    async upload(url, formData) {
        const requestId = Math.random().toString(36).substr(2, 9);
        const controller = new AbortController();
        this._pendingRequests.set(requestId, controller);

        try {
            url = this._withPlatform(url);
            const res = await fetch(`${this.baseUrl}${url}`, {
                method: 'POST',
                headers: this._getHeaders(),
                body: formData,
                signal: controller.signal
            });

            if (res.status === 401) {
                this._handle401(res);
                return { error: 'unauthorized', status: 401 };
            }

            return await res.json();
        } catch (e) {
            if (e.name === 'AbortError') {
                return { error: 'aborted' };
            }
            console.error('Upload error:', e);
            return { error: 'network_error', message: e.message };
        } finally {
            this._pendingRequests.delete(requestId);
        }
    }

    async post(url, body = {}, options = {}) {
        return await this.fetch(url, 'POST', body, options);
    }

    async get(url, params = {}, options = {}) {
        const qs = new URLSearchParams(params).toString();
        return await this.fetch(qs ? `${url}?${qs}` : url, 'GET', null, options);
    }

    async put(url, body = {}, options = {}) {
        return await this.fetch(url, 'PUT', body, options);
    }

    async del(url, options = {}) {
        return await this.fetch(url, 'DELETE', null, options);
    }

    async getStatus() {
        return await this.fetch('/api/status', 'GET', null, { useCache: true });
    }

    async getConversations(limit = 50) {
        return await this.fetch(`/api/conversations?limit=${limit}`);
    }

    async getMessages(chatId = '', limit = 50) {
        let url = `/api/messages?limit=${limit}`;
        if (chatId) url += `&chat_id=${encodeURIComponent(chatId)}`;
        return await this.fetch(url);
    }

    async getMemories(params = {}) {
        const qs = new URLSearchParams();
        if (params.limit) qs.set('limit', params.limit);
        if (params.object_type && params.object_type !== 'all') qs.set('object_type', params.object_type);
        if (params.sender) qs.set('sender', params.sender);
        if (params.keyword) qs.set('keyword', params.keyword);
        if (params.scope && params.scope !== 'all') qs.set('scope', params.scope);
        if (params.chat_id) qs.set('chat_id', params.chat_id);
        const q = qs.toString();
        return await this.fetch(`/api/memories${q ? '?' + q : ''}`);
    }

    async getMemoryFacets() {
        return await this.fetch('/api/memories/facets', 'GET', null, { useCache: true });
    }

    async getMemoryClassifySpec() {
        return await this.fetch('/api/memories/classify-spec', 'GET', null, { useCache: true });
    }

    async addMemory(content, source = '', chatId = '', scope = '') {
        const body = { content, source, chat_id: chatId };
        if (scope) body.scope = scope;
        return await this.fetch('/api/memories', 'POST', body);
    }

    async deleteMemory(id) {
        return await this.fetch(`/api/memories/${id}`, 'DELETE');
    }

    async updateMemory(id, content, scope = '') {
        const body = {};
        if (content !== undefined && content !== null) body.content = content;
        if (scope) body.scope = scope;
        return await this.fetch(`/api/memories/${id}`, 'PUT', body);
    }

    async getConfig() {
        return await this.fetch('/api/config', 'GET', null, { useCache: true });
    }

    async updateConfig(config) {
        return await this.fetch('/api/config', 'POST', config);
    }

    async getSystemPrompt() {
        return await this.fetch('/api/llm/prompt', 'GET', null, { useCache: true });
    }

    async updateSystemPrompt(prompt) {
        return await this.fetch('/api/llm/prompt', 'POST', { system_prompt: prompt });
    }

    async getTools() {
        return await this.fetch('/api/tools', 'GET', null, { useCache: true });
    }

    async getKeywords(category = '', search = '', page = 1, limit = 200) {
        let url = `/api/keywords?page=${page}&limit=${limit}`;
        if (category) url += `&category=${encodeURIComponent(category)}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;
        return await this.fetch(url);
    }

    async getKeyword(id) {
        return await this.fetch(`/api/keywords/${id}`);
    }

    async addKeyword(data) {
        return await this.fetch('/api/keywords', 'POST', data);
    }

    async updateKeyword(id, data) {
        return await this.fetch(`/api/keywords/${id}`, 'PUT', data);
    }

    async deleteKeyword(id) {
        return await this.fetch(`/api/keywords/${id}`, 'DELETE');
    }

    async testKeywordMatch(text) {
        return await this.fetch('/api/keywords/test-match', 'POST', { text });
    }

    async batchKeywords(ids, action, category = '') {
        const body = { ids, action };
        if (category) body.category = category;
        return await this.fetch('/api/keywords/batch', 'POST', body);
    }

    async getKeywordStats() {
        return await this.fetch('/api/keywords/stats', 'GET', null, { useCache: true });
    }

    async importKeywords(file) {
        const formData = new FormData();
        formData.append('file', file);
        return await this.upload('/api/keywords/import', formData);
    }

    async exportKeywords(category = '') {
        let url = category
            ? `/api/keywords/export?category=${encodeURIComponent(category)}`
            : '/api/keywords/export';
        url = this._withPlatform(url);
        const res = await fetch(url, { headers: this.getAuthHeaders() });
        if (!res.ok) throw new Error(`导出失败: ${res.status}`);
        const blob = await res.blob();
        this._downloadBlob(blob, 'keywords_export.json');
    }

    async getDingtalkDocs(keyword = '', limit = 100) {
        let url = `/api/dingtalk-docs?limit=${limit}`;
        if (keyword) url += `&keyword=${encodeURIComponent(keyword)}`;
        return await this.fetch(url);
    }

    async getDingtalkDoc(docId) {
        return await this.fetch(`/api/dingtalk-docs/${docId}`);
    }

    async deleteDingtalkDoc(docId) {
        return await this.fetch(`/api/dingtalk-docs/${docId}`, 'DELETE');
    }

    async searchDingtalkDocs(query) {
        return await this.fetch('/api/dingtalk-docs/search', 'POST', { query });
    }

    async syncDingtalkDoc(docId) {
        return await this.fetch(`/api/dingtalk-docs/sync/${docId}`, 'POST');
    }

    async syncBatchDingtalkDocs(query = '') {
        return await this.fetch('/api/dingtalk-docs/sync-batch', 'POST', { query });
    }

    async importDingtalkDocToKb(docId) {
        return await this.fetch('/api/dingtalk-docs/import-kb', 'POST', { doc_id: docId });
    }

    // ---- Feishu Doc API ----
    async searchFeishuDocs(query) {
        return await this.fetch(`/api/kb/feishu-docs?query=${encodeURIComponent(query)}`);
    }

    async importFeishuDoc(docToken, title = '', entityType = '') {
        return await this.fetch('/api/kb/import-from-feishu', 'POST', {
            doc_token: docToken,
            title,
            doc_type: 'feishu',
            entity_type: entityType || '',
        });
    }

    async importFeishuFolder(folderToken) {
        return await this.fetch('/api/kb/import-from-feishu', 'POST', { folder_token: folderToken });
    }

    async setDocAutoSync(docId, autoSync) {
        return await this.fetch(`/api/dingtalk-docs/${docId}/auto-sync`, 'POST', { auto_sync: autoSync });
    }

    async updateDingtalkDoc(docId, data) {
        return await this.fetch(`/api/dingtalk-docs/${docId}`, 'PUT', data);
    }

    async getMessageStats(days = 7) {
        return await this.fetch(`/api/stats/messages?days=${days}`, 'GET', null, { useCache: true });
    }

    async getKbDocs(status = '', docType = '', limit = 50, offset = 0) {
        let url = `/api/kb/documents?limit=${limit}&offset=${offset}`;
        if (status) url += `&status=${status}`;
        if (docType) url += `&doc_type=${docType}`;
        return await this.fetch(url);
    }

    async getKbDocument(docId) {
        return await this.fetch(`/api/kb/documents/${docId}`);
    }

    async createKbDocument(data) {
        return await this.fetch('/api/kb/documents', 'POST', data);
    }

    async deleteKbDocument(docId) {
        return await this.fetch(`/api/kb/documents/${docId}`, 'DELETE');
    }

    async reindexKbDocument(docId) {
        return await this.fetch(`/api/kb/documents/${docId}/reindex`, 'POST');
    }

    async rechunkAllDocs() {
        return await this.fetch('/api/kb/reindex', 'POST');
    }

    async updateKbDocument(docId, data) {
        return await this.fetch(`/api/kb/documents/${docId}`, 'PUT', data);
    }

    async kbQuery(query, topK = 5, minSimilarity = 0.0) {
        return await this.fetch('/api/kb/query', 'POST', {
            query, top_k: topK, min_similarity: minSimilarity
        });
    }

    async kbChat(query, topK = 5, useLlm = false) {
        return await this.fetch('/api/kb/chat', 'POST', { query, top_k: topK, use_llm: useLlm });
    }

    async feedback(payload) {
        return await this.fetch('/api/feedback', 'POST', payload);
    }

    async getKbStats() {
        return await this.fetch('/api/kb/stats', 'GET', null, { useCache: true });
    }

    async getRules() {
        return await this.fetch('/api/rules', 'GET', null, { useCache: true });
    }

    async getToolsConfig() {
        return await this.fetch('/api/tools', 'GET', null, { useCache: true });
    }

    async exportConfig() {
        const res = await fetch(this._withPlatform('/api/config/export'), { headers: this.getAuthHeaders() });
        if (!res.ok) throw new Error(`导出失败: ${res.status}`);
        const blob = await res.blob();
        this._downloadBlob(blob, 'config_export.json');
    }

    async importConfig(formData) {
        return await this.upload('/api/config/import', formData);
    }

    // ── CSV 导出方法 ─────────────────────────────────────────────────
    async _exportCSV(url, filename) {
        const res = await fetch(this._withPlatform(url), { headers: this.getAuthHeaders() });
        if (!res.ok) throw new Error(`导出失败: ${res.status}`);
        const blob = await res.blob();
        this._downloadBlob(blob, filename);
    }

    async exportMessages(chatId = '', limit = 1000) {
        let url = `/api/messages/export?limit=${limit}`;
        if (chatId) url += `&chat_id=${encodeURIComponent(chatId)}`;
        return this._exportCSV(url, `messages_${new Date().toISOString().slice(0, 10)}.csv`);
    }

    async exportDecisions(filters = {}) {
        let url = '/api/decisions/export?limit=10000';
        for (const [k, v] of Object.entries(filters)) {
            if (v) url += `&${k}=${encodeURIComponent(v)}`;
        }
        return this._exportCSV(url, `decisions_${new Date().toISOString().slice(0, 10)}.csv`);
    }

    async exportMetrics(timeRangeHours = 0, limit = 10000) {
        let url = `/api/metrics/export?limit=${limit}`;
        if (timeRangeHours > 0) url += `&time_range_hours=${timeRangeHours}`;
        return this._exportCSV(url, `routing_quality_${new Date().toISOString().slice(0, 10)}.csv`);
    }

    async exportDeadLetters(status = 'all', limit = 10000) {
        return this._exportCSV(`/api/dead-letters/export?status=${status}&limit=${limit}`, `dead_letters_${new Date().toISOString().slice(0, 10)}.csv`);
    }

    async exportCostQuality(hours = 24) {
        return this._exportCSV(`/api/cost-quality/export?hours=${hours}&limit=10000`, `cost_quality_${new Date().toISOString().slice(0, 10)}.csv`);
    }

    async getToolStats(params = {}) {
        let url = '/api/stats/tools';
        const queryParams = [];
        if (params.period) queryParams.push(`days=${params.period}`);
        if (params.top_n) queryParams.push(`top_n=${params.top_n}`);
        if (queryParams.length > 0) url += '?' + queryParams.join('&');
        return await this.fetch(url, 'GET', null, { useCache: true });
    }

    async syncHistory(payload = {}) {
        return await this.fetch('/api/messages/sync-history', 'POST', payload);
    }

    async syncHistoryStatus() {
        return await this.fetch('/api/messages/sync-history/status', 'GET');
    }

    async syncHistoryLog(lines = 80) {
        return await this.fetch(`/api/messages/sync-history/log?lines=${lines}`, 'GET');
    }

    async syncHistoryCancel() {
        return await this.fetch('/api/messages/sync-history/cancel', 'POST', {});
    }

    async getHealth() {
        return await this.fetch('/health');
    }

    async getImageToken() {
        return await this.fetch('/api/image-token');
    }
}

window.ApiClient = ApiClient;
window.api = new ApiClient();