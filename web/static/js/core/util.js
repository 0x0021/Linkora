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

    // ============ 消息内容纯文本清洗（仪表盘紧凑列表预览） ============
    // 钉钉/飞书落库的原始 content 混着大量机器占位符：mediaId、本地缓存路径、
    // OCR 区块标记、dws 下载提示、卡片/文件标签、app 裸 JSON……消息页用富文本
    // renderMsgContent 渲染（产 HTML 卡片），但仪表盘「最近消息」「决策追踪」
    // 是紧凑单行列表，需先清洗为纯文本再截断展示，避免 mediaId 这类噪声刷屏。

    const _MSG_TYPE_LABELS = {
        text: '文本', image: '图片', mixed: '图文', voice: '语音', video: '视频',
        file: '文件', link: '链接', app: '应用', call: '通话', recall: '撤回',
        system: '系统', interactive: '卡片', post: '动态', location: '位置'
    };

    // 内容被清洗为空时，按消息类型给一个最小可读标签兜底
    const _EMPTY_FALLBACK = {
        image: '[图片]', mixed: '[图片]', video: '[视频]', voice: '[语音]',
        file: '[文件]', app: '[应用消息]', call: '[通话]', link: '[链接]',
        interactive: '[卡片]'
    };

    /** 消息类型英文枚举 → 中文标签（未知值原样返回，空值按文本处理） */
    function msgTypeLabel(t) {
        const k = String(t === null || t === undefined ? '' : t).toLowerCase();
        if (!k) return '文本';
        return Object.prototype.hasOwnProperty.call(_MSG_TYPE_LABELS, k) ? _MSG_TYPE_LABELS[k] : k;
    }

    function _emptyFallback(msgType) {
        const k = String(msgType === null || msgType === undefined ? '' : msgType).toLowerCase();
        return _EMPTY_FALLBACK[k] || '';
    }

    /**
     * 把原始消息内容清洗为人类可读的纯文本预览。
     * 处理：app 裸 JSON、本地缓存路径、mediaId 占位符、dws 下载提示、
     *      OCR 区块标记、<card>/<file> 标签、连续空白。
     * @param {string} raw 原始内容
     * @param {string} [msgType] 清洗后为空时的兜底类型
     * @returns {string} 清洗后的纯文本（可能为空串）
     */
    function cleanMsgPreview(raw, msgType) {
        if (raw === null || raw === undefined) return _emptyFallback(msgType);
        let s = String(raw);
        if (!s.trim()) return _emptyFallback(msgType);

        // 0) app 类型裸 JSON：{"textContent":{"text":"..."},"contentType":N}
        s = s.replace(
            /\{\s*"textContent"\s*:\s*\{\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"[\s\S]*?"contentType"\s*:\s*\d+\s*\}/g,
            function (m, txt) {
                return txt ? txt.replace(/\\n/g, '\n').replace(/\\"/g, '"') : '';
            }
        );

        // 1) 本地缓存路径标记：[本地图片] <path> / [本地文件] <path>
        s = s.replace(/\[本地(?:图片|文件)\]\s*\S+/g, '');

        // 2) 媒体占位符（带 mediaId）→ 中文标签
        s = s.replace(/\[图片消息\]\(\s*mediaId=[^)]*\)/g, '[图片]');
        s = s.replace(/\[视频消息\]\(\s*mediaId=[^)]*\)(?:\s*fileName=[^\s]*)?(?:\s*url:\s*\S*)?/g, '[视频]');
        s = s.replace(/\[语音消息\]\(\s*mediaId=[^)]*\)/g, '[语音]');
        s = s.replace(/\[文件\]\(\s*mediaId=[^)]*\)/g, '[文件]');
        // 裸标记（历史数据可能无 mediaId）兜底
        s = s.replace(/\[图片消息\]/g, '[图片]');
        s = s.replace(/\[视频消息\]/g, '[视频]');
        s = s.replace(/\[语音消息\]/g, '[语音]');
        s = s.replace(/\[文件消息\]/g, '[文件]');

        // 3) dws 下载提示尾巴
        s = s.replace(/注意：如需下载使用\s*dws\s+chat\s+message\s+download-media\s*命令下载/g, '');

        // 4) OCR 区块标记与占位
        s = s.replace(/————\s*图片识别内容\s*————/g, '');
        s = s.replace(/————\s*图片识别内容结束\s*————/g, '');
        s = s.replace(/\[图片识别中[^\]]*\]/g, '');
        s = s.replace(/\[图片识别内容\]/g, '');
        s = s.replace(/【图片内容】/g, '');
        s = s.replace(/<\[图片(?:识别中)?[^\]]*\]>/g, '[图片]');

        // 5) 卡片标签：<card title="X">…</card> → [卡片] X
        s = s.replace(/<card\s+title="([^"]*)"[^>]*>/g, function (m, t) {
            return t ? '[卡片] ' + t + '\n' : '';
        });
        s = s.replace(/<\/card>/g, '');

        // 6) <file key="..." name="X"/> → [文件] X
        s = s.replace(/<file\b[^>]*?name="([^"]*)"[^>]*\/>/g, function (m, n) {
            return n ? '[文件] ' + n : '[文件]';
        });
        s = s.replace(/<file\b[^>]*\/>/g, '[文件]');

        // 7) 折叠空白 + 合并相邻重复的媒体标签
        s = s.replace(/[ \t]+/g, ' ');
        s = s.replace(/[ \t]*\n[ \t]*/g, '\n');
        s = s.replace(/\n{2,}/g, '\n');
        s = s.replace(/(\[(?:图片|视频|语音|文件)\])(?:\s*\1)+/g, '$1');
        s = s.trim();

        return s || _emptyFallback(msgType);
    }

    /**
     * 剥离钉钉/飞书原始消息里的机器占位符，保留用户真实文字。
     * 用于会话详情页富文本渲染前的预处理——与 cleanMsgPreview（紧凑列表用、会插入
     * [图片]/[视频] 占位）不同，这里不插入占位，因为图片/视频由 image_path_map 单独渲染。
     *
     * 剥离对象：OCR 区块分隔行、[图片消息](mediaId=…)/[本地图片] <path> 等媒体原始标记、
     * 裸 [图片识别中…]、钉钉 dws 下载提示。保留真实文字（如“可以了”“老徐…”）。
     */
    function cleanContentNoise(raw) {
        if (!raw) return raw;
        return String(raw)
            // OCR 区块分隔行（———— 图片识别内容 ———— / —— 图片识别内容结束 ——）
            .replace(/————\s*图片识别内容(开始|结束)?\s*————/g, '')
            // 媒体原始占位符（整行移除；图片/语音/视频由媒体渲染器单独处理）
            .replace(/\[图片消息\]\(\s*mediaId=[^)]*\)/g, '')
            .replace(/\[语音消息\]\(\s*mediaId=[^)]*\)/g, '')
            .replace(/\[视频消息\][^\n]*/g, '')
            .replace(/\[本地图片\][^\n]*/g, '')
            .replace(/\[本地文件\][^\n]*/g, '')
            // 裸 [图片识别中…]（区别于 <[图片识别中]> 占位，后者交给 renderMsgContent）
            .replace(/(?<!<)\[图片识别中[^\]]*\]/g, '')
            // 钉钉 dws 下载提示（语音/视频/文件尾部）
            .replace(/\s*注意：如需下载使用[^\n]*/g, '')
            // 折叠多余空行
            .replace(/\n{3,}/g, '\n\n')
            .replace(/^\n+|\n+$/g, '');
    }

    global.cleanMsgPreview = cleanMsgPreview;
    global.cleanContentNoise = cleanContentNoise;
    global.msgTypeLabel = msgTypeLabel;

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
