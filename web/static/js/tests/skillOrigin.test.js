// 技能来源徽标（内置 vs 用户安装）单测。
//
// 锁住全站统一视觉语言的数据契约：
// - skillOriginBadgeHtml(origin)：显式 origin → 徽标 HTML（builtin=靛蓝 fa-cube / user=琥珀 fa-arrow-down-to-bracket）
// - registerSkillOrigins / ensureSkillOriginMap：name→origin 注册表（ensure 全局只发一次请求）
// - skillOriginBadge(name)：历史记录（决策流/路由追踪）按技能名查表渲染
// - applySkillOriginBadges(root)：把 [data-skill-origin] 占位槽补上徽标（幂等）
// - 决策行（decisionTable）技能 pill 内含来源占位槽
import { describe, it, expect, beforeEach, vi } from 'vitest';

describe('技能来源徽标', () => {
  beforeEach(async () => {
    document.body.innerHTML = '';
    // util.js 通过 window.api.fetch 拉取 /api/skills；每次用例前重置模块状态
    window.api = {
      fetch: vi.fn(() => Promise.resolve({ skills: [] })),
    };
    // 动态 import 拿最新模块实例（vitest 模块注册表按文件缓存）
    vi.resetModules();
    await import('../core/util.js');
  });

  it('skillOriginBadgeHtml：builtin/user 渲染对应样式与图标，未知 origin 返回空', () => {
    const builtin = window.skillOriginBadgeHtml('builtin');
    expect(builtin).toContain('origin-badge--builtin');
    expect(builtin).toContain('fa-cube');
    expect(builtin).toContain('内置');
    const user = window.skillOriginBadgeHtml('user');
    expect(user).toContain('origin-badge--user');
    expect(user).toContain('fa-arrow-down-to-bracket');
    expect(user).toContain('安装');
    expect(window.skillOriginBadgeHtml('')).toBe('');
    expect(window.skillOriginBadgeHtml('unknown')).toBe('');
    expect(window.skillOriginBadgeHtml(undefined)).toBe('');
  });

  it('registerSkillOrigins + skillOriginBadge：按技能名查找渲染', () => {
    window.registerSkillOrigins([
      { name: 'contract-review', origin: 'builtin' },
      { name: 'weather', origin: 'user' },
      { name: 'bad-entry' },            // 缺 origin → 忽略
      { origin: 'user' },               // 缺 name → 忽略
    ]);
    expect(window.skillOriginBadge('contract-review')).toContain('origin-badge--builtin');
    expect(window.skillOriginBadge('weather')).toContain('origin-badge--user');
    // 未注册 / 空名 → 无徽标（不渲染占位）
    expect(window.skillOriginBadge('never-installed')).toBe('');
    expect(window.skillOriginBadge('')).toBe('');
  });

  it('ensureSkillOriginMap：拉取 /api/skills 并注册，全局只发一次请求', async () => {
    window.api.fetch = vi.fn(() =>
      Promise.resolve({ skills: [{ name: 'weather', origin: 'user' }] })
    );
    await window.ensureSkillOriginMap();
    await window.ensureSkillOriginMap();   // 第二次应命中缓存
    expect(window.api.fetch).toHaveBeenCalledTimes(1);
    expect(window.api.fetch).toHaveBeenCalledWith('/api/skills');
    expect(window.skillOriginBadge('weather')).toContain('origin-badge--user');
  });

  it('ensureSkillOriginMap：请求失败时静默降级（不抛错、无徽标）', async () => {
    window.api.fetch = vi.fn(() => Promise.reject(new Error('network')));
    await expect(window.ensureSkillOriginMap()).resolves.toBeUndefined();
    expect(window.skillOriginBadge('weather')).toBe('');
  });

  it('applySkillOriginBadges：回填占位槽且幂等（不重复包裹）', () => {
    window.registerSkillOrigins([{ name: 'weather', origin: 'user' }]);
    document.body.innerHTML = `
      <div id="feed">
        <span class="pill pill-skill">⚡ weather<span class="skill-origin-slot" data-skill-origin="weather"></span></span>
        <span class="pill pill-skill">⚡ contract-review<span class="skill-origin-slot" data-skill-origin="contract-review"></span></span>
      </div>`;
    const feed = document.getElementById('feed');
    window.applySkillOriginBadges(feed);
    const slots = feed.querySelectorAll('[data-skill-origin]');
    expect(slots[0].querySelector('.origin-badge--user')).not.toBeNull();
    // 未注册技能 → 槽位保持为空（不渲染未知徽标）
    expect(slots[1].innerHTML).toBe('');
    // 幂等：再次调用不会把徽标再包一层
    window.applySkillOriginBadges(feed);
    expect(slots[0].querySelectorAll('.origin-badge')).toHaveLength(1);
  });

  it('决策行技能 pill 带来源占位槽（renderDecisionRow 产出契约）', async () => {
    vi.resetModules();
    await import('../components/decisionTable.js');
    const row = window.renderDecisionRow({
      ts: '2026-10-14T10:00:00+00:00',
      content: '北京天气怎么样',
      action: 'llm',
      sender: '测试用户',
      skill_name: 'weather',
      skill_source: 'intent',
    });
    expect(row).toContain('data-skill-origin="weather"');
    expect(row).toContain('skill-origin-slot');
  });
});
