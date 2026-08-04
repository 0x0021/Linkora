/**
 * 主题切换器：点击立即在 light / dark 之间切换
 * - 防闪烁：index.html <head> 内联脚本已在首屏前设置 data-theme
 * - 本脚本负责图标同步、状态持久化
 */
(function () {
  'use strict';

  const KEY = 'dt-theme';
  const ICONS = { light: 'fa-sun', dark: 'fa-moon' };
  const LABELS = { light: '浅色模式', dark: '深色模式' };

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

  // 初始化
  apply(currentMode());

  // 如果旧存的是 'system'，升级为具体模式并持久化
  try {
    var old = localStorage.getItem(KEY);
    if (old === 'system') save(currentMode());
  } catch (e) {}
})();
