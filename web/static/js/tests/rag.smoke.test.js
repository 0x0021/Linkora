// rag.js 冒烟测试（P1-3）
// 经典脚本：在浏览器里顶层 function 即全局；这里显式挂到 window 以便 ESM/测试访问。
import { describe, it, expect, beforeEach } from 'vitest';

describe('rag.js (smoke)', () => {
  beforeEach(async () => {
    // 每个用例独立重新加载模块，保证 window 暴露是干净的
    vi.resetModules();
    await import('../pages/rag.js');
  });

  it('脚本加载且不抛错，并暴露纯函数与入口', () => {
    expect(typeof window.formatSource).toBe('function');
    expect(typeof window.formatDocType).toBe('function');
    expect(typeof window.loadRagPage).toBe('function');
  });

  it('formatSource 把来源值映射为可读彩色 tag', () => {
    expect(window.formatSource('dingtalk')).toContain('钉钉文档');
    expect(window.formatSource('manual')).toContain('手动录入');
    expect(window.formatSource('upload:abc')).toContain('本地上传');
    expect(window.formatSource('web:news')).toContain('WEB');
    expect(window.formatSource('https://example.com/x')).toContain('WEB');
    // 空来源 → 占位灰 tag
    expect(window.formatSource('')).toContain('tag-gray');
    // 未知来源 → 走 escapeHtml 兜底分支
    expect(window.formatSource('marketplace:xyz')).toContain('marketplace:xyz');
  });

  it('formatDocType 把 doc_type 映射为可读中文 tag', () => {
    expect(window.formatDocType('pdf')).toContain('PDF');
    expect(window.formatDocType('markdown')).toContain('Markdown');
    expect(window.formatDocType('faq')).toContain('FAQ');
    expect(window.formatDocType('upload')).toContain('本地上传');
    expect(window.formatDocType('')).toContain('未知');
    expect(window.formatDocType('weird-type')).toContain('weird-type');
  });
});
