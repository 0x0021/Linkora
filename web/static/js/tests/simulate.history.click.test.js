// 行为级回归：模拟测试页点击「测试历史」条目 → 内容与发送人正确回填到输入框。
//
// 根因（2026-10-29）：renderSimHistory 用
//   data-args='["${escapeHtml(item.content)}", "${escapeHtml(item.sender_name || '')}"]'
// 「先 HTML 转义、再手工拼 JSON」。浏览器解析 HTML 属性时会**还原 HTML 实体**，
// 于是内容里的裸 " 变回引号、内容里的**真实换行**原样留在 JSON 字符串里：
//   - 裸引号   → JSON 结构被截断（Expecting ',' delimiter）
//   - 裸换行   → JSON 规范禁止字符串内出现控制字符（Invalid control character）
// 两者都会让 data-args 的 JSON.parse 抛错 → app.js 的 runAction 退化成 args=[]
// → useHistory() 收到 undefined → 输入框被填成字面量 "undefined"。
//
// 修复：改用 jsonAttr()（先 JSON.stringify 正确转义引号/换行/反斜杠，再整体 HTML 转义）。
//
// 本测试加载【真实源码】(core/util.js + pages/simulate.js + core/app.js) 在 jsdom 里
// 走完整链路：渲染历史列表 → 真实派发 click（冒泡到 app.js 的 document 级委托）
// → 断言输入框拿到的是**原始内容**（含引号/换行），而不是 "undefined"。
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'fs';
import { join } from 'path';

const ROOT = process.cwd();

function loadRealScript(rel) {
  window.eval(readFileSync(join(ROOT, 'web/static/js', rel), 'utf8'));
}

// 会破坏「手工拼 JSON」的载荷：裸双引号、单引号、真实换行、反斜杠、& 实体字符
const NASTY_CONTENT = '帮我分析一下上周的销售数据，看看哪个产品卖得\n最好 "带引号" \\ 反斜杠 & 符号';
const NASTY_SENDER = '张"三"';

describe('模拟测试：点击历史记录 → 内容与发送人正确回填（回归 data-args JSON.parse 失败）', () => {
  let historyItems;

  beforeEach(() => {
    document.body.innerHTML = `
      <input id="simulate-sender" />
      <textarea id="simulate-content"></textarea>
      <div id="sim-avatar"></div>
      <input id="sim-search" />
      <div id="sim-history-list"></div>`;

    historyItems = [
      { id: 'h-1', content: NASTY_CONTENT, sender_name: NASTY_SENDER, timestamp: '2026-10-29T10:00:00' },
    ];

    window.api = { get: async () => ({ history: historyItems }) };
    window.debounce = (fn) => fn;
    window.iconize = (s) => s;
    window.showToast = () => {};
    window.skillOriginBadge = () => '';
    // app.js 依赖 window.Linkora 作为动作注册表（runAction 通过 L.actions / window[action] 找处理器）
    window.Linkora = window.Linkora || { actions: {}, register: (n, f) => { window.Linkora.actions[n] = f; return f; } };

    // 按浏览器加载顺序加载真实源码：core/util.js → pages/simulate.js → core/app.js
    loadRealScript('core/util.js');
    loadRealScript('pages/simulate.js');
    try { loadRealScript('core/app.js'); } catch (_) { /* init 块缺其他页面全局，预期 */ }
  });

  it('渲染的 data-args 必须是可解析 JSON，且参数值与原始内容完全一致', async () => {
    await window.renderSimHistory(historyItems);

    const item = document.querySelector('.sim-history-item');
    expect(item).toBeTruthy();

    const raw = item.getAttribute('data-args');
    // 关键断言：不能出现裸换行 / 裸引号破坏结构
    expect(() => JSON.parse(raw)).not.toThrow();

    const args = JSON.parse(raw);
    expect(args).toHaveLength(2);
    expect(args[0]).toBe(NASTY_CONTENT);
    expect(args[1]).toBe(NASTY_SENDER);
  });

  it('点击历史记录 → 内容与发送人回填到输入框（不是 undefined）', async () => {
    await window.renderSimHistory(historyItems);

    const item = document.querySelector('.sim-history-item');
    item.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await new Promise((r) => setTimeout(r, 20));

    expect(document.getElementById('simulate-content').value).toBe(NASTY_CONTENT);
    expect(document.getElementById('simulate-sender').value).toBe(NASTY_SENDER);
    // 回归点：旧实现这里会是字面量 "undefined"
    expect(document.getElementById('simulate-content').value).not.toBe('undefined');
    expect(document.getElementById('simulate-sender').value).not.toBe('undefined');
  });

  it('jsonAttr 对各种载荷都产出可解析且可还原的 JSON', () => {
    const cases = [
      ['纯文本', 'hello'],
      ['含双引号', '他说"你好"'],
      ["含单引号", "it's fine"],
      ['含真实换行', '第一行\n第二行'],
      ['含反斜杠', '路径 C:\\temp\\x'],
      ['含 & 实体字符', 'a & b'],
      ['含尖括号', '<script>x</script>'],
      ['空字符串', ''],
      ['含 emoji', '🎯 目标'],
    ];
    for (const [label, val] of cases) {
      const attr = window.jsonAttr([val, '张三']);
      // 浏览器会还原 HTML 实体 → 还原后必须是合法 JSON
      const decoded = attr
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
      let parsed;
      expect(() => { parsed = JSON.parse(decoded); }, label).not.toThrow();
      expect(parsed[0], label).toBe(val);
      expect(parsed[1], label).toBe('张三');
    }
  });
});
