// 回归测试：决策流「最新在前」排序（2026-10-05 首页决策追踪不显示最新消息的根因）。
//
// 后端 tracker.recent() 返回**时间正序**（旧→新），而 renderDecisionFeed 原先
// 直接 slice(0, max) 取前 N 条 → 取到的是**最旧**几条。轮询路径（dashboard.js 的
// fetchDashboardStream）未像首次加载那样手工 reverse，故首页「决策追踪」看起来
// "没有最新消息"。修复把倒序下沉到组件内部，本测试锁住该行为。
import { describe, it, expect, beforeEach } from 'vitest';

describe('renderDecisionFeed 最新在前排序', () => {
  let container;

  beforeEach(async () => {
    document.body.innerHTML = '<div id="dec-top"></div>';
    container = document.getElementById('dec-top');
    await import('../components/decisionTable.js');
  });

  const tsList = () =>
    Array.from(container.querySelectorAll('.dec-ts')).map((el) => el.textContent.trim());

  it('传入正序数据时，max 截取的是最新的 N 条（而非最旧）', () => {
    // 后端正序：最旧 → 最新
    const list = [
      { ts: '2026-10-05T01:00:00+00:00', content: '最旧', action: 'skip', sender: 'a' },
      { ts: '2026-10-05T02:00:00+00:00', content: '中间', action: 'llm', sender: 'b' },
      { ts: '2026-10-05T03:00:00+00:00', content: '最新', action: 'llm', sender: 'c' },
    ];
    window.renderDecisionFeed('dec-top', list, { max: 2 });
    const rows = container.querySelectorAll('.decision-row');
    expect(rows).toHaveLength(2);
    const body = container.textContent;
    // 最新的两条应在其中，最旧的不应出现
    expect(body).toContain('最新');
    expect(body).toContain('中间');
    expect(body).not.toContain('最旧');
  });

  it('不传 max 时也按时间倒序（最新在第一行）', () => {
    const list = [
      { ts: '2026-10-05T01:00:00+00:00', content: '最旧', action: 'skip', sender: 'a' },
      { ts: '2026-10-05T02:00:00+00:00', content: '最新', action: 'llm', sender: 'b' },
    ];
    window.renderDecisionFeed('dec-top', list, {});
    const rows = container.querySelectorAll('.decision-row');
    expect(rows).toHaveLength(2);
    // 第一行应是最新那条
    expect(rows[0].textContent).toContain('最新');
    expect(rows[1].textContent).toContain('最旧');
  });

  it('已倒序传入时结果不变（幂等，不会因组件内倒序而反转为最旧在前）', () => {
    const newestFirst = [
      { ts: '2026-10-05T03:00:00+00:00', content: '最新', action: 'llm', sender: 'c' },
      { ts: '2026-10-05T01:00:00+00:00', content: '最旧', action: 'skip', sender: 'a' },
    ];
    window.renderDecisionFeed('dec-top', newestFirst, { max: 2 });
    const rows = container.querySelectorAll('.decision-row');
    expect(rows[0].textContent).toContain('最新');
  });

  it('空数组渲染空态', () => {
    window.renderDecisionFeed('dec-top', [], { max: 2, emptyText: '暂无决策记录' });
    expect(container.querySelectorAll('.decision-row')).toHaveLength(0);
    expect(container.textContent).toContain('暂无决策记录');
  });
});
