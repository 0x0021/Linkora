// dataTable 真实 DOM 渲染测试（Phase 2 质量护栏）
// 取代 setup.js 旧有的「万能假元素吞噬」模式：本文件依赖 setup 升级后的「真实优先」
// getElementById，在 jsdom 中真实注入容器并断言渲染产物（表格行数 / 单元格文本 / 空态 /
// 自定义 render / 缺失字段占位）。这是首个「渲染结果可被断言」的前端测试，为后续
// dashboard / messages 等整页渲染测试建立范本与基础设施。
import { describe, it, expect, beforeEach } from 'vitest';

describe('renderDataTable 真实 DOM 渲染', () => {
  let container;

  beforeEach(async () => {
    // 真实注入容器节点（setup 升级后 getElementById 真实优先，可定位到它）
    document.body.innerHTML = '<div id="tbl"></div>';
    container = document.getElementById('tbl');
    // 加载组件，挂到 window（IIFE 幂等，重复 import 无害）
    await import('../components/dataTable.js');
  });

  it('有数据时渲染 data-table，表头/行数/单元格正确', () => {
    window.renderDataTable('tbl', {
      columns: [
        { key: 'name', label: '名称' },
        { key: 'score', label: '分数' },
      ],
      rows: [
        { name: '甲', score: 10 },
        { name: '乙', score: 20 },
      ],
    });

    const table = container.querySelector('table.data-table');
    expect(table).not.toBeNull();
    expect(container.querySelectorAll('thead th')).toHaveLength(2);
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
    expect(container.querySelector('thead th').textContent).toBe('名称');
    // 首行首列 = 甲，首行第二列 = 10
    expect(container.querySelectorAll('tbody td')[0].textContent).toBe('甲');
    expect(container.querySelectorAll('tbody td')[1].textContent).toBe('10');
  });

  it('空 rows 渲染空态文案且不生成 table', () => {
    window.renderDataTable('tbl', {
      columns: [{ key: 'x', label: 'X' }],
      rows: [],
      emptyText: '暂无记录',
    });

    const empty = container.querySelector('.metrics-empty');
    expect(empty).not.toBeNull();
    expect(empty.textContent).toContain('暂无记录');
    expect(container.querySelector('table')).toBeNull();
  });

  it('column.render 自定义单元格内容', () => {
    window.renderDataTable('tbl', {
      columns: [{ key: 'v', label: 'V', render: (r) => `值=${r.v}` }],
      rows: [{ v: 5 }],
    });

    expect(container.querySelector('tbody td').textContent).toBe('值=5');
  });

  it('缺失字段渲染为破折号占位', () => {
    window.renderDataTable('tbl', {
      columns: [{ key: 'missing', label: '缺失' }],
      rows: [{}],
    });

    expect(container.querySelector('tbody td').textContent).toBe('—');
  });

  it('接受元素对象作为 target（不依赖 id 查找）', () => {
    window.renderDataTable(container, {
      columns: [{ key: 'k', label: 'K' }],
      rows: [{ k: '直接传元素' }],
    });

    expect(container.querySelectorAll('tbody tr')).toHaveLength(1);
    expect(container.querySelector('tbody td').textContent).toBe('直接传元素');
  });
});
