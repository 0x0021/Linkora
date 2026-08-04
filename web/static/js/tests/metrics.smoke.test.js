// metrics.js 冒烟测试（P1-3）
// 经典脚本：纯格式化函数原本是顶层全局，这里显式挂到 window 以便 ESM/测试访问。
import { describe, it, expect, beforeEach } from 'vitest';

describe('metrics.js (smoke)', () => {
  beforeEach(async () => {
    vi.resetModules();
    await import('../pages/metrics.js');
  });

  it('脚本加载且不抛错，并暴露格式化函数与入口', () => {
    expect(typeof window.metricsFmtMs).toBe('function');
    expect(typeof window.metricsFmtNum).toBe('function');
    expect(typeof window.metricsFmtTokens).toBe('function');
    expect(typeof window.metricsFmtCost).toBe('function');
    // 这两个入口原本就挂在 window 上
    expect(typeof window.filterMetricsTable).toBe('function');
    expect(typeof window.loadMetricsReliability).toBe('function');
  });

  it('metricsFmtMs 正确处理 null/0/毫秒/秒', () => {
    expect(window.metricsFmtMs(null)).toBe('—');
    expect(window.metricsFmtMs(0)).toBe('—');
    expect(window.metricsFmtMs(500)).toBe('500ms');
    expect(window.metricsFmtMs(1500)).toBe('1.50s');
    expect(window.metricsFmtMs(1234)).toBe('1.23s');
  });

  it('metricsFmtNum 处理 null 与千分位', () => {
    expect(window.metricsFmtNum(null)).toBe('—');
    const out = window.metricsFmtNum(1234567);
    expect(typeof out).toBe('string');
    expect(out).toContain('234');
    expect(out).not.toBe('—');
  });

  it('metricsFmtTokens 处理 K/M 单位', () => {
    expect(window.metricsFmtTokens(null)).toBe('—');
    expect(window.metricsFmtTokens(0)).toBe('—');
    expect(window.metricsFmtTokens(1500)).toBe('1.5K');
    expect(window.metricsFmtTokens(1500000)).toBe('1.50M');
  });

  it('metricsFmtCost 处理 $0 与小数精度', () => {
    expect(window.metricsFmtCost(0)).toBe('$0.00');
    expect(window.metricsFmtCost(0.005)).toBe('$0.0050');
    expect(window.metricsFmtCost(1.5)).toBe('$1.50');
    expect(window.metricsFmtCost(0.5)).toBe('$0.50');
  });
});
