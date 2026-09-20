// 真实 DOM 渲染测试：persona 页确定性渲染函数
// 沿用 Phase 2 护栏模式：向 jsdom 注入真实节点 → 调用渲染函数 → 断言节点内容/className。
// 这些函数原本不挂 window（IIFE 内部），经 window.Linkora 桥接暴露（零行为变更）。
import { describe, it, expect, beforeAll, beforeEach } from 'vitest';

let L;
beforeAll(async () => {
  // 加载页面脚本，触发 window.Linkora 桥接
  await import('../pages/persona.js');
  expect(window.Linkora).toBeTruthy();
  L = window.Linkora;
  expect(typeof L.renderStatus).toBe('function');
  expect(typeof L.renderQuality).toBe('function');
  expect(typeof L.renderSource).toBe('function');
  // renderSource 的平台名解析走 (window.store && window.store.getPlatform) 分支：
  // 固定 _platformNames 并保证 store.getPlatform 存在，使分支确定性走「查表」路径。
  window._platformNames = { dingtalk: '钉钉', feishu: '飞书', wecom: '企微' };
  if (!window.store) window.store = {};
  if (typeof window.store.getPlatform !== 'function') window.store.getPlatform = () => 'dingtalk';
});

// 每次测试前重建真实的 persona 节点（setup.js 的 getElementById 真实优先）
beforeEach(() => {
  document.body.innerHTML = [
    '<span id="ps-auto-status"></span>',
    '<span id="ps-confidence"></span>',
    '<span id="ps-src-completeness"></span>',
    '<div id="ps-freshness"></div>',
    '<span id="ps-src-platform"></span>',
    '<span id="ps-src-owner"></span>',
    '<span id="ps-src-total"></span>',
    '<span id="ps-src-owner-msg"></span>',
    '<span id="ps-src-limit"></span>',
    '<span id="ps-src-clean"></span>',
    '<span id="ps-src-owner-2"></span>',
    '<div id="ps-source-hint"></div>',
  ].join('\n');
});

describe('renderStatus（状态徽章）', () => {
  it('自动画像已启用', () => {
    L.renderStatus({ auto_profile: { prompt: 'x' } });
    const el = document.getElementById('ps-auto-status');
    expect(el.textContent).toBe('自动画像已启用');
    expect(el.classList.contains('active')).toBe(true);
    expect(el.classList.contains('empty')).toBe(false);
  });

  it('手动覆盖已启用（全局）', () => {
    L.renderStatus({ override: { enabled: true, prompt: 'y' } });
    const el = document.getElementById('ps-auto-status');
    expect(el.textContent).toBe('手动覆盖已启用（全局）');
    expect(el.classList.contains('active')).toBe(true);
  });

  it('手动覆盖已启用（本平台）', () => {
    L.renderStatus({ override: { enabled: true, prompt: 'y', is_platform_specific: true } });
    const el = document.getElementById('ps-auto-status');
    expect(el.textContent).toBe('手动覆盖已启用（本平台）');
  });

  it('尚无画像（空数据）', () => {
    L.renderStatus({});
    const el = document.getElementById('ps-auto-status');
    expect(el.textContent).toBe('尚无画像');
    expect(el.classList.contains('empty')).toBe(true);
    expect(el.classList.contains('active')).toBe(false);
  });
});

describe('renderQuality（置信度 / 完整度 / 时效）', () => {
  it('高置信度展示样本量与等级', () => {
    L.renderQuality({ auto_profile: { confidence: 'high', cleaned_count: 120 } });
    const el = document.getElementById('ps-confidence');
    expect(el.textContent).toBe('样本量 120 条 · 置信度高');
    expect(el.className).toContain('conf-high');
  });

  it('缺置信度 → 占位', () => {
    L.renderQuality({ auto_profile: {} });
    const el = document.getElementById('ps-confidence');
    expect(el.textContent).toBe('置信度 —');
    expect(el.className).toBe('ps-status-badge');
  });

  it('完整度评分格式化', () => {
    L.renderQuality({ auto_profile: { completeness: 75 } });
    expect(document.getElementById('ps-src-completeness').textContent).toBe('75 / 100');
  });

  it('完整度缺省 → —', () => {
    L.renderQuality({});
    expect(document.getElementById('ps-src-completeness').textContent).toBe('—');
  });

  it('时效：未过期', () => {
    L.renderQuality({ freshness: { days_since_update: 3, stale: false } });
    const el = document.getElementById('ps-freshness');
    expect(el.innerHTML).toContain('3 天前更新');
    expect(el.className).toContain('ps-freshness-ok');
  });

  it('时效：已过期提示重算', () => {
    L.renderQuality({ freshness: { days_since_update: 10, stale: true } });
    const el = document.getElementById('ps-freshness');
    expect(el.innerHTML).toContain('已 10 天未更新，建议重算');
    expect(el.className).toContain('ps-freshness-stale');
  });

  it('时效：缺数据 → 占位', () => {
    L.renderQuality({});
    const el = document.getElementById('ps-freshness');
    expect(el.innerHTML).toContain('时效：—');
    expect(el.className).toBe('ps-meta-item');
  });
});

describe('renderSource（抽取来源多字段映射）', () => {
  it('正常来源映射 + 平台名解析 + 清洗状态', () => {
    L.renderSource(
      {
        platform: 'dingtalk',
        owner: '张三',
        total_messages: 100,
        owner_messages: 80,
        sample_limit: 50,
      },
      { raw_count: 200, cleaned_count: 180 }
    );
    expect(document.getElementById('ps-src-platform').textContent).toBe('钉钉');
    expect(document.getElementById('ps-src-owner').textContent).toBe('张三');
    expect(document.getElementById('ps-src-total').textContent).toBe('100');
    expect(document.getElementById('ps-src-owner-msg').textContent).toBe('80');
    expect(document.getElementById('ps-src-limit').textContent).toBe('50');
    // 清洗状态：raw → cleaned
    expect(document.getElementById('ps-src-clean').textContent).toBe('是 200→180');
    // owner-2：非「未配置」时显示 owner
    expect(document.getElementById('ps-src-owner-2').textContent).toBe('张三');
    // 提示：已基于 N 条主人历史消息抽取
    expect(document.getElementById('ps-source-hint').textContent).toBe('已基于 80 条主人历史消息抽取');
  });

  it('清洗前后相等 → 已清洗(N)', () => {
    L.renderSource({ platform: 'feishu', owner: '李四', total_messages: 10 }, { raw_count: 10, cleaned_count: 10 });
    expect(document.getElementById('ps-src-clean').textContent).toBe('已清洗（10）');
  });

  it('无历史消息计数 → 无法读取提示', () => {
    L.renderSource({ platform: 'wecom', owner: '未配置' }, {});
    expect(document.getElementById('ps-src-platform').textContent).toBe('企微');
    expect(document.getElementById('ps-src-owner').textContent).toBe('未配置');
    expect(document.getElementById('ps-src-total').textContent).toBe('—');
    expect(document.getElementById('ps-source-hint').textContent).toBe('无法读取历史消息计数');
  });

  it('零消息 → 暂无历史消息提示', () => {
    L.renderSource({ platform: 'dingtalk', owner: '王五', total_messages: 0 }, {});
    expect(document.getElementById('ps-source-hint').textContent).toBe('该平台暂无历史消息，无法抽取');
  });

  it('有总数无主人消息 → 身份配置提示', () => {
    L.renderSource({ platform: 'dingtalk', owner: '王五', total_messages: 5, owner_messages: 0 }, {});
    expect(document.getElementById('ps-source-hint').textContent).toBe('未找到该主人发出的消息（请检查平台主人身份配置）');
  });
});
