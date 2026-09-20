#!/usr/bin/env node
/**
 * 运行时冒烟：用 jsdom 加载**真实生产 HTML + bundle**，断言"页面能正常起步"。
 *
 * 为什么需要它：`smoke_bundle.mjs` 只断言产物里"有这些符号"，`vitest` 只测单函数。
 * 两者都抓不到「加载期运行时崩溃」——例如命名空间被先执行的页面桥接短路成残缺对象
 * 导致 register() 抛错、IIFE 中断、事件委托没挂上（全站按钮失效），或 defer 单脚本
 * 里 init() 同步执行撞上后续文件顶层 let 的 TDZ。这类错误只有真正执行才暴露。
 *
 * 断言：
 *   1. 加载期无 jsdomError（module 脚本相关的 jsdom 自身限制白名单除外）
 *   2. window.Linkora 存在，且 actions 是对象、pager-go 已注册（= register 未崩）
 *   3. document 级点击委托可用：点击一个 data-action 按钮能分发到处理器
 *   4. window.switchPage 已暴露
 *
 * 退出码：0 通过；1 失败（CI 可当门禁）。
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const { JSDOM, ResourceLoader, VirtualConsole } = require(
  path.join(ROOT, 'node_modules', 'jsdom', 'lib', 'api.js')
);

const DIST = path.join(ROOT, 'web', 'static', 'dist');
const manifest = JSON.parse(fs.readFileSync(path.join(DIST, 'manifest.json'), 'utf8'));

// ---- 用真实模板 + manifest 合成静态 HTML ----
// 生产模式只加载单个 bundle（`{% if bundle_js_v %}` 分支）；模板里的 dev 分支
// （36 个独立 /static/js/*.js）必须剔除，否则同一份 app.js 会被执行两次，
// 且 drafts.js 是 module（jsdom 不执行）会引入假阳性。
let html = fs.readFileSync(path.join(ROOT, 'web', 'templates', 'index.html'), 'utf8');
html = html.replace(/\{%[\s\S]*?%\}/g, '');
html = html.replace(/^\s*<script[^>]*src="\/static\/js\/[\s\S]*?<\/script>\s*$/gm, '');
html = html.replace(/\{\{\s*bundle_js_v\s*\}\}/g, manifest.js);
html = html.replace(/\{\{\s*bundle_css_v\s*\}\}/g, manifest.css);
html = html.replace(/\{\{[\s\S]*?\}\}/g, '');

// 加载期异常（jsdom 中某些未捕获错误会直接冒泡到 Node）也计为运行时错误
const escaped = [];
process.on('uncaughtException', (e) => {
  escaped.push(e && e.stack ? e.stack : String(e));
});

class DiskLoader extends ResourceLoader {
  fetch(url) {
    const p = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    if (p.startsWith('/static/dist/')) {
      // HTML 可能引用已被构建清理的旧 hash 产物 → 回退到 manifest 指向的最新产物
      const direct = path.join(ROOT, 'web', p);
      const fallback = path.join(DIST, p.endsWith('.css') ? manifest.css : manifest.js);
      const f = fs.existsSync(direct) ? direct : fallback;
      return Promise.resolve(fs.existsSync(f) ? fs.readFileSync(f) : Buffer.from(''));
    }
    if (p.startsWith('/static/js/')) {
      const f = path.join(ROOT, 'web', p);
      return Promise.resolve(fs.existsSync(f) ? fs.readFileSync(f) : Buffer.from(''));
    }
    return Promise.resolve(Buffer.from(''));
  }
}

const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => {
  const detail = e.detail && e.detail.stack ? e.detail.stack : e.message || String(e);
  errors.push(detail);
});
vc.on('error', () => {});
vc.on('warn', () => {});
vc.on('log', () => {});
vc.on('info', () => {});

// jsdom 自身的限制（非产品缺陷），不计入失败
const BENIGN = [
  'loadDraftsPage is not defined',      // drafts.js 是 type=module，jsdom 不执行模块
  'Cannot use import statement outside a module',
  'Not implemented: HTMLCanvasElement',
];

const dom = new JSDOM(html, {
  url: 'http://127.0.0.1:8080/',
  runScripts: 'dangerously',
  resources: new DiskLoader(),
  virtualConsole: vc,
  pretendToBeVisual: true,
});
const w = dom.window;
w.matchMedia = w.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
w.HTMLCanvasElement.prototype.getContext = () => null;
w.fetch = () => Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
try { w.localStorage.clear(); w.sessionStorage.clear(); } catch { /* ignore */ }

const failures = [];
const check = (ok, label, extra = '') => {
  console.log(`${ok ? '  ✓' : '  ✗'} ${label}${extra ? ' — ' + extra : ''}`);
  if (!ok) failures.push(label);
};

await new Promise((r) => setTimeout(r, 2000));

console.log(`[runtime] bundle = ${manifest.js}`);

const realErrors = errors.concat(escaped).filter((e) => !BENIGN.some((b) => e.includes(b)));

// 断言区整体兜底：即使某个断言自身因残缺状态抛错，也要走到结论输出并以非零码退出
try {
  check(realErrors.length === 0, '加载期无运行时错误',
    realErrors.length ? realErrors[0].split('\n').slice(0, 2).join(' | ') : '');

  const L = w.Linkora;
  check(!!L && typeof L === 'object', 'window.Linkora 存在');
  check(!!L && !!L.actions && typeof L.actions === 'object', 'Linkora.actions 已建立（register 未崩）',
    L && L.actions ? '已注册: ' + Object.keys(L.actions).join(',') : 'actions 缺失');
  check(!!L && !!L.actions && typeof L.actions['pager-go'] === 'function', 'pager-go 处理器已注册');
  check(typeof w.switchPage === 'function', 'window.switchPage 已暴露');

  // 事件委托端到端：造一个 data-action 按钮，点击应分发到 window.Linkora.actions
  let hit = 0;
  if (L && L.actions && typeof L.actions === 'object') {
    L.actions.__smoke_probe = () => { hit++; };
    const btn = w.document.createElement('button');
    btn.setAttribute('data-action', '__smoke_probe');
    btn.setAttribute('data-args', '[]');
    w.document.body.appendChild(btn);
    btn.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
    check(hit === 1, 'document 级点击委托可用（data-action 分发成功）');
  } else {
    check(false, 'document 级点击委托可用（data-action 分发成功）', 'actions 缺失，跳过探测');
  }
} catch (e) {
  failures.push('断言过程异常: ' + (e && e.message ? e.message : e));
  console.error('[runtime] 断言异常:', e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n') : e);
}

try { dom.window.close(); } catch { /* ignore */ }

if (realErrors.length) {
  console.log('\n--- 运行时错误明细 ---');
  realErrors.slice(0, 5).forEach((e) => console.log(e + '\n'));
}
if (failures.length) {
  console.error(`\n[runtime] FAIL — ${failures.length} 项未通过: ${failures.join('；')}`);
  process.exit(1);
}
console.log('[runtime] OK — 页面可正常起步，命名空间与事件委托均就绪');
process.exit(0);
