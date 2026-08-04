// persona.js 冒烟测试（P1-3）
// IIFE 页面脚本：入口函数挂在 window 上；整条渲染链路在 jsdom + 轻量 DOM stub 下应「不崩」。
import { describe, it, expect, beforeEach, vi } from 'vitest';

// 一个典型 /api/persona 返回，覆盖自动画像 / 覆盖 / few-shot / 来源 / 时效 / 冷启动 各分支
const PERSONA_FIXTURE = {
  auto_profile: {
    prompt: '主人风格：简短、直接。',
    avg_len: 30, emoji_rate: 0.2, polite_rate: 0.5, casual_rate: 0.3,
    short_rate: 0.1, question_rate: 0.4, sample_count: 10,
    confidence: 'high', cleaned_count: 10, completeness: 80,
    prompt_source: 'llm', top_openers: ['在吗', '帮我看下'],
  },
  override: { enabled: false, prompt: '' },
  few_shot_examples: [],
  source: {
    platform: 'dingtalk', owner: 'me', total_messages: 100,
    owner_messages: 50, sample_limit: 200, raw_count: 120, cleaned_count: 100,
  },
  freshness: { days_since_update: 2, stale: false },
  cold_start: { is_cold: false },
};

describe('persona.js (smoke)', () => {
  beforeEach(async () => {
    vi.resetModules();
    await import('../pages/persona.js');
    // 让 api.fetch 默认返回成功 fixture
    globalThis.api.fetch = vi.fn(async () => PERSONA_FIXTURE);
  });

  it('脚本加载且不抛错，并暴露关键入口函数', () => {
    expect(typeof window.loadPersonaPage).toBe('function');
    expect(typeof window.runBacktest).toBe('function');
    expect(typeof window.reanalyzePersona).toBe('function');
    expect(typeof window.saveOverride).toBe('function');
    expect(typeof window.openVersionModal).toBe('function');
  });

  it('loadPersonaPage 整条渲染链路在 stub 环境下不崩，并触发 KPI 渲染', async () => {
    await window.loadPersonaPage();
    // renderAuto 内对每个风格指标调用了统一 KpiCard 组件
    expect(globalThis.renderKpiCard).toHaveBeenCalled();
    expect(globalThis.renderKpiCard.mock.calls.length).toBeGreaterThan(0);
  });

  it('loadPersonaPage 在 API 报错时走错误条分支而不崩', async () => {
    globalThis.api.fetch = vi.fn(async () => ({ error: 'unauthorized', status: 401 }));
    await window.loadPersonaPage();
    // 错误分支只更新错误条，不应抛
    expect(true).toBe(true);
  });
});
