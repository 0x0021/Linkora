/**
 * 主题切换器：明暗模式（light / dark）+ 主题配色（accent）双轴管理
 * - 防闪烁：index.html <head> 内联脚本已在首屏前设置 data-theme / data-accent
 * - 本脚本负责图标与浮层同步、状态持久化（localStorage: dt-theme / dt-accent）
 * - 配色令牌定义在 css/theme-accent.css，两处新增配色需同步维护
 */
(function () {
  'use strict';

  const KEY = 'dt-theme';
  const ACCENT_KEY = 'dt-accent';

  const ICONS = { light: 'fa-sun', dark: 'fa-moon' };
  const LABELS = { light: '浅色模式', dark: '深色模式' };

  // 内置主题配色（与 css/theme-accent.css 的 [data-accent] 块一一对应；
  // light/dark 两档供 canvas 图表取色用，UI 本身走 CSS 变量无需 JS 参与）
  const ACCENTS = {
    blue:    { label: '湛蓝', light: '#2563eb', dark: '#3b82f6' },
    indigo:  { label: '靛青', light: '#4f46e5', dark: '#818cf8' },
    violet:  { label: '雅紫', light: '#7c3aed', dark: '#a78bfa' },
    cyan:    { label: '碧青', light: '#0891b2', dark: '#22d3ee' },
    emerald: { label: '翠绿', light: '#059669', dark: '#34d399' },
    amber:   { label: '琥珀', light: '#d97706', dark: '#fbbf24' },
    rose:    { label: '玫红', light: '#e11d48', dark: '#fb7185' },
  };
  const DEFAULT_ACCENT = 'blue';

  function currentMode() {
    try {
      var stored = localStorage.getItem(KEY);
      // 兼容旧值 'system' → 解析为当前有效主题
      if (!stored || stored === 'system') {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      }
      return stored;
    } catch (e) { return 'light'; }
  }

  function apply(mode) {
    document.documentElement.setAttribute('data-theme', mode);
    document.documentElement.setAttribute('data-bs-theme', mode);
    var btn = document.getElementById('theme-toggle');
    var icon = document.getElementById('theme-toggle-icon');
    if (btn) btn.title = LABELS[mode];
    if (icon) icon.className = 'fa-solid ' + ICONS[mode];
  }

  function save(mode) {
    try { localStorage.setItem(KEY, mode); } catch (e) {}
  }

  /** 点击即切换 light ↔ dark，无需循环 */
  window.cycleTheme = function () {
    var cur = currentMode();
    var next = cur === 'light' ? 'dark' : 'light';
    save(next);
    document.documentElement.classList.add('theme-anim');
    apply(next);
    window.dispatchEvent(new Event('dt-theme-change'));
    if (window.__themeAnimTimer) window.clearTimeout(window.__themeAnimTimer);
    window.__themeAnimTimer = window.setTimeout(function () {
      document.documentElement.classList.remove('theme-anim');
    }, 440);
  };

  // ==================== 主题配色（accent） ====================

  function currentAccent() {
    try {
      var a = localStorage.getItem(ACCENT_KEY);
      if (a && ACCENTS[a]) return a;
    } catch (e) { /* ignore */ }
    return DEFAULT_ACCENT;
  }

  /** 应用配色：写 data-accent 属性 + 同步浮层选中态；save=false 时只预览不持久化 */
  function applyAccent(id, save) {
    var meta = ACCENTS[id];
    if (!meta) id = DEFAULT_ACCENT;
    document.documentElement.setAttribute('data-accent', id);
    var menu = document.getElementById('accent-menu');
    if (menu) {
      var items = menu.querySelectorAll('.accent-swatch');
      for (var i = 0; i < items.length; i++) {
        var on = items[i].getAttribute('data-accent-id') === id;
        items[i].classList.toggle('active', on);
        items[i].setAttribute('aria-checked', on ? 'true' : 'false');
      }
    }
    if (save !== false) {
      try { localStorage.setItem(ACCENT_KEY, id); } catch (e) {}
    }
  }

  /**
   * 取当前主题配色主色（hex），供 Chart.js 等 canvas 场景使用——
   * canvas 不解析 CSS 变量字符串，必须由 JS 直接给具体色值。
   * 跟随 data-accent + data-theme 双轴；配色切换后需重绘图表才生效。
   */
  window.getAccentColor = function () {
    var meta = ACCENTS[currentAccent()] || ACCENTS[DEFAULT_ACCENT];
    return document.documentElement.getAttribute('data-theme') === 'dark'
      ? meta.dark : meta.light;
  };

  /** 构建配色选择浮层并绑定交互（纯 addEventListener，CSP 安全无内联 onclick） */
  function buildAccentMenu() {
    var wrap = document.getElementById('accent-picker');
    if (!wrap || document.getElementById('accent-menu')) return;

    var itemsHtml = '';
    Object.keys(ACCENTS).forEach(function (id) {
      var m = ACCENTS[id];
      itemsHtml += `<button type="button" class="accent-swatch" role="radio"`
        + ` data-accent-id="${id}" title="${m.label}" aria-label="${m.label}">`
        + `<span class="accent-swatch-dot" style="background:linear-gradient(135deg, ${m.light}, ${m.dark})"></span>`
        + `<span class="accent-swatch-name">${m.label}</span>`
        + `<i class="fa-solid fa-check accent-swatch-check" aria-hidden="true"></i></button>`;
    });
    var menu = document.createElement('div');
    menu.className = 'accent-menu';
    menu.id = 'accent-menu';
    menu.setAttribute('role', 'radiogroup');
    menu.setAttribute('aria-label', '主题配色');
    menu.innerHTML = `<div class="accent-menu-title">主题配色</div>`
      + `<div class="accent-swatch-grid">${itemsHtml}</div>`;
    wrap.appendChild(menu);

    function closeMenu() {
      menu.classList.remove('open');
      var toggle = document.getElementById('accent-toggle');
      if (toggle) toggle.setAttribute('aria-expanded', 'false');
    }

    wrap.addEventListener('click', function (ev) {
      var target = ev.target;
      var swatch = target.closest ? target.closest('.accent-swatch') : null;
      if (swatch) {
        applyAccent(swatch.getAttribute('data-accent-id'));
        closeMenu();
        return;
      }
      if (target.closest('.accent-toggle')) {
        var opening = !menu.classList.contains('open');
        menu.classList.toggle('open');
        var toggle = document.getElementById('accent-toggle');
        if (toggle) toggle.setAttribute('aria-expanded', opening ? 'true' : 'false');
      }
    });
    // 点击浮层外 / Esc 关闭
    document.addEventListener('click', function (ev) {
      if (!wrap.contains(ev.target)) closeMenu();
    });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closeMenu();
    });
  }

  // 初始化
  apply(currentMode());
  applyAccent(currentAccent());
  buildAccentMenu();

  // 如果旧存的是 'system'，升级为具体模式并持久化
  try {
    var old = localStorage.getItem(KEY);
    if (old === 'system') save(currentMode());
  } catch (e) {}
})();
