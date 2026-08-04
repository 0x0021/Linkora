// ============ components/panel.js ============
// 面板与空态辅助组件（复用既有 .panel 结构）
// 依赖：全局 escapeHtml

(function (global) {
  'use strict';

  // 将容器渲染为一个 .panel（header + body）
  function renderPanel(target, opts) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;
    const actions = opts.actions ? `<div class="panel-actions">${opts.actions}</div>` : '';
    el.className = ('panel' + (opts.className ? ' ' + opts.className : '')).trim();
    el.innerHTML = `
      <div class="panel-header">
        <h3>${opts.icon ? opts.icon + ' ' : ''}${escapeHtml(opts.title || '')}</h3>
        ${actions}
      </div>
      <div class="panel-body">${opts.body || ''}</div>`;
    return el;
  }

  function emptyState(target, text) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;
    el.innerHTML = `<div class="metrics-empty">${escapeHtml(text || '暂无数据')}</div>`;
    return el;
  }

  global.Panel = { render: renderPanel, emptyState };
  global.renderPanel = renderPanel;
  global.renderEmptyState = emptyState;
})(window);
