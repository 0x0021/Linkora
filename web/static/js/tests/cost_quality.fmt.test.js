// 纯函数单元测试：cost_quality 的格式化工具
// 这些是确定性纯函数（无 DOM / 无异步），经 window.Linkora 桥接暴露（零行为变更）。
import { describe, it, expect, beforeAll } from 'vitest';

let L;
beforeAll(async () => {
  await import('../pages/cost_quality.js');
  expect(window.Linkora).toBeTruthy();
  L = window.Linkora;
  expect(typeof L.cqFmtPct).toBe('function');
  expect(typeof L.cqFmtCostCny).toBe('function');
});

describe('cqFmtPct（百分比格式化）', () => {
  it('null / 0 → 0%', () => {
    expect(L.cqFmtPct(null)).toBe('0%');
    expect(L.cqFmtPct(0)).toBe('0%');
    expect(L.cqFmtPct(undefined)).toBe('0%');
  });

  it('正常比例 → 一位小数 + %', () => {
    expect(L.cqFmtPct(0.5)).toBe('50.0%');
    expect(L.cqFmtPct(1)).toBe('100.0%');
    expect(L.cqFmtPct(0.005)).toBe('0.5%');
    expect(L.cqFmtPct(0.1234)).toBe('12.3%');
  });
});

describe('cqFmtCostCny（人民币格式化）', () => {
  it('null / 0 → ¥0.00', () => {
    expect(L.cqFmtCostCny(null)).toBe('¥0.00');
    expect(L.cqFmtCostCny(0)).toBe('¥0.00');
  });

  it('极小额（<0.01）→ 四位小数', () => {
    expect(L.cqFmtCostCny(0.005)).toBe('¥0.0050');
    expect(L.cqFmtCostCny(0.009)).toBe('¥0.0090');
  });

  it('常规额 → 两位小數 + ¥ 前缀', () => {
    expect(L.cqFmtCostCny(0.01)).toBe('¥0.01');
    expect(L.cqFmtCostCny(12.3)).toBe('¥12.30');
    expect(L.cqFmtCostCny(999.999)).toBe('¥1000.00'); // toFixed 四舍五入
  });
});
