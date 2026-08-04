// ============ components/stateBadge.js ============
// 状态徽标组件（ok / warn / bad / info / neutral）
// 依赖：无

(function (global) {
  'use strict';

  // variant: ok | warn | bad | info | neutral
  function renderStateBadge(target, text, variant) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;
    const v = variant || 'neutral';
    el.className = ('state-badge state-badge--' + v + (el.className ? ' ' + el.className : '')).trim();
    el.textContent = (text == null ? '' : String(text));
    return el;
  }

  global.StateBadge = { render: renderStateBadge };
  global.renderStateBadge = renderStateBadge;
})(window);
