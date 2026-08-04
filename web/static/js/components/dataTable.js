// ============ components/dataTable.js ============
// 通用数据表组件（替代各页手写 table 拼接 + 空态）
// 依赖：全局 escapeHtml
//
// 用法：
//   renderDataTable('cq-citations-table', {
//     columns: [
//       { key: 'sender_name', label: '发送者' },
//       { key: 'reply_preview', label: '回复预览',
//         render: (r) => escapeHtml((r.reply_preview || '').slice(0, 40)) },
//     ],
//     rows: items,
//     emptyText: '暂无记录',
//   });

(function (global) {
  'use strict';

  function renderDataTable(target, opts) {
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;
    const rows = opts.rows || [];
    if (rows.length === 0) {
      el.innerHTML = `<div class="metrics-empty">${escapeHtml(opts.emptyText || '暂无数据')}</div>`;
      return el;
    }
    const cols = opts.columns || [];
    const thead = cols.map(c => `<th class="${c.cls || ''}">${escapeHtml(c.label || '')}</th>`).join('');
    const body = rows.map((r, i) => {
      const tds = cols.map(c => {
        const raw = typeof c.render === 'function' ? c.render(r, i) : (r[c.key] != null ? r[c.key] : '—');
        return `<td class="${c.tdCls || ''}">${raw}</td>`;
      }).join('');
      return `<tr>${tds}</tr>`;
    }).join('');
    el.innerHTML = `<div class="table-container"><table class="data-table"><thead><tr>${thead}</tr></thead><tbody>${body}</tbody></table></div>`;
    return el;
  }

  global.DataTable = { render: renderDataTable };
  global.renderDataTable = renderDataTable;
})(window);
