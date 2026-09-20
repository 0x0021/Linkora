// dashboard 确定性渲染函数真实 DOM 测试（Phase 2 质量护栏）
// 验证「数据 → DOM」的核心可视化逻辑：消息类型分布、Hero sparkline、Top 发送者、
// 以及日志/消息行的 HTML 片段生成。这些函数经 dashboard.js 末尾的 window.Linkora 桥接暴露。
// 不测整页 loadDashboardData（含 api 取数 + canvas，需整页 mock），只测可独立断言的渲染函数。
import { describe, it, expect, beforeAll, beforeEach, afterAll, vi } from 'vitest';

let L;
beforeAll(async () => {
  // dashboard.js 模块级有 setInterval(fitDashboardToViewport, 2500) 副作用，
  // 用 fake timers 冻结，避免定时器泄漏 / 干扰测试进程退出。
  vi.useFakeTimers();
  // 加载整个页面脚本，触发 window.Linkora 桥接
  await import('../pages/dashboard.js');
  expect(window.Linkora).toBeTruthy();
  L = window.Linkora;
});

afterAll(() => {
  vi.useRealTimers();
});

describe('renderMsgTypeChart 消息类型分布', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="msgtype-chart-wrap"></div>
      <div id="msgtype-total-hint"></div>
      <div id="chart-msg-types-skeleton"></div>`;
  });

  it('按 msgTypes 渲染堆叠段与图例，hint 显示总数', () => {
    L.renderMsgTypeChart([
      { msg_type: '私信', cnt: 50 },
      { msg_type: '群消息', cnt: 30 },
      { msg_type: '系统通知', cnt: 20 },
    ]);
    const wrap = document.getElementById('msgtype-chart-wrap');
    expect(wrap.querySelectorAll('.mt-seg')).toHaveLength(3);
    expect(wrap.querySelectorAll('.mt-row')).toHaveLength(3);
    expect(document.getElementById('msgtype-total-hint').textContent).toContain('100');
    // 名称按 fallback（msgTypeLabel 未注入）原样出现
    expect(wrap.textContent).toContain('私信');
    expect(wrap.textContent).toContain('群消息');
  });

  it('空数据仍渲染容器但无段', () => {
    L.renderMsgTypeChart([]);
    const wrap = document.getElementById('msgtype-chart-wrap');
    expect(wrap.querySelectorAll('.mt-seg')).toHaveLength(0);
    expect(wrap.querySelectorAll('.mt-row')).toHaveLength(0);
    // 空数据 total 被归一为 1（renderMsgTypeChart: || 1 避免除 0），hint 显示「共 1 条」
    expect(document.getElementById('msgtype-total-hint').textContent).toContain('共');
  });
});

describe('renderHeroSparkline Hero 趋势', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="stat-messages-spark">
        <svg class="ov-spark-line"></svg>
        <svg class="ov-spark-area"></svg>
        <g class="ov-spark-dots"></g>
      </div>
      <div id="stat-messages-spark-axis"></div>
      <div id="stat-messages-today"></div>`;
  });

  it('有趋势时折线 path 以 M 开头并含点，axis 含日期', () => {
    L.renderHeroSparkline([
      { day: '2026-09-10', cnt: 3 },
      { day: '2026-09-11', cnt: 7 },
      { day: '2026-09-12', cnt: 5 },
    ]);
    const line = document.querySelector('.ov-spark-line');
    const d = line.getAttribute('d');
    expect(d.startsWith('M')).toBe(true);
    expect(d).toContain('L');
    expect(document.getElementById('stat-messages-spark-axis').textContent).toContain('09-10');
    expect(document.getElementById('stat-messages-spark-axis').textContent).toContain('09-12');
    // 今日 delta（最后 vs 倒数第二）
    expect(document.getElementById('stat-messages-today').textContent).toContain('今日');
  });

  it('空趋势显示暂无数据', () => {
    L.renderHeroSparkline([]);
    expect(document.getElementById('stat-messages-spark-axis').textContent).toContain('暂无数据');
  });
});

describe('renderTopSenders Top 发送者', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="top-senders-list"></div>';
  });

  it('渲染最多 5 行，含排名/名称/计数', () => {
    L.renderTopSenders([
      { sender_name: '张三', cnt: 12 },
      { sender_name: '李四', cnt: 9 },
      { sender_name: '王五', cnt: 1 },
    ]);
    const list = document.getElementById('top-senders-list');
    const rows = list.querySelectorAll('.kw-top-item');
    expect(rows).toHaveLength(3);
    expect(rows[0].querySelector('.kw-top-pattern').textContent).toBe('张三');
    expect(rows[0].querySelector('.kw-top-count').textContent).toBe('12');
    // 第一名带 top1 标识
    expect(rows[0].querySelector('.kw-top-rank').classList.contains('top1')).toBe(true);
  });

  it('空数据渲染空态', () => {
    L.renderTopSenders([]);
    const list = document.getElementById('top-senders-list');
    expect(list.querySelector('.empty-state')).not.toBeNull();
    expect(list.textContent).toContain('暂无数据');
  });
});

describe('renderLogItem 消息行 HTML（纯函数）', () => {
  it('含发送者 / 清洗后内容 / msg_type fallback', () => {
    const html = L.renderLogItem({
      id: 'm1',
      sender_name: '赵六',
      receiver_name: '群A',
      content: '你好世界',
      msg_type: 'text',
      timestamp: '2026-09-12T04:30:00', // 无时区：避免 CI/本地时区偏移影响 fmtDashTime 断言
    });
    expect(html).toContain('赵六');
    expect(html).toContain('你好世界');
    expect(html).toContain('text'); // msgTypeLabel 未注入，fallback 原样
    expect(html).toContain('04:30'); // fmtDashTime 本地格式化
  });

  it('is_bot 标记 robot 图标', () => {
    const html = L.renderLogItem({ sender_name: 'B', content: 'x', is_bot: true, msg_type: 'text', timestamp: '2026-09-12T04:30:00+00:00' });
    expect(html).toContain('fa-robot');
  });

  it('长内容超 60 字截断并带省略号', () => {
    const long = '甲'.repeat(120);
    const html = L.renderLogItem({ sender_name: 'B', content: long, msg_type: 'text', timestamp: '2026-09-12T04:30:00+00:00' });
    expect(html).toContain('...');
    // 截断后预览 ≤ 63 字符（60 + ...）
    const previewSpan = html.match(/log-item-text[^>]*>([^<]*)</);
    expect(previewSpan[1].length).toBeLessThanOrEqual(63);
  });
});

describe('renderLogLine 日志行 HTML（纯函数）', () => {
  it('含 level / logger / message，且 message 被转义', () => {
    const html = L.renderLogLine({ level: 'ERROR', ts: '2026-09-12T04:30:00Z', logger: 'src.foo', message: 'a <b> b' });
    expect(html).toContain('ERROR');
    expect(html).toContain('foo'); // 去掉 src. 前缀
    expect(html).toContain('a &lt;b&gt; b'); // 转义
  });
});
