// 前端冒烟测试公共 setup（P1-3）
// 页面脚本是经典 <script>，依赖一批「全局」：api / escapeHtml / renderKpiCard / showToast /
// ChartCard / Chart / switchPage / confirm 等（正常由 app.js / components / CDN 注入）。
// 在 jsdom 下这些不存在，这里统一补齐最小 stub，让脚本能被 import 并执行而不崩。
import { vi } from 'vitest';

// ---- 通用「万能元素」stub：任何 getElementById / createElement 都返回一个安全的假元素 ----
// 这样 render* 函数即使找不到真实节点也不会抛（冒烟测试只关心「整条渲染链路不崩」，不校验 DOM 内容）。
function makeEl() {
  const el = {
    textContent: '',
    innerHTML: '',
    value: '',
    checked: false,
    disabled: false,
    className: '',
    title: '',
    style: {},
    dataset: {},
    children: [],
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    appendChild() {},
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    addEventListener() {},
    getContext() { return {}; },
  };
  return el;
}

if (typeof globalThis.document !== 'undefined') {
  globalThis.document.getElementById = () => makeEl();
  globalThis.document.createElement = () => makeEl();
  globalThis.document.querySelectorAll = () => [];
  globalThis.document.querySelector = () => null;
}

// ---- 注入 app.js / components 提供的全局 ----
globalThis.escapeHtml = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

globalThis.formatTime = (s) => (s ? String(s) : '—');
globalThis.showToast = vi.fn();
globalThis.renderKpiCard = vi.fn();
globalThis.switchPage = vi.fn();
globalThis.confirm = vi.fn(() => true);
globalThis.alert = vi.fn();

// 图表（仅 metrics.js 的绘制函数用到，冒烟不校验图形）
globalThis.Chart = vi.fn();
globalThis.ChartCard = {
  showEmpty: vi.fn(),
  ensureCanvas: vi.fn(() => ({ canvas: makeEl() })),
};

// 后端 API 客户端：默认返回一个「成功但空」的响应；各测试可按需覆盖 api.fetch 实现
globalThis.api = {
  fetch: vi.fn(async () => ({ error: null })),
  rechunkAllDocs: vi.fn(async () => ({ success: true })),
};
