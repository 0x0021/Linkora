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

    // ============ Chart.js 按需懒加载（F-H7） ============
    // 生产态原先在 index.html 用 <script defer> 直接拉 chart.umd.min.js（~205KB），
    // 每页首屏都下载。改为「用到才加载」：首次进入含图表的页才动态注入脚本，
    // 之后缓存 Promise，重复进入不再重复下载。仅当 canvas 存在且真正要 new Chart 时调用。
    let _chartLoadPromise = null;
    function loadChart() {
        if (typeof global.Chart !== 'undefined') return Promise.resolve(global.Chart);
        if (_chartLoadPromise) return _chartLoadPromise;
        _chartLoadPromise = new Promise(function (resolve, reject) {
            const s = document.createElement('script');
            s.src = '/static/vendor/chart.umd.min.js';
            s.async = true;
            s.onload = function () { resolve(global.Chart); };
            s.onerror = function () {
                _chartLoadPromise = null;
                reject(new Error('Chart.js 加载失败'));
            };
            document.head.appendChild(s);
        });
        return _chartLoadPromise;
    }
    global.loadChart = loadChart;
})(window);
