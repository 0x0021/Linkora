// 技能列表卡片渲染：来源徽标（内置 vs 用户安装）必须进入卡片 DOM。
//
// 覆盖主界面契约：/api/skills 返回的 origin 字段 → 卡片姓名旁的 .origin-badge
// （builtin=靛蓝 fa-cube「内置」/ user=琥珀 fa-arrow-down-to-bracket「安装」），
// 并顺带锁定 name→origin 注册表被列表页填充（供弹窗标题 / 跨页查找）。
import { describe, it, expect, beforeEach, vi } from 'vitest';

const SKILL = (name, origin) => ({
  name,
  description: `${name} 描述`,
  origin,
  enabled: true,
  weight: 0.6,
  platforms: [],
  intent_keywords: [],
  allowed_tools: [],
  has_config: false,
  source_path: `/tmp/${name}/SKILL.md`,
});

describe('技能列表卡片来源徽标', () => {
  beforeEach(async () => {
    vi.resetModules();
    document.body.innerHTML =
        '<div id="skills-content"></div><select id="skill-platform-select"></select>';
    // bundle 内 _installedSkillNames 由 app.js 顶层 let 声明（拼接共享作用域）；
    // ESM 单文件导入时补全局桩，语义与生产一致。
    globalThis._installedSkillNames = new Set();
    await import('../core/util.js');      // skillOriginBadgeHtml / registerSkillOrigins
    await import('../pages/skills.js');  // window.loadSkillsPage 桥接
  });

  it('按 origin 渲染内置/安装徽标，并注册 name→origin', async () => {
    window.api.fetch = vi.fn(async (url) => {
      if (url === '/api/skills') {
        return { skills: [SKILL('web-composite-search', 'builtin'), SKILL('weather', 'user')] };
      }
      return {};
    });
    await window.loadSkillsPage();

    const cards = document.querySelectorAll('.skill-card');
    expect(cards).toHaveLength(2);

    const builtinBadge = cards[0].querySelector('.origin-badge--builtin');
    expect(builtinBadge).not.toBeNull();
    expect(builtinBadge.textContent).toContain('内置');
    expect(builtinBadge.querySelector('.fa-cube')).not.toBeNull();
    // 用户安装卡片不得带内置徽标（一眼可辨，不混戴）
    expect(cards[0].querySelector('.origin-badge--user')).toBeNull();

    const userBadge = cards[1].querySelector('.origin-badge--user');
    expect(userBadge).not.toBeNull();
    expect(userBadge.textContent).toContain('安装');
    expect(userBadge.querySelector('.fa-arrow-down-to-bracket')).not.toBeNull();
    expect(cards[1].querySelector('.origin-badge--builtin')).toBeNull();

    // 注册表已填充：决策流/路由追踪可按名查找
    expect(window.skillOriginBadge('web-composite-search')).toContain('origin-badge--builtin');
    expect(window.skillOriginBadge('weather')).toContain('origin-badge--user');
  });

  it('origin 缺失（旧数据）时不渲染徽标，不抛错', async () => {
    window.api.fetch = vi.fn(async () => ({
      skills: [{ name: 'legacy', description: '无 origin 字段', enabled: true, weight: 0.5,
                 platforms: [], intent_keywords: [], allowed_tools: [], has_config: false,
                 source_path: '/tmp/legacy/SKILL.md' }],
    }));
    await window.loadSkillsPage();
    const card = document.querySelector('.skill-card');
    expect(card).not.toBeNull();
    expect(card.querySelector('.origin-badge')).toBeNull();
    expect(card.textContent).toContain('legacy');
  });
});
