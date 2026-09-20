// kpiCard 真实 DOM 渲染测试（Phase 2 质量护栏）
// 验证统一 KPI 卡片组件「数据 → DOM」的正确性：卡片结构 / label / value / sub / trend /
// icon（纯文本 vs HTML 片段）/ data-kpi 属性，以及 updateKpiValue 的局部刷新。
// 依赖 setup.js 注入的 escapeHtml，组件本身无 canvas / 无 async 取数，是干净范本。
import { describe, it, expect, beforeEach } from 'vitest';

describe('renderKpiCard 真实 DOM 渲染', () => {
  let el;

  beforeEach(async () => {
    document.body.innerHTML = '<div id="kpi"></div>';
    el = document.getElementById('kpi');
    // 加载组件真实实现（IIFE 幂等），覆盖 setup 预置的 vi.fn stub
    await import('../components/kpiCard.js');
    expect(typeof window.renderKpiCard).toBe('function');
  });

  it('渲染基础卡片结构含 label / value', () => {
    window.renderKpiCard('kpi', { label: '24h 成本', value: '¥0.00' });

    expect(el.classList.contains('kpi-card')).toBe(true);
    expect(el.querySelector('.kpi-label').textContent).toBe('24h 成本');
    expect(el.querySelector('.kpi-value').textContent).toBe('¥0.00');
    // 默认带 data-kpi 标识
    expect(el.getAttribute('data-kpi')).toBeTruthy();
  });

  it('sub 与 trend 正确渲染', () => {
    window.renderKpiCard('kpi', {
      label: '调用次数',
      value: '128',
      sub: '≈ 昨日',
      trend: { dir: 'up', value: '+12%', good: true },
    });

    expect(el.querySelector('.kpi-sub').textContent).toBe('≈ 昨日');
    const trend = el.querySelector('.kpi-trend');
    expect(trend).not.toBeNull();
    expect(trend.classList.contains('is-good')).toBe(true);
    expect(trend.textContent).toContain('+12%');
    expect(trend.textContent).toContain('▲'); // up 方向
  });

  it('trend good=false 标记 is-bad，down 方向用 ▼', () => {
    window.renderKpiCard('kpi', {
      label: '错误率',
      value: '3%',
      trend: { dir: 'down', value: '-1%', good: false },
    });
    const trend = el.querySelector('.kpi-trend');
    expect(trend.classList.contains('is-bad')).toBe(true);
    expect(trend.textContent).toContain('▼');
  });

  it('纯文本 icon 会被 escapeHtml，HTML 片段 icon 原样注入', () => {
    // 纯文本 emoji：escapeHtml 包裹
    window.renderKpiCard('kpi', { label: 'L', value: 'V', icon: '💰' });
    expect(el.querySelector('.kpi-icon').textContent).toBe('💰');

    // HTML 片段：不 escape（支持 <i class> 图标）
    document.body.innerHTML = '<div id="kpi2"></div>';
    const el2 = document.getElementById('kpi2');
    window.renderKpiCard(el2, { label: 'L2', value: 'V2', icon: '<i class="fa-solid fa-robot"></i>' });
    const icon = el2.querySelector('.kpi-icon');
    expect(icon.querySelector('i.fa-robot')).not.toBeNull();
  });

  it('updateKpiValue 局部刷新数值与 sub，不重建卡片', () => {
    window.renderKpiCard('kpi', { label: '成本', value: '¥1.00', sub: 'a' });
    const dataKpi = el.getAttribute('data-kpi');

    window.updateKpiValue('kpi', '¥2.50', 'b');

    expect(el.getAttribute('data-kpi')).toBe(dataKpi); // 仍是同一卡片
    expect(el.querySelector('.kpi-value').textContent).toBe('¥2.50');
    expect(el.querySelector('.kpi-sub').textContent).toBe('b');
  });

  it('接受元素对象作为 target', () => {
    window.renderKpiCard(el, { label: '直接传元素', value: 'OK' });
    expect(el.querySelector('.kpi-value').textContent).toBe('OK');
  });
});
