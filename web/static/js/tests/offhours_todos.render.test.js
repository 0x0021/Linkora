import { describe, it, expect, vi, beforeEach } from 'vitest';

globalThis.renderPager = vi.fn();
globalThis.showToast = vi.fn();
globalThis.confirm = vi.fn(() => true);

await import('../pages/offhours_todos.js');

const rules = {
    enabled: true,
    workdays: '1,2,3,4,5',
    work_start: '09:00',
    work_end: '18:00',
    timezone: 'Asia/Shanghai',
    lookback_hours: 24,
    scan_interval_minutes: 15,
    auto_create: true,
    notify_sender: true,
    notify_owner: true,
};

const unanswered = {
    success: true,
    total: 1,
    items: [{
        dedupe_key: 'dingtalk:cid-1:m1',
        platform: 'dingtalk',
        chat_id: 'cid-1',
        chat_name: '项目群',
        sender_name: '张三',
        msg_timestamp: '2026-11-22T20:05:00',
        reason: 'weekend',
        content_preview: '明天能给我回复吗？',
        status: 'pending',
    }],
};

let urls = [];

beforeEach(() => {
    urls = [];
    document.body.innerHTML = `
        <div id="otd-rules"></div>
        <div id="otd-stats"></div>
        <div id="offhours-todos-content"></div>
    `;
    globalThis.api = {
        fetch: vi.fn(async (url) => {
            urls.push(url);
            if (url.includes('/rules')) return { success: true, rules };
            if (url.includes('/stats')) return { success: true, stats: { dingtalk: { pending: 1, todo_created: 0, replied_later: 0 } } };
            return unanswered;
        }),
        post: vi.fn(async (url) => ({ success: true, result: { offhours: 3, todos_created: 2, notify_only: 1 } })),
    };
});

describe('未回复待办页面', () => {
    it('加载规则、统计与未回复消息列表', async () => {
        await window.loadOffhoursTodosPage();
        expect(urls.some((u) => u.includes('/rules'))).toBe(true);
        expect(urls.some((u) => u.includes('/stats'))).toBe(true);
        expect(urls.some((u) => u.includes('/unanswered'))).toBe(true);
        const content = document.getElementById('offhours-todos-content').innerHTML;
        expect(content).toContain('张三');
        expect(content).toContain('建单');
        expect(document.getElementById('otd-rules').innerHTML).toContain('工作时段');
    });

    it('切到记录 tab 请求 records 接口', async () => {
        await window.loadOffhoursTodosPage();
        const el = { dataset: { tab: 'records' } };
        await window._otdSwitchTab(el);
        expect(urls.some((u) => u.includes('/records'))).toBe(true);
    });

    it('手动扫描调用 scan 接口并提示结果', async () => {
        await window.loadOffhoursTodosPage();
        await window._otdScan();
        expect(api.post).toHaveBeenCalledWith(
            '/api/offhours-todos/scan',
            expect.objectContaining({ create_todos: true }),
        );
        expect(showToast).toHaveBeenCalled();
    });
});
