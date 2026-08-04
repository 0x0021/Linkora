// ============ components/kpiCard.js ============
// 统一 KPI 卡片组件（消除 dashboard/persona/cost-quality/metrics 四套手写 KPI 渲染）
// 依赖：全局 escapeHtml（core/app.js）
//
// 用法：
//   renderKpiCard('cq-kpi-cost', {
//     label: '24h 成本', value: '¥0.00', icon: '💰',
//     sub: '≈ $0.00',
//     trend: { dir: 'down', value: '-12%', good: true }
//   });
//   updateKpiValue('cq-kpi-cost', '¥1.20');   // 轮询时仅刷新数值，不重建卡片
//
// 容器 target 可以是 id 字符串或 DOM 元素；组件保留容器原有 class，仅注入统一内部结构。

(function (global) {
  'use strict';

  function _resolve(target) {
    return typeof target === 'string' ? document.getElementById(target) : target;
  }

  function _trendHtml(t) {
    const dir = t.dir === 'up' ? '▲' : t.dir === 'down' ? '▼' : '▶';
    const cls = t.good === false ? 'is-bad' : t.good === true ? 'is-good' : 'is-neutral';
    return `<div class="kpi-trend ${cls}">${dir} <span>${escapeHtml(String(t.value ?? ''))}</span></div>`;
  }

  function renderKpiCard(target, opts) {
    const el = _resolve(target);
    if (!el) return null;
    const id = opts.id || el.id || ('kpi-' + Math.random().toString(36).slice(2, 8));
    const variant = opts.variant ? ' kpi-card--' + opts.variant : '';
    // icon 支持纯文本（emoji 等）或 HTML 片段（<i class="...">）；HTML 片段不 escape
    const isHtmlIcon = opts.icon && typeof opts.icon === 'string' && opts.icon.trim().startsWith('<');
    const icon = opts.icon ? `<div class="kpi-icon" aria-hidden="true">${isHtmlIcon ? opts.icon : escapeHtml(String(opts.icon))}</div>` : '';
    const sub = opts.sub ? `<div class="kpi-sub" id="${id}-sub">${escapeHtml(String(opts.sub))}</div>` : '';
    const trend = opts.trend ? _trendHtml(opts.trend) : '';
    // 重建 className：仅保留非 kpi-card 系列的既有类，避免每次 render 累积 'kpi-card'
    const preserved = (el.className || '').split(/\s+/).filter(c => c && !c.startsWith('kpi-card'));
    el.className = ('kpi-card' + variant + (preserved.length ? ' ' + preserved.join(' ') : '')).trim();
    el.setAttribute('data-kpi', id);
    el.innerHTML = `${icon}
      <div class="kpi-main">
        <div class="kpi-label">${escapeHtml(opts.label || '')}</div>
        <div class="kpi-value" id="${id}-value">${escapeHtml(String(opts.value ?? ''))}</div>
        ${sub}${trend}
      </div>`;
    return el;
  }

  function updateKpiValue(target, value, sub) {
    const el = _resolve(target);
    if (!el) return;
    const baseId = el.getAttribute('data-kpi') || el.id;
    if (baseId) {
      const v = document.getElementById(baseId + '-value');
      if (v) v.textContent = (value == null ? '' : String(value));
      if (sub != null) {
        const s = document.getElementById(baseId + '-sub');
        if (s) s.textContent = String(sub);
      }
    } else {
      el.textContent = (value == null ? '' : String(value));
    }
  }

  global.KpiCard = { render: renderKpiCard, update: updateKpiValue };
  global.renderKpiCard = renderKpiCard;
  global.updateKpiValue = updateKpiValue;
})(window);
