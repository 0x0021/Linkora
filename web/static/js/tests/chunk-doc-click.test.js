// 行为级回归：分块管理页点击左侧文档，右侧内容页必须更新。
//
// 根因（2026-10-28）：rag.js 列表项的 data-args 写成了
//   data-args=\'[\' + d.id + \']\'
// 单引号 JS 字符串里 \' 是【转义引号】而非字符串边界，于是 "+ d.id +"
// 整段成为字符串内的字面文本、从不插值——渲染出 data-args='['：
//   click → runAction → JSON.parse('[') 失败 → args=[] → selectChunkDoc() 空参
//   → selectedChunkDocId=undefined → viewKbDocChunks 提前 return → 右侧永不更新。
// 首个文档有内容是因为加载时直接 JS 调用 selectChunkDoc(id)（不经 data-args），
// 所以「默认有内容、点击无反应」，极具迷惑性。
//
// 本测试加载【真实源码】(rag.js + app.js) 在 jsdom 里走完整链路：
// 渲染列表 → 真实派发 click（冒泡到 app.js 的 document 级委托）→ 断言右侧切换。
// 任何让 data-args 失效的写法回归（minify 错绑、转义引号、拼接变量改名…）都会在此报警。
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const ROOT = process.cwd();

function loadRealScript(rel) {
  window.eval(readFileSync(join(ROOT, 'web/static/js', rel), 'utf8'));
}

describe('分块管理：点击左侧文档 → 右侧内容页更新（回归 data-args 字面量拼接）', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="chunk-doc-count"></div>
      <div id="chunk-doc-list"></div>
      <span id="chunk-selected-doc"></span>
      <span id="chunk-count"></span>
      <div id="chunk-list"></div>`;

    window.api = {
      getKbDocs: async () => ({ documents: [
        { id: 86, title: 'DocA', chunk_count: 2, source: 'dingtalk' },
        { id: 82, title: 'DocB', chunk_count: 1, source: 'manual' },
      ] }),
      getKbDocument: async (id) => ({ document: { title: 'Doc' + id, chunks: [
        { chunk_index: 0, content: '内容' + id, embedding: 'x' },
      ] } }),
    };
    window.debounce = (fn) => fn;
    window.iconize = (s) => s;

    // 按浏览器加载顺序加载真实源码：页面脚本先、app.js 后。
    // app.js 末尾 init 块引用了其他页面脚本（keywords 等）的全局，在测试环境会抛错——
    // 预期内：其 document 级事件委托在 init 块之前注册，行为链完整可用。
    loadRealScript('pages/rag.js');
    try { loadRealScript('core/app.js'); } catch (_) { /* init 块缺其他页面全局，预期 */ }
  });

  it('渲染的 data-args 必须是可解析 JSON（历史 bug 渲染成 "["）', async () => {
    await window.loadKbChunksPage();
    const items = document.querySelectorAll('.chunk-doc-item');
    expect(items.length).toBe(2);
    for (const el of items) {
      expect(() => JSON.parse(el.getAttribute('data-args'))).not.toThrow();
    }
    expect(items[1].getAttribute('data-args')).toBe('[82]');
  });

  it('点击第二个文档 → 右侧标题/分块卡片/高亮全部切换', async () => {
    await window.loadKbChunksPage();
    const items = document.querySelectorAll('.chunk-doc-item');
    const second = items[1];

    second.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 20));

    expect(document.getElementById('chunk-selected-doc').textContent).toBe('Doc82');
    expect(document.getElementById('chunk-count').textContent).toContain('1');
    expect(document.querySelectorAll('#chunk-list .chunk-card').length).toBe(1);
    expect(second.classList.contains('active')).toBe(true);
    expect(items[0].classList.contains('active')).toBe(false);
  });
});
