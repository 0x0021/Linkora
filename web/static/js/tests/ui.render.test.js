// ui.render.test.js — core/ui.js 共享组件渲染契约（Phase 2 质量护栏）
// 验证 renderEmptyState / renderErrorState / renderThreadSkeleton 的输出结构与各页
// 原有内联空态逐字节一致（迁移零行为变更），且 message 经 escapeHtml 转义。
import { describe, it, expect, beforeAll } from 'vitest';

let ui;
beforeAll(async () => {
  // ui.js 桥接到 window.Linkora，供经典脚本与测试取用
  await import('../core/ui.js');
  expect(window.Linkora).toBeTruthy();
  ui = window.Linkora;
  expect(typeof ui.renderEmptyState).toBe('function');
  expect(typeof ui.renderErrorState).toBe('function');
  expect(typeof ui.renderThreadSkeleton).toBe('function');
});

describe('renderEmptyState', () => {
  it('默认图标为 💬（与 messages.js 原内联一致）', () => {
    expect(ui.renderEmptyState('暂无消息')).toBe(
      '<div class="empty-state"><div class="empty-icon">💬</div><p>暂无消息</p></div>'
    );
  });

  it('自定义图标（与 messages.js 错误态一致）', () => {
    expect(ui.renderEmptyState('加载失败', { icon: '&#x26A0;' })).toBe(
      '<div class="empty-state"><div class="empty-icon">&#x26A0;</div><p>加载失败</p></div>'
    );
  });

  it('noIcon + style 不渲染图标（与 dashboard/messages padding 空态一致）', () => {
    expect(ui.renderEmptyState('暂无数据', { style: 'padding: 24px;', noIcon: true })).toBe(
      '<div class="empty-state" style="padding: 24px;"><p>暂无数据</p></div>'
    );
  });

  it('iconStyle 作用于 .empty-icon（与 messages.js 数据加载失败一致）', () => {
    expect(
      ui.renderErrorState('数据加载失败', { style: 'padding: 24px;', iconStyle: 'font-size:2rem;' })
    ).toBe(
      '<div class="empty-state" style="padding: 24px;"><div class="empty-icon" style="font-size:2rem;">&#x26A0;</div><p>数据加载失败</p></div>'
    );
  });

  it('message 经 escapeHtml 转义（守住 XSS 卫生）', () => {
    const html = ui.renderEmptyState('<b>x</b> & "y"');
    expect(html).toContain('&lt;b&gt;x&lt;/b&gt;');
    expect(html).toContain('&amp;');
    expect(html).toContain('&quot;y&quot;');
    expect(html).not.toContain('<b>');
  });
});

describe('renderErrorState', () => {
  it('默认警告图标', () => {
    expect(ui.renderErrorState('出错了')).toBe(
      '<div class="empty-state"><div class="empty-icon">&#x26A0;</div><p>出错了</p></div>'
    );
  });

  it('不透传修改调用方的 opts 对象', () => {
    const opts = { icon: 'X' };
    ui.renderErrorState('a', opts);
    expect(opts).toEqual({ icon: 'X' });
  });
});

describe('renderThreadSkeleton', () => {
  it('默认 6 行骨架（与 messages.js 原 thread-skeleton 结构一致）', () => {
    const html = ui.renderThreadSkeleton();
    expect(html.startsWith('<div class="thread-skeleton">')).toBe(true);
    expect((html.match(/sk-msg-row/g) || []).length).toBe(6);
    expect((html.match(/skeleton-line/g) || []).length).toBe(12);
  });

  it('支持指定行数', () => {
    expect((ui.renderThreadSkeleton(2).match(/sk-msg-row/g) || []).length).toBe(2);
  });
});
