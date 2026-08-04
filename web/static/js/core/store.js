class AppStore {
    constructor() {
        this._state = {
            auth: {
                isAuthenticated: false,
                username: null
            },
            ui: {
                currentPage: 'dashboard',
                sidebarCollapsed: false,
                theme: 'light',
                loading: false,
                notification: null
            },
            data: {
                conversations: [],
                messages: {},
                keywords: [],
                kbDocs: [],
                stats: {},
                memories: [],
                config: {}
            }
        };
        this._listeners = new Map();
        this._persistKeys = ['ui.theme', 'ui.sidebarCollapsed'];
        this._loadPersistedState();
    }

    _loadPersistedState() {
        try {
            this._persistKeys.forEach(key => {
                const saved = localStorage.getItem(`dt-store-${key}`);
                if (saved) {
                    const [parent, child] = key.split('.');
                    this._state[parent][child] = JSON.parse(saved);
                }
            });
        } catch (e) {
            console.error('[store] Failed to load persisted state:', e);
        }
    }

    _persistState(key) {
        if (this._persistKeys.includes(key)) {
            try {
                const [parent, child] = key.split('.');
                localStorage.setItem(`dt-store-${key}`, JSON.stringify(this._state[parent][child]));
            } catch (e) {
                console.error('[store] Failed to persist state:', e);
            }
        }
    }

    subscribe(key, callback) {
        if (!this._listeners.has(key)) {
            this._listeners.set(key, []);
        }
        this._listeners.get(key).push(callback);
        return () => {
            const callbacks = this._listeners.get(key);
            if (callbacks) {
                const index = callbacks.indexOf(callback);
                if (index > -1) callbacks.splice(index, 1);
            }
        };
    }

    _notify(key, value) {
        const callbacks = this._listeners.get(key);
        if (callbacks) {
            callbacks.forEach(cb => cb(value));
        }
    }

    get(path) {
        const keys = path.split('.');
        let result = this._state;
        for (const key of keys) {
            if (result && typeof result === 'object' && key in result) {
                result = result[key];
            } else {
                return undefined;
            }
        }
        return result;
    }

    set(path, value) {
        const keys = path.split('.');
        const lastKey = keys.pop();
        let target = this._state;
        for (const key of keys) {
            if (!target[key]) {
                target[key] = {};
            }
            target = target[key];
        }
        target[lastKey] = value;
        this._notify(path, value);
        this._persistState(path);
    }

    update(path, updater) {
        const current = this.get(path);
        const updated = updater(current);
        this.set(path, updated);
        return updated;
    }

    merge(path, partial) {
        const current = this.get(path) || {};
        this.set(path, { ...current, ...partial });
    }

    // ============ 领域切片（Service 层使用） ============
    // 在 data.<domain>.<key> 之上提供 domain 语义化读写与订阅，
    // 兼容既有 subscribe/set/get(path)；多平台隔离沿用 clearData()（切换平台清空 data.*）。
    slice(domain, key) {
        return this.get('data.' + domain + '.' + key);
    }

    setSlice(domain, key, value) {
        this.set('data.' + domain + '.' + key, value);
        return value;
    }

    subscribeSlice(domain, key, cb) {
        return this.subscribe('data.' + domain + '.' + key, cb);
    }

    // 多平台隔离：当前选中的平台（持久化到 localStorage，跨刷新保留）。
    // 缺省 "dingtalk" 保证向后兼容。非法/无效平台由前端切换器在初始化时回退。
    getPlatform() {
        try {
            const p = localStorage.getItem('dt-platform');
            if (!p) return 'dingtalk';
            // 校验：仅接受已知平台 id，防止 localStorage 被污染导致切换器异常
            const known = (window.__PLATFORMS__ || []).map(x => x.id);
            if (known.length && !known.includes(p)) {
                console.warn('[store] localStorage 中平台 %s 非法，回退默认 dingtalk', p);
                return 'dingtalk';
            }
            return p;
        } catch (_) {
            return 'dingtalk';
        }
    }

    setPlatform(p) {
        if (!p) p = 'dingtalk';
        try { localStorage.setItem('dt-platform', p); } catch (_) {}
        this._notify('platform', p);
    }

    // 清空前端数据缓存，强制按新平台隔离重渲染（切换平台时调用）
    clearData() {
        this.set('data.conversations', []);
        this.set('data.messages', {});
        this.set('data.keywords', []);
        this.set('data.kbDocs', []);
        this.set('data.stats', {});
        this.set('data.memories', []);
        this.set('data.config', {});
    }
}

window.store = new AppStore();