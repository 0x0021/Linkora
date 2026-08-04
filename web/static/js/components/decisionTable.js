// ============ components/decisionTable.js ============
// 统一决策记录渲染（消除 dashboard 与 intent 两页重复且一致的决策行模板）
// 依赖：全局 escapeHtml（core/app.js）
//
// 用法：
//   renderDecisionFeed('decisions-feed', list, { emptyText: '暂无决策记录' });
//   renderDecisionFeed('decisions-top-list', list, { max: 2, emptyText: '暂无决策记录' });  // dashboard lite

(function (global) {
  'use strict';

  function actionLabel(a) {
    return ({ 'skip': '跳过', 'reply-rule': '规则回复', 'llm': 'LLM 处理' })[a] || a;
  }

  function skillSourceLabel(s) {
    return ({ 'explicit': '显式', 'intent': '意图', 'keyword': '关键词' })[s] || s;
  }

  // tool 标签截断：超 max 个时只显示前 max 个 + "+N"，悬停展开全部
  function renderToolsCapped(tools, max) {
    max = max || 3;
    const arr = Array.isArray(tools) ? tools : [];
    if (!arr.length) return '';
    if (arr.length <= max) {
      return `<div class="dec-tools">${arr.map(t => `<span class="tam-tag">${escapeHtml(t)}</span>`).join('')}</div>`;
    }
    const shown = arr.slice(0, max).map(t => `<span class="tam-tag">${escapeHtml(t)}</span>`).join('');
    return `<div class="dec-tools">
        ${shown}
        <span class="tam-tag tam-tag-more" tabindex="0" title="${escapeHtml(arr.slice(max).join('、'))}">+${arr.length - max}</span>
    </div>`;
  }

  function fmtTs(ts) {
    if (typeof global.formatTsLocal === 'function') return global.formatTsLocal(ts);
    return escapeHtml(String(ts || '').slice(0, 16));
  }

  // 单行决策 HTML（dashboard lite 与 intent full 共用同一结构）
  function renderDecisionRow(d) {
    const ts = fmtTs(d.ts);
    const intentPill = d.intent ? `<span class="pill pill-intent">${escapeHtml(d.intent)}</span>` : '';
    const modePill = d.routing_mode ? `<span class="pill pill-mode">${escapeHtml(d.routing_mode)}</span>` : '';
    const skillLabel = d.skill_name ? `${escapeHtml(d.skill_name)}${d.skill_source ? ' · ' + skillSourceLabel(d.skill_source) : ''}` : '';
    const skillPill = skillLabel ? `<span class="pill pill-skill" title="技能来源: ${escapeHtml(d.skill_source || '')}">⚡ ${skillLabel}</span>` : '';
    const toolsHtml = renderToolsCapped(d.routed_tools, 3);
    const reply = d.reply_preview ? `<div class="dec-reply">↳ ${escapeHtml(d.reply_preview)}</div>` : '';
    return `
        <div class="decision-row">
            <div class="dec-ts">${escapeHtml(ts)}</div>
            <div class="dec-main">
                <div class="dec-line1">
                    <span class="pill pill-${escapeHtml(d.action)}">${actionLabel(d.action)}</span>
                    ${intentPill} ${modePill} ${skillPill}
                    <span class="dec-sender">${escapeHtml(d.sender || '—')}</span>
                    <span class="dec-chat">@ ${escapeHtml(d.chat || '')}</span>
                </div>
                <div class="dec-content">${escapeHtml(d.content || '')}</div>
                ${toolsHtml}${reply}
            </div>
        </div>`;
  }

  // 渲染整段决策流（含空态与数量上限）
  function renderDecisionFeed(containerId, decisions, opts) {
    const el = typeof containerId === 'string' ? document.getElementById(containerId) : containerId;
    if (!el) return null;
    const list = Array.isArray(decisions) ? decisions : [];
    const max = opts && opts.max ? opts.max : 0;
    const shown = max > 0 ? list.slice(0, max) : list;
    if (shown.length === 0) {
      el.innerHTML = `<div class="empty-state" style="padding:16px;text-align:center;color:var(--text-tertiary);">
        <i class="fa-solid fa-message" style="font-size:1.5rem;opacity:.4;"></i>
        <p style="margin-top:.4rem;">${escapeHtml((opts && opts.emptyText) || '暂无决策记录')}</p>
      </div>`;
      return el;
    }
    el.innerHTML = shown.map(renderDecisionRow).join('');
    return el;
  }

  global.DecisionTable = { renderRow: renderDecisionRow, renderFeed: renderDecisionFeed };
  global.renderDecisionFeed = renderDecisionFeed;
  global.renderDecisionRow = renderDecisionRow;
})(window);
