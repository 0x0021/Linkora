#!/usr/bin/env node
/**
 * 运行时冒烟：用 jsdom 加载**真实生产 HTML + bundle**，推断「页面能正常起步 + 全站页面能切换」。
 *
 * 为什么需要它：`smoke_bundle.mjs` 只断言产物里"有这些符号"，`vitest` 只测单函数。
 * 两者都抓不到「加载期 / 切换页面时的运行时崩溃」——例如命名空间被先执行的页面桥接
 * 短路成残缺对象导致 register() 抛错、IIFE 中断、事件委托没挂上（全站按钮失效），
 * 或 defer 单脚本里 init() 同步执行撞上后续文件顶层 let 的 TDZ。这类错误只有真正执行才暴露。
 *
 * 它同时是本机「无 GUI 浏览器」约束下，对**手动全页冒烟**的自动化替代：
 * 逐个 switchPage() 走完 17 个页面，任何一页在加载/渲染期抛错都会让 CI 变红；
 * 并进一步真的**点击**迁移后的真实控件（tab / 弹窗 / 全选），覆盖交互路径。
 *
 * 断言：
 *   A. 起步：加载期无运行时错误 / window.Linkora.actions 已建立（= register 未崩）
 *          / pager-go 已注册 / switchPage 已暴露 / document 级点击委托端到端可用
 *   B. init() 完整性：init() 暴露的全部全局都是 function（若 init() 中途抛错会缺失，
 *          例如历史缺陷：jsdom 不执行 module 脚本 → window.loadDraftsPage 未定义 →
 *          init() 在赋值处中断 → 后续 window.loadDashboard/loadMessages 全部缺失，
 *          护栏退化成"在半个 app 上断言"）
 *   C. 页面遍历：17 个页面逐个切换，页容器须激活，且不得产生新的运行时错误
 *   D. 交互冒烟（真点迁移后的控件）：
 *          D1 tab 按钮 data-args 与自身 data-tab 一致 + 点击后自激活（抓参数错配）
 *          D2 **全部**弹窗均须含 [data-action^="close"] 关闭钮且带 aria-label
 *             （前一条是硬契约：名字不以 close 开头会退化成"只摘 .active 的 fallback 关闭"，
 *              不执行弹窗自身的关闭逻辑；后一条是选择器迁移是否生效的精确探针）
 *          D3 同步中心「点入口打开 → Esc 关闭」真实往返 + 全站弹窗 Esc 关闭
 *          D4 全站弹窗关闭钮直点可关闭
 *          D5 全选复选框：@el 间接引用生效且未被 preventDefault 回滚
 *          D6 防内联回流：模板内联 onclick 仅允许 1 处已知（灯箱背景关闭）
 *          D7 triggerClick 通用动作 + role=button 元素的 Enter/Space 键盘可达性
 *          D8 <a href="#"> 的默认 # 跳转被阻止（等价旧内联的 return false）
 *          D9 图片 404 降级：/api/image/* 破图替换为 .img-unavailable 占位（幂等、
 *             连 .chat-image-wrap 一并替换），外链图片与灯箱共享节点不得被改写
 *
 * jsdom 已知限制（已在下方 stub 掉，不算产品缺陷）：
 *   - 不执行 <script type="module">（drafts.js 是 module，故预置 no-op loadDraftsPage
 *     与 closeDraftEditModal 替身）
 *   - 无 canvas 2D 实现（getContext 返回替身；Chart.js 亦替身，不测图表绘制）
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
// （36 个独立 /static/js/*.js）必须剔除，否则同一份 app.js 会被执行两次。
// 这一步同时剔除了 `type="module"` 的 drafts.js（jsdom 不执行模块，见 beforeParse 注释）。
let html = fs.readFileSync(path.join(ROOT, 'web', 'templates', 'index.html'), 'utf8');
html = html.replace(/\{%[\s\S]*?%\}/g, '');
html = html.replace(/^\s*<script[^>]*src="\/static\/js\/[\s\S]*?<\/script>\s*$/gm, '');
html = html.replace(/\{\{\s*bundle_js_v\s*\}\}/g, manifest.js);
html = html.replace(/\{\{\s*bundle_css_v\s*\}\}/g, manifest.css);
html = html.replace(/\{\{[\s\S]*?\}\}/g, '');

// 加载期 / 页面切换期异常（未捕获错误与未处理的 Promise rejection）都计为运行时错误
const escaped = [];
process.on('uncaughtException', (e) => {
  escaped.push(e && e.stack ? e.stack : String(e));
});
process.on('unhandledRejection', (e) => {
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
    if (p.startsWith('/static/js/') || p.startsWith('/static/vendor/')) {
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

// jsdom 自身的限制 / 无关噪声（非产品缺陷），不计入失败
const BENIGN = [
  'Not implemented: HTMLCanvasElement',
  'Not implemented: window.scrollTo',
  'Not implemented: HTMLFormElement.prototype.submit',
  'Could not parse CSS stylesheet',
  'Could not load img', // jsdom 无图片解码；img.onerror 属页面自身处理范围
];

// canvas 2D 上下文替身：页面代码普遍写 `ctx.canvas` / 调 2D API，jsdom 默认返回 null 会炸
function ctx2d(canvas) {
  const noop = () => {};
  const grad = { addColorStop: noop };
  return {
    canvas,
    save: noop, restore: noop, scale: noop, rotate: noop, translate: noop, transform: noop,
    setTransform: noop, resetTransform: noop, clearRect: noop, fillRect: noop, strokeRect: noop,
    beginPath: noop, closePath: noop, moveTo: noop, lineTo: noop, bezierCurveTo: noop,
    quadraticCurveTo: noop, arc: noop, arcTo: noop, rect: noop, ellipse: noop,
    fill: noop, stroke: noop, clip: noop, isPointInPath: () => false,
    fillText: noop, strokeText: noop, drawImage: noop, putImageData: noop,
    measureText: () => ({ width: 0, actualBoundingBoxAscent: 0, actualBoundingBoxDescent: 0 }),
    createImageData: () => ({ data: [] }),
    getImageData: () => ({ data: [] }),
    createLinearGradient: () => grad,
    createRadialGradient: () => grad,
    createPattern: () => null,
    setLineDash: noop,
    getLineDash: () => [],
  };
}

const EMPTY_RES = { ok: true, status: 200, headers: { get: () => 'application/json' } };

const dom = new JSDOM(html, {
  url: 'http://127.0.0.1:8080/',
  runScripts: 'dangerously',
  resources: new DiskLoader(),
  virtualConsole: vc,
  pretendToBeVisual: true,
  // 所有替身必须在**任何脚本执行前**就位（beforeParse），否则 bundle 求值期就会踩空
  beforeParse(w) {
    // ① jsdom 不执行 type=module，而 drafts.js 是 module（真实浏览器里它挂 window.loadDraftsPage）。
    //    若不预置，app.js 的 init() 会在 `window.loadDraftsPage = loadDraftsPage` 处抛
    //    ReferenceError 并**中断整个 init()** → 其后半段的 window.loadDashboard /
    //    loadMessages / syncHistory 等全部不暴露，护栏就退化成"在半个 app 上断言"。
    w.loadDraftsPage = () => {};

    // ①b 同上（drafts.js 提供的弹窗关闭动作）。按其真实实现（pages/drafts.js:closeDraftEditModal）
    //    等价替代：摘掉 .active 并重置编辑态。若不预置，draft-edit-modal 的关闭钮会走到
    //    委托的「未找到 action 处理器」分支 → 弹窗关不掉，D2/D3/D4 断言会（正确地）失败。
    //    断言的链路（a11y 查 [data-action^=close] → 委托分发 → 弹窗关闭）全是真的，
    //    只有最终关闭函数的实现体是替身——这是 jsdom 不执行 module 的既有妥协，非护栏放水。
    w.closeDraftEditModal = () => {
      const m = w.document.getElementById('draft-edit-modal');
      if (m) m.classList.remove('active');
    };

    // ② 浏览器能力替身
    w.matchMedia = w.matchMedia || (() => ({
      matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
    }));
    w.ResizeObserver = w.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
    w.IntersectionObserver = w.IntersectionObserver || class {
      observe() {} unobserve() {} disconnect() {} takeRecords() { return []; }
    };
    if (w.HTMLCanvasElement) {
      w.HTMLCanvasElement.prototype.getContext = function () { return ctx2d(this); };
    }

    // ③ Chart.js 替身：jsdom 无真实绘制能力，加载 205KB 真实库只会引入不确定性。
    //    loadChart() 见到 window.Chart 已存在即直接返回，不会去拉 vendor。
    //    代价：不验证图表绘制正确性（那属于人工/视觉回归范畴），只验证页面代码路径
    //    能跑通且图表实例能被正常创建与 destroy（切页不清会内存泄漏）。
    w.Chart = function ChartStub() {
      return { destroy() {}, update() {}, resize() {}, data: { datasets: [] }, options: {} };
    };

    // ④ API 替身：统一 200 + 空 JSON（= "服务在，但没数据"）。
    //    页面必须能容忍空数据（真实 API 也会返回空集合），若因此抛错即为真实缺陷。
    //    /api/status 与 /api/platforms 给出最小可用形状，让应用走"已登录"启动路径。
    const res = (body) => ({
      ...EMPTY_RES,
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
    });
    w.fetch = (url) => {
      const u = typeof url === 'string' ? url : (url && url.url) || '';
      if (u.includes('/api/status')) {
        return Promise.resolve(res({ status: 'running', user: { name: 'smoke' } }));
      }
      if (u.includes('/api/platforms')) {
        return Promise.resolve(res({
          platforms: [{ id: 'dingtalk', display_name: '钉钉', enabled: true, adapter_type: 'dingtalk' }],
        }));
      }
      return Promise.resolve(res({}));
    };

    // ⑤ 让应用走"已登录"分支（isAuthenticated() 读 localStorage.web_auth）
    try {
      w.localStorage.clear();
      w.sessionStorage.clear();
      w.localStorage.setItem('web_auth', 'c21va2U6cHc='); // btoa('smoke:pw')
    } catch { /* ignore */ }
  },
});

const w = dom.window;

const failures = [];
const check = (ok, label, extra = '') => {
  console.log(`${ok ? '  ✓' : '  ✗'} ${label}${extra ? ' — ' + extra : ''}`);
  if (!ok) failures.push(label);
};

await new Promise((r) => setTimeout(r, 2000));

console.log(`[runtime] bundle = ${manifest.js}`);

const allErrors = () => errors.concat(escaped);
const realErrors = () => allErrors().filter((e) => !BENIGN.some((b) => e.includes(b)));

// —— 交互冒烟用的小工具 ——
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 用真实鼠标事件点击：会经过 document 级事件委托，等价于用户手点
const click = (el) => el.dispatchEvent(new w.MouseEvent('click', { bubbles: true, cancelable: true }));
// 自 `before` 起新增的「真实」错误（滤掉 jsdom 限制类噪声）
const freshErrors = (before) => allErrors().slice(before).filter((e) => !BENIGN.some((b) => e.includes(b)));
const pressEsc = () => w.document.dispatchEvent(
  new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
);

// init() 应当暴露的全部全局（见 app.js::init() 尾部）
const INIT_GLOBALS = [
  'switchPage', 'switchPlatform', 'initPlatformSwitcher',
  'loadSkillsPage', 'loadDeadLettersPage', 'loadDraftsPage', 'debouncedLoadDraftsPage',
  'loadDashboard', 'loadDashboardData', 'loadMessages', 'syncHistory', 'selectMessageConversation',
];

// 期望的页面集合（与 app.js::switchPage 的 titles 表一致）
const EXPECTED_PAGES = [
  'dashboard', 'keywords', 'rag', 'messages', 'intent', 'skills', 'tools', 'deadletters',
  'drafts', 'config', 'persona', 'metrics', 'models', 'cost-quality', 'logs', 'simulate', 'summaries',
];

// ---- A. 起步断言 ----
// 断言区整体兜底：即使某个断言自身因残缺状态抛错，也要走到结论输出并以非零码退出
try {
  const boot = realErrors();
  check(boot.length === 0, '加载期无运行时错误',
    boot.length ? boot[0].split('\n').slice(0, 2).join(' | ') : '');

  const L = w.Linkora;
  check(!!L && typeof L === 'object', 'window.Linkora 存在');
  check(!!L && !!L.actions && typeof L.actions === 'object', 'Linkora.actions 已建立（register 未崩）',
    L && L.actions ? '已注册: ' + Object.keys(L.actions).join(',') : 'actions 缺失');
  check(!!L && !!L.actions && typeof L.actions['pager-go'] === 'function', 'pager-go 处理器已注册');
  check(typeof w.switchPage === 'function', 'window.switchPage 已暴露');

  // ---- B. init() 完整性 ----
  const missing = INIT_GLOBALS.filter((k) => typeof w[k] !== 'function');
  check(missing.length === 0, 'init() 完整执行（其暴露的全局均存在）',
    missing.length ? '缺失: ' + missing.join(',') : `${INIT_GLOBALS.length} 项`);

  // ---- A2. 事件委托端到端 ----
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
  failures.push('起步断言异常: ' + (e && e.message ? e.message : e));
  console.error('[runtime] 起步断言异常:', e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n') : e);
}

// ---- C. 页面遍历（本机"手点全页冒烟"的自动化替代）----
try {
  // 以模板里的真实导航入口为准，避免本脚本的页面清单随源码漂移。
  // 入口有两类：侧边栏 `.nav-item`（如 dashboard/messages）与头像下拉里的
  // `.user-dropdown-item`（如 config），二者都是 <a data-page="...">。
  const navPages = Array.from(
    new Set(
      Array.from(w.document.querySelectorAll('a[data-page], .nav-item[data-page]'))
        .map((el) => el.dataset.page)
        .filter(Boolean)
    )
  );

  const sameSet = navPages.length === EXPECTED_PAGES.length
    && EXPECTED_PAGES.every((p) => navPages.includes(p));
  check(sameSet, '导航页面集合与预期一致',
    sameSet ? `${navPages.length} 页` : `DOM=${navPages.length} 页 [${navPages.join(',')}]`);

  // switchPage 在 currentPage === page 时直接 return；初始页是 dashboard，故它排到最后
  const order = EXPECTED_PAGES.filter((p) => p !== 'dashboard').concat('dashboard');
  const pageFailures = [];

  for (const page of order) {
    const before = allErrors().length;
    let threw = null;
    try {
      w.switchPage(page);
    } catch (e) {
      threw = e && e.stack ? e.stack.split('\n').slice(0, 2).join(' | ') : String(e);
    }
    // 等异步 load 函数（fetch → 渲染）跑完
    await new Promise((r) => setTimeout(r, 150));

    const pageEl = w.document.getElementById(`page-${page}`);
    const activated = !!pageEl && pageEl.classList.contains('active');
    const fresh = allErrors().slice(before).filter((e) => !BENIGN.some((b) => e.includes(b)));

    if (threw || !activated || fresh.length) {
      pageFailures.push(page);
      console.log(`  ✗ 页面 ${page}`
        + (threw ? ` — 同步抛错: ${threw}` : '')
        + (!activated ? ' — 页容器未激活' : '')
        + (fresh.length ? ` — 运行时错误: ${fresh[0].split('\n').slice(0, 2).join(' | ')}` : ''));
    } else {
      console.log(`  ✓ 页面 ${page}`);
    }
  }

  check(pageFailures.length === 0, `全部 ${order.length} 个页面可正常切换与加载`,
    pageFailures.length ? `失败: ${pageFailures.join(',')}` : '');
} catch (e) {
  failures.push('页面遍历异常: ' + (e && e.message ? e.message : e));
  console.error('[runtime] 页面遍历异常:', e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n') : e);
}

// ---- D. 交互冒烟：真点 Phase 0 迁移后的真实控件 ----
// 为什么需要：Phase 0 把 151 处内联 onclick 迁成 data-action，但此前的护栏只点过一个
// 「合成」按钮，从未点过模板里真实的 tab / 弹窗关闭钮 / 全选复选框。这类迁移一旦把参数
// 配错、或 a11y 的 [data-action^="close"] 选择器失效，就是「构建绿 + 单测绿 + 页面照崩」。
try {
  // D1. tab 按钮：data-args 必须与元素自身 data-tab/data-sub 一致，且点击后自身激活。
  //     若迁移时 args 错配（A 按钮带了 B 的参数），激活的会是另一个 tab，断言立刻失败。
  const TAB_GROUPS = [
    { name: 'rag', sel: '#page-rag .section-tab', key: 'tab', min: 6 },
    { name: 'keywords', sel: '#page-keywords .section-tab', key: 'tab', min: 2 },
    { name: 'intent', sel: '.intent-tab-btn', key: 'tab', min: 3 },
    { name: 'skills-sub', sel: '.skill-subnav-btn', key: 'sub', min: 2 },
    { name: 'market', sel: '#marketplace-tabs .market-tab', key: 'tab', min: 6 },
  ];
  let tabCount = 0;
  const tabProblems = [];
  for (const g of TAB_GROUPS) {
    const els = Array.from(w.document.querySelectorAll(g.sel));
    if (els.length < g.min) tabProblems.push(`${g.name} 组元素数 ${els.length} < 预期下限 ${g.min}`);
    for (const el of els) {
      tabCount++;
      const key = el.dataset[g.key];
      let args = null;
      try { args = el.dataset.args ? JSON.parse(el.dataset.args) : null; } catch { /* 视为不一致 */ }
      const label = `${g.name}/${key}`;
      if (!el.dataset.action || !Array.isArray(args) || args[0] !== key) {
        tabProblems.push(`${label} data-args 与自身 data-${g.key} 不一致`);
        continue;
      }
      const before = allErrors().length;
      let threw = null;
      try { click(el); } catch (e) { threw = e && e.message ? e.message : String(e); }
      await sleep(30);
      const errs = freshErrors(before);
      if (threw) tabProblems.push(`${label} 点击抛错: ${threw}`);
      else if (!el.classList.contains('active')) tabProblems.push(`${label} 点击后未激活（args 可能错配）`);
      else if (errs.length) tabProblems.push(`${label} 运行时错误: ${errs[0].split('\n')[0]}`);
    }
  }
  check(tabCount >= 19 && tabProblems.length === 0,
    `全部 ${tabCount} 个 tab 按钮：参数与自身 data-tab 一致且点击后自激活`,
    tabProblems.length ? tabProblems.slice(0, 3).join('；') : '');

  // D2. 弹窗 a11y 契约：**每个**弹窗都必须有 [data-action^="close"] 关闭钮，且该钮带 aria-label。
  //     前一条是硬契约——app.js 的 a11y 层靠这个选择器挂 role/aria-modal、补 aria-label，
  //     并在 Esc / 遮罩点击时点它。名字不以 close 开头（如历史遗留的 _draftCloseEditModal）
  //     会让该弹窗退化成"只被摘掉 .active 的 fallback 关闭"，不执行自身的关闭逻辑。
  //     后一条是「a11y 选择器由 [onclick^=close] 迁到 [data-action^=close] 是否生效」的精确探针。
  const modals = Array.from(w.document.querySelectorAll('.modal'));
  const noClose = [];
  const unlabelled = [];
  for (const m of modals) {
    const btn = m.querySelector('[data-action^="close"]');
    const id = m.id || '(无 id)';
    if (!btn) { noClose.push(id); continue; }
    if (!btn.getAttribute('aria-label')) unlabelled.push(id);
  }
  check(modals.length >= 19, `.modal 数量符合预期（${modals.length}）`);
  check(noClose.length === 0,
    `全部 ${modals.length} 个弹窗均含 [data-action^="close"] 关闭钮`,
    noClose.length ? `缺: ${noClose.join(',')}` : '');
  check(unlabelled.length === 0,
    `全部 ${modals.length} 个弹窗关闭钮均带 aria-label（a11y 选择器迁移生效）`,
    unlabelled.length ? `未标注: ${unlabelled.join(',')}` : '');

  // D3a. 真实往返：点「同步中心」入口打开 → Esc 关闭。
  //      走的是用户真实路径，并能抓住「弹窗用 style.display 而非 .active 驱动」这类缺陷
  //      （a11y 的 topModal() 只看 .active，display 驱动的弹窗 Esc 关不掉）。
  const syncEntry = w.document.querySelector('[data-action="openSyncCenter"]');
  if (!syncEntry) {
    check(false, '同步中心入口按钮存在（openSyncCenter）');
  } else {
    const syncModal = w.document.getElementById('sync-center-modal');
    const before = allErrors().length;
    let threw = null;
    try { click(syncEntry); } catch (e) { threw = e && e.message ? e.message : String(e); }
    await sleep(60);
    const opened = !!syncModal && syncModal.classList.contains('active');
    try { pressEsc(); } catch { /* 下面统一判定 */ }
    await sleep(30);
    const closed = !!syncModal && !syncModal.classList.contains('active');
    const errs = freshErrors(before);
    check(!threw && opened && closed && errs.length === 0,
      '同步中心：点入口打开 → Esc 关闭（真实路径往返）',
      [!threw ? '' : '点击抛错:' + threw, opened ? '' : '点后未打开', closed ? '' : 'Esc 未关闭',
        errs.length ? '错误:' + errs[0].split('\n')[0] : ''].filter(Boolean).join('；'));
  }

  // D3b. 逐个弹窗：置为 active → Esc 应关闭（验证 closeModal 能找到并点到关闭钮）
  const a11yModals = Array.from(w.document.querySelectorAll('.modal:not(.image-lightbox)'));
  const escProblems = [];
  for (const m of a11yModals) {
    m.classList.add('active');
    const before = allErrors().length;
    let threw = null;
    try { pressEsc(); } catch (e) { threw = e && e.message ? e.message : String(e); }
    await sleep(20);
    const errs = freshErrors(before);
    if (threw || m.classList.contains('active') || errs.length) {
      escProblems.push(`${m.id}${threw ? ' 抛错:' + threw : ''}`
        + `${m.classList.contains('active') ? ' 未关闭' : ''}`
        + `${errs.length ? ' 错误:' + errs[0].split('\n')[0] : ''}`);
    }
    m.classList.remove('active');
  }
  check(escProblems.length === 0, `全部 ${a11yModals.length} 个弹窗可按 Esc 关闭`,
    escProblems.length ? escProblems.slice(0, 3).join('；') : '');

  // D4. 逐个弹窗：点关闭钮应关闭（真实用户操作路径）
  const clickProblems = [];
  let clickCount = 0;
  for (const m of modals) {
    const btn = m.querySelector('[data-action^="close"]');
    if (!btn) continue;
    clickCount++;
    m.classList.add('active');
    const before = allErrors().length;
    let threw = null;
    try { click(btn); } catch (e) { threw = e && e.message ? e.message : String(e); }
    await sleep(20);
    const errs = freshErrors(before);
    if (threw || m.classList.contains('active') || errs.length) {
      clickProblems.push(`${m.id}${threw ? ' 抛错:' + threw : ''}`
        + `${m.classList.contains('active') ? ' 未关闭' : ''}`
        + `${errs.length ? ' 错误:' + errs[0].split('\n')[0] : ''}`);
    }
    m.classList.remove('active');
    m.style.display = '';
  }
  check(clickCount === modals.length && clickProblems.length === 0,
    `全部 ${clickCount} 个弹窗关闭钮可关闭`,
    clickProblems.length ? clickProblems.slice(0, 3).join('；') : '');

  // D5. 全选复选框：验证 @el 间接引用（data-args:["@el"] → 被点元素）与「委托不 preventDefault」。
  //     委托若 preventDefault，复选框的原生勾选会被回滚 → 留下「行被勾上、全选框自己弹回未勾」
  //     的不一致状态。此断言正是当初那个决策（不 preventDefault）的守卫。
  const master = w.document.querySelector('[data-action="toggleAllKwSelect"]');
  if (!master) {
    check(false, '全选复选框存在（toggleAllKwSelect）');
  } else {
    const host = w.document.createElement('div');
    for (const id of ['9001', '9002']) {
      const cb = w.document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'kw-checkbox';
      cb.dataset.id = id;
      host.appendChild(cb);
    }
    w.document.body.appendChild(host);
    master.checked = false;
    const before = allErrors().length;
    let threw = null;
    try { click(master); } catch (e) { threw = e && e.message ? e.message : String(e); }
    await sleep(30);
    const rows = Array.from(host.querySelectorAll('.kw-checkbox'));
    const rowsChecked = rows.length === 2 && rows.every((r) => r.checked);
    const errs = freshErrors(before);
    check(!threw && master.checked === true && rowsChecked && errs.length === 0,
      '全选：@el 间接引用生效且未被 preventDefault 回滚',
      [!threw ? '' : '抛错:' + threw, master.checked ? '' : '全选框被回滚为未勾选',
        rowsChecked ? '' : '行未全部勾选',
        errs.length ? '错误:' + errs[0].split('\n')[0] : ''].filter(Boolean).join('；'));
    // 复位：取消全选并移除临时行，避免污染后续断言
    click(master);
    await sleep(20);
    host.remove();
  }

  // D6. 防内联事件回流：模板内联 onclick 已完成 4 → 1 的收口，不允许再长回来。
  //     唯一保留的一处是图片灯箱的背景点击关闭——它需要比较 event.target === this，
  //     而委托的调用约定是 fn.apply(el, args)（不传事件对象），无法通用化。
  const INLINE_ONCLICK_ALLOWED = ['if(event.target===this)closeImageLightbox()'];
  const tplHtml = fs.readFileSync(path.join(ROOT, 'web', 'templates', 'index.html'), 'utf8');
  const inlineOnclicks = Array.from(tplHtml.matchAll(/onclick="([^"]*)"/g), (mm) => mm[1].trim());
  const unexpectedInline = inlineOnclicks.filter((v) => !INLINE_ONCLICK_ALLOWED.includes(v));
  check(unexpectedInline.length === 0,
    `模板无新增内联 onclick（允许 ${INLINE_ONCLICK_ALLOWED.length} 处已知：灯箱背景关闭）`,
    unexpectedInline.length
      ? `新增 ${unexpectedInline.length} 处: ${unexpectedInline.slice(0, 2).join(' | ').slice(0, 120)}`
      : `实际 ${inlineOnclicks.length} 处`);

  // D7. 内联迁移的通用机制（本轮新增，替代原先散落在模板里的写法）：
  //     ① triggerClick —— 替代内联 document.getElementById('x').click()（触发隐藏 file input）
  //     ② 键盘可达性 —— role="button" + data-action 元素在 Enter / Space 时等价一次点击
  //     上传区 upload-area 同时用到两者，是这条链路的真实样本。
  const uploadArea = w.document.getElementById('upload-area');
  const fileInput = w.document.getElementById('import-file');
  if (!uploadArea || !fileInput) {
    check(false, '上传区与隐藏 file input 存在（triggerClick 样本）',
      [!uploadArea ? '缺 upload-area' : '', !fileInput ? '缺 import-file' : ''].filter(Boolean).join('；'));
  } else {
    let inputClicks = 0;
    fileInput.click = function () { inputClicks++; }; // 拦截，避免 jsdom 触碰真实文件选择

    // ① 点击上传区 → 应触发 file input 的 click
    const beforeClick = allErrors().length;
    click(uploadArea);
    await sleep(20);
    const errsClick = freshErrors(beforeClick);
    check(inputClicks === 1 && errsClick.length === 0,
      'triggerClick：点击上传区触发隐藏 file input',
      [inputClicks === 1 ? '' : `input.click 被调用 ${inputClicks} 次（期望 1）`,
        errsClick.length ? '错误:' + errsClick[0].split('\n')[0] : ''].filter(Boolean).join('；'));

    // ② 键盘：Enter / Space 各触发一次点击（累计 3）
    if (uploadArea.focus) { try { uploadArea.focus(); } catch { /* ignore */ } }
    const beforeKeys = allErrors().length;
    for (const key of ['Enter', ' ']) {
      uploadArea.dispatchEvent(new w.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
    }
    await sleep(20);
    const errsKeys = freshErrors(beforeKeys);
    check(inputClicks === 3 && errsKeys.length === 0,
      '键盘可达性：role=button + data-action 元素 Enter/Space 等价一次点击',
      [inputClicks === 3 ? '' : `累计触发 ${inputClicks} 次（期望 3 = 1 次点击 + Enter + Space）`,
        errsKeys.length ? '错误:' + errsKeys[0].split('\n')[0] : ''].filter(Boolean).join('；'));
  }

  // D8. <a href="#"> 的默认 # 跳转被阻止（等价旧内联 `switchPage('deadletters');return false;`
  //     的 return false；顺带修掉 doLogout 那处原本会留下 # 的锚点）。
  //     用临时锚点探测，既验证委托分发成功（正面），又验证 defaultPrevented（防规则失效），
  //     且不依赖真实模板元素、无副作用。同时 D5 已反证非锚点（复选框）未被误伤。
  if (!w.Linkora || !w.Linkora.actions) {
    check(false, '<a href="#"> 的默认 # 跳转被阻止', 'actions 缺失，跳过探测');
  } else {
    let hit = 0;
    let defaultPrevented = null;
    w.Linkora.actions.__smoke_anchor = () => { hit++; };
    const a = w.document.createElement('a');
    a.setAttribute('href', '#');
    a.setAttribute('data-action', '__smoke_anchor');
    a.setAttribute('data-args', '[]');
    w.document.body.appendChild(a);
    const capture = (e) => { setTimeout(() => { defaultPrevented = e.defaultPrevented; }, 0); };
    w.document.addEventListener('click', capture, true);
    click(a);
    await sleep(30);
    w.document.removeEventListener('click', capture, true);
    a.remove();
    check(hit === 1 && defaultPrevented === true,
      '<a href="#"> 的默认 # 跳转被阻止（等价旧内联的 return false）',
      [hit === 1 ? '' : `action 未分发（hit=${hit}）`,
        defaultPrevented === true ? '' : `defaultPrevented=${defaultPrevented}`].filter(Boolean).join('；'));
  }

  // D9. 图片加载失败降级（app.js 捕获阶段 error 监听）：
  //     本站图片接口（/api/image/*）破图须替换为 .img-unavailable 占位；
  //     非本站图片（外链）不得被改写；灯箱共享节点不得被替换（否则灯箱链路崩）。
  //     用例各自独立容器，避免一条失败污染其他断言的可读性。
  {
    const stage = w.document.createElement('div');
    w.document.body.appendChild(stage);
    const pane = () => {
      const d = w.document.createElement('div');
      stage.appendChild(d);
      return d;
    };
    const mkImg = (src, parent) => {
      const img = w.document.createElement('img');
      img.setAttribute('src', src);
      parent.appendChild(img);
      return img;
    };
    // 真实浏览器里 404 由网络层触发 error；jsdom 不加载图片，故手动派发同型事件。
    const fire = (el) => el.dispatchEvent(new w.Event('error'));
    const countPh = (el) => el.querySelectorAll('.img-unavailable').length;

    // 1) 本站 API 图片：应被降级为占位（img 本体被移除）
    const p1 = pane();
    const broken = mkImg('/api/image/dingtalk/ding9888ef577f7811cb/a/b/ocr_x.png?w=320&fmt=webp', p1);
    fire(broken);
    const degraded = countPh(p1) === 1 && !broken.isConnected;
    fire(broken);  // 2) 幂等：重复 error 不应产生第二个占位
    const idempotent = countPh(p1) === 1;

    // 3) 外链图片：不属本站图片接口 → 不得改写
    const p3 = pane();
    const ext = mkImg('https://example.com/x.png', p3);
    fire(ext);
    const extUntouched = ext.isConnected && countPh(p3) === 0;

    // 4) 对话图包裹层：img 被 .chat-image-wrap 包着时应连包裹层一起替换，不留空边框盒子
    const p4 = pane();
    const wrap = w.document.createElement('div');
    wrap.className = 'chat-image-wrap';
    p4.appendChild(wrap);
    const wrapped = mkImg('/api/image/dingtalk/a/b/c/ocr_y.png', wrap);
    fire(wrapped);
    const wrapOk = countPh(p4) === 1 && !wrap.isConnected;

    // 5) 灯箱共享节点：即便 src 命中图片接口也不得替换（openImageLightbox 依赖它）
    const lb = w.document.getElementById('image-lightbox-img');
    let lbOk = true;
    if (lb) {
      lb.setAttribute('src', '/api/image/dingtalk/smoke/none/ocr_z.png');
      fire(lb);
      lbOk = w.document.getElementById('image-lightbox-img') === lb;
      lb.removeAttribute('src');
    }

    stage.remove();
    const bad = [];
    if (!degraded) bad.push('本站破图未降级');
    if (!idempotent) bad.push('未幂等（重复 error 产生多个占位）');
    if (!extUntouched) bad.push('外链图片被误改写');
    if (!wrapOk) bad.push('对话图包裹层未一并替换');
    if (!lbOk) bad.push('灯箱共享节点被替换');
    check(bad.length === 0, '图片 404 降级为占位（本站图片降级 / 外链不动 / 灯箱保护）', bad.join('；'));
  }
} catch (e) {
  failures.push('交互冒烟异常: ' + (e && e.message ? e.message : e));
  console.error('[runtime] 交互冒烟异常:', e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n') : e);
}

try { dom.window.close(); } catch { /* ignore */ }

const finalErrors = realErrors();
if (finalErrors.length) {
  console.log('\n--- 运行时错误明细 ---');
  finalErrors.slice(0, 8).forEach((e) => console.log(e + '\n'));
}
if (failures.length) {
  console.error(`\n[runtime] FAIL — ${failures.length} 项未通过: ${failures.join('；')}`);
  process.exit(1);
}
console.log('[runtime] OK — 起步正常、init 完整、全站页面切换零运行时错误');
process.exit(0);
