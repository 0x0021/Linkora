// ============ components/chartCard.js ============
// 统一图表卡片生命周期：canvas 确保/重建、空态、Chart 实例登记与销毁，复用 chartTheme()
// 依赖：全局 escapeHtml、chartTheme、Chart（Chart.js）
//
// 用法：
//   const ctx = ChartCard.ensureCanvas('chart-cq-confidence', 'chart-cq-confidence');
//   if (!ctx) return;
//   // 数据为空：ChartCard.showEmpty('chart-cq-confidence', '暂无置信度数据');
//   const chart = new Chart(ctx.canvas, { ... });
//   ChartCard.setChart('chart-cq-confidence', chart);   // 登记以便后续销毁
//   // 页面 stop 时：ChartCard.destroy('chart-cq-confidence');
//   // 全部销毁：ChartCard.destroyAll();

(function (global) {
  'use strict';

  const _registry = new Map(); // canvasId -> Chart

  function ensureCanvas(wrapId, canvasId, label) {
    const wrap = typeof wrapId === 'string' ? document.getElementById(wrapId) : wrapId;
    if (!wrap) return null;
    const ariaLabel = escapeHtml(label || canvasId);
    let canvas = wrap.querySelector('canvas');
    if (!canvas) {
      wrap.innerHTML = `<canvas id="${canvasId}" role="img" aria-label="${ariaLabel}"></canvas>`;
      canvas = wrap.querySelector('canvas');
    } else if (label) {
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', ariaLabel);
    }
    return { wrap, canvas };
  }

  function showEmpty(wrapId, text) {
    const wrap = typeof wrapId === 'string' ? document.getElementById(wrapId) : wrapId;
    if (wrap) wrap.innerHTML = `<div class="metrics-empty">${escapeHtml(text || '暂无数据')}</div>`;
  }

  function setChart(canvasId, chart) {
    if (!canvasId) return;
    if (_registry.has(canvasId)) {
      try { _registry.get(canvasId).destroy(); } catch (_) {}
    }
    if (chart) _registry.set(canvasId, chart);
  }

  function destroy(canvasId) {
    if (_registry.has(canvasId)) {
      try { _registry.get(canvasId).destroy(); } catch (_) {}
      _registry.delete(canvasId);
    }
  }

  function destroyAll() {
    Array.from(_registry.keys()).forEach(destroy);
  }

  global.ChartCard = { ensureCanvas, showEmpty, setChart, destroy, destroyAll };
})(window);
