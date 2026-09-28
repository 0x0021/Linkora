// web/static/js/core/ui.js
// 共享 UI 基础组件：空态 / 错误态 / 加载骨架 的统一渲染，消除各页面重复内联 HTML。
//
// 经典 <script>（非 module）：renderEmptyState / renderErrorState / renderThreadSkeleton
// 挂全局作用域，与 escapeHtml / iconize 同款用法；同时桥接到 window.Linkora 命名空间，
// 便于 vitest 经 `import` 取用（见 web/static/js/tests/ui.render.test.js）。
//
// 设计要点：
//  - message 一律经 escapeHtml 转义（即便当前调用都传纯文本，也守住 XSS 卫生），
//    icon 是可信字面量（emoji / HTML 实体），不做转义。
//  - 输出结构与各页原有内联空态逐字节一致，迁移不改变任何渲染结果。

function renderEmptyState(message, opts) {
  opts = opts || {};
  const cls = opts.className ? ' ' + opts.className : '';
  const style = opts.style ? ' style="' + opts.style + '"' : '';
  const safeMsg = escapeHtml(message == null ? '' : String(message));
  if (opts.noIcon) {
    return '<div class="empty-state' + cls + '"' + style + '><p>' + safeMsg + '</p></div>';
  }
  const icon = opts.icon != null ? opts.icon : '💬';
  const iconStyle = opts.iconStyle ? ' style="' + opts.iconStyle + '"' : '';
  return (
    '<div class="empty-state' + cls + '"' + style +
    '><div class="empty-icon"' + iconStyle + '>' + icon + '</div><p>' + safeMsg + '</p></div>'
  );
}

// 错误态：空态的语义化包装，默认用警告图标（⚠，HTML 实体）。
function renderErrorState(message, opts) {
  const o = Object.assign({}, opts || {});
  if (o.icon == null) o.icon = '&#x26A0;';
  return renderEmptyState(message, o);
}

// 消息线程加载骨架（与 messages.js 原有 thread-skeleton 结构一致）。
function renderThreadSkeleton(count) {
  const n = count || 6;
  let rows = '';
  for (let i = 0; i < n; i++) {
    rows +=
      '<div class="sk-msg-row">' +
      '<div class="skeleton skeleton-avatar"></div>' +
      '<div class="skeleton-bubble">' +
      '<div class="skeleton skeleton-line" style="width:30%"></div>' +
      '<div class="skeleton skeleton-line" style="width:70%"></div>' +
      '</div>' +
      '</div>';
  }
  return '<div class="thread-skeleton">' + rows + '</div>';
}

// 暴露为全局：经典 <script>（浏览器）下函数本就全局；此处显式挂 globalThis，
// 确保 vitest(ESM) 下跨文件裸引用也能解析（与 util.js 的 global.escapeHtml 同思路）。
globalThis.renderEmptyState = renderEmptyState;
globalThis.renderErrorState = renderErrorState;
globalThis.renderThreadSkeleton = renderThreadSkeleton;
// 桥接到 window.Linkora 命名空间（defensive：app.js 后续会补齐 actions）。
window.Linkora = window.Linkora || {};
if (!window.Linkora.actions) window.Linkora.actions = {};
Object.assign(window.Linkora, { renderEmptyState, renderErrorState, renderThreadSkeleton });
