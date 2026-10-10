// 草稿审阅页：过滤器与搜索渲染回归测试
// 覆盖：点击「全部/待处理」应正确请求对应 status 并渲染；搜索框处理器存在且能触发加载。
import { describe, it, expect, vi, beforeEach } from 'vitest';

// 补齐 drafts.js 依赖的全局（setup.js 未覆盖）
globalThis.setText = vi.fn();
globalThis.renderPager = vi.fn();
globalThis.toast = vi.fn();
globalThis.L = { actions: {} };
globalThis.confirm = vi.fn(() => true);

// 真实加载草稿页模块（顶层会把处理函数挂到 window）
await import('../pages/drafts.js');

function makeItems(status, n) {
  const arr = [];
  for (let i = 0; i < n; i++) {
    arr.push({
      draft_id: status + '-' + i,
      platform: 'dingtalk',
      sender_name: '张三',
      sender_id: 'ou_x',
      conversation_name: '会话' + i,
      conversation_id: 'cid_x',
      user_message: '用户消息' + i,
      ai_reply: 'AI回复' + i,
      rag_confidence: 0.9,
      status,
      created_at: '2026-10-10T12:00:00',
    });
  }
  return arr;
}

const ALL_ITEMS = [
  ...makeItems('approved', 2),
  ...makeItems('discarded', 11),
  ...makeItems('pending', 3),
];

let lastUrl = '';

beforeEach(() => {
  // 注入真实 jsdom 节点，使 getElementById 命中、innerHTML 可读
  document.body.innerHTML = `
    <div id="draft-tabs-container"></div>
    <input id="draft-search" value="" />
    <div id="drafts-content"></div>
    <div id="draft-batch-bar"></div>
    <span id="draft-batch-count"></span>
    <button id="draft-batch-read-btn"></button>
    <span id="draft-stat-pending"></span>
    <span id="draft-stat-approved"></span>
    <span id="draft-stat-discarded"></span>
    <span id="draft-pending-badge"></span>
  `;
  lastUrl = '';
  globalThis.api = {
    fetch: vi.fn(async (url) => {
      lastUrl = url;
      const m = url.match(/status=([^&]+)/);
      const st = m ? decodeURIComponent(m[1]) : 'all';
      const items = st === 'all' ? ALL_ITEMS : ALL_ITEMS.filter((x) => x.status === st);
      return { success: true, items, total: items.length, count: items.length, pending_count: 3 };
    }),
    post: vi.fn(async () => ({ success: true })),
  };
});

function rowCount() {
  return (document.getElementById('drafts-content').innerHTML.match(/class="draft-row"/g) || []).length;
}

describe('草稿过滤器渲染', () => {
  it('点击「全部」请求 status=all 并渲染全部 16 条', async () => {
    await window._draftSwitchStatus('all');
    await new Promise((r) => setTimeout(r, 30));
    expect(lastUrl).toContain('status=all');
    expect(rowCount()).toBe(16);
  });

  it('点击「待处理」请求 status=pending 并渲染 3 条', async () => {
    await window._draftSwitchStatus('pending');
    await new Promise((r) => setTimeout(r, 30));
    expect(lastUrl).toContain('status=pending');
    expect(rowCount()).toBe(3);
  });

  it('搜索框处理器已注册，能触发 loadDraftsPage', async () => {
    expect(typeof window.debouncedLoadDraftsPage).toBe('function');
    await window._draftSwitchStatus('all'); // 先重置到「全部」状态
    await new Promise((r) => setTimeout(r, 30));
    window.debouncedLoadDraftsPage();
    await new Promise((r) => setTimeout(r, 350)); // 等防抖（300ms）+ 渲染
    expect(globalThis.api.fetch).toHaveBeenCalled();
    expect(rowCount()).toBe(16); // 搜索框为空 → 不过滤 → 全部 16 条
  });
});
