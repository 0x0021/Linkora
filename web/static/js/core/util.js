// core/util.js — 全局通用工具函数单一来源
// 在所有页面脚本之前加载（模板中紧随 core/store.js），消除各页重复定义、靠加载顺序覆盖的脆弱性。
(function (global) {
    'use strict';

    function escapeHtml(text) {
        if (text === null || text === undefined) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function setText(id, text) {
        const el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    global.escapeHtml = escapeHtml;
    global.setText = setText;
})(window);
