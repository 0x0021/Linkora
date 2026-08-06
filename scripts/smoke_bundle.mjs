// scripts/smoke_bundle.mjs
// 构建产物冒烟测试：在 jsdom 中求值打包后的 bundle.js，验证关键全局函数/对象已挂载、
// 且无「重复声明 / 解析错误」。这是对「合并等价于多 script 加载」的实证校验。
//
// 运行：node scripts/smoke_bundle.mjs

import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const manifest = JSON.parse(readFileSync(join(ROOT, 'web/static/dist/manifest.json'), 'utf8'));
const jsCode = readFileSync(join(ROOT, 'web/static/dist', manifest.js), 'utf8');

const dom = new JSDOM('<!DOCTYPE html><html><head></head><body><div id="toast"></div></body></html>', {
  url: 'http://localhost/',
  pretendToBeVisual: true,
  runScripts: 'outside-only',
});
const { window } = dom;

// 阻止 app.js 的 DOMContentLoaded 初始化路径（init/checkWebAuth 等会触发 fetch 与 DOM 查询），
// 冒烟只需验证「顶层函数声明 + window.X 桥接」已同步挂载全局符号，无需真实初始化。
window.document.addEventListener = () => {};
window.addEventListener = () => {};

// 补齐 CDN/外部全局 stub（仅避免极个别顶-level 引用；实际均在函数内使用）
window.Chart = function () {};
window.bootstrap = { Modal: function () {}, Tooltip: function () {} };
// jsdom 无 fetch；补 stub 让顶层 refreshImageToken / initPlatformSwitcher 路径可正常走完
// （真实浏览器有 fetch；返回完整 Response 形态，headers 标 application/json 走 json 分支）
window.fetch = async () => ({
  ok: true,
  status: 200,
  headers: { get: () => 'application/json' },
  json: async () => ({ token: 't', exp: 9999999999, platforms: [], success: true }),
  text: async () => '',
});
// 极简 DOM 缺业务元素；补假元素 stub，避免 checkWebAuth/updateWebUserInfo 等顶层调用
// 触碰 null.style 而抛错（真实页面元素齐全，此处仅为冒烟环境兜底）
window.document.getElementById = (id) => {
  const el = {
    id,
    textContent: '',
    innerHTML: '',
    value: '',
    checked: false,
    disabled: false,
    className: '',
    title: '',
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    setAttribute() {},
    getAttribute: () => null,
    removeAttribute() {},
    appendChild() {},
    remove() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    getContext: () => ({}),
  };
  return el;
};
window.document.querySelectorAll = () => [];
window.document.querySelector = () => null;

let evalError = null;
// 求值期间临时静默 bundle 内部的 console.error（fetch 失败等容错日志），保持 CI 输出干净
const _origErr = window.console.error;
window.console.error = () => {};
try {
  window.eval(jsCode);
} catch (e) {
  evalError = e;
} finally {
  window.console.error = _origErr;
}

if (evalError) {
  console.error('[smoke] bundle 求值抛错：', evalError && evalError.stack ? evalError.stack : evalError);
  process.exit(1);
}

// 合并前由各经典 script 暴露到全局的关键符号（函数声明 / window.X= 桥接）
// 注：drafts.js 为 type=module 未并入 bundle，其 loadDraftsPage 等不在此断言。
const required = [
  'escapeHtml', 'setText', 'formatTime',
  'api', 'store',
  'switchPage', 'loadDashboard', 'loadMessages',
  'renderKpiCard', 'showToast', 'init',
  'startCostQualityPolling', 'stopCostQualityPolling',
];
const missing = required.filter((n) => typeof window[n] === 'undefined');
if (missing.length) {
  console.error('[smoke] 缺失全局符号：', missing.join(', '));
  process.exit(1);
}
console.log('[smoke] OK — bundle 求值无错，关键全局符号均存在（' + required.length + ' 项）');
// jsdom / 遗留定时器会保活事件循环，显式退出确保 CI 干净收尾
process.exit(0);
