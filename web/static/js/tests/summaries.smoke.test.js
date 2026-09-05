// summaries.js 冒烟测试：对话摘要卡片渲染
//
// 重点覆盖 2026-09-05 的线上事故——单条摘要是按日期分段的多行文本
// （【对话摘要】YYYY-MM-DD 分段），而 digest 逐行解析时只认
// 「• **名称**：内容」单行；若不把续行回接到上一条目，卡片就只剩首行，
// 当首行恰是纯日期头时看上去就只有一个日期、正文全丢。
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));

describe('summaries.js (digest 卡片渲染)', () => {
    beforeEach(async () => {
        vi.resetModules();
        // 这些由 core/app.js 在浏览器里提供，jsdom 下需先补齐再加载页面脚本，
        // 让经典的 render* 函数能被调用而不崩（只关心渲染链路与输出形态，不校验 DOM）。
        globalThis.simpleMarkdown = (md) => {
            if (!md) return '';
            let html = globalThis.escapeHtml(md);
            html = html.replace(/\*\*([\s\S]*?)\*\*/g, '<strong>$1</strong>');
            return html.replace(/\n/g, '<br>');
        };
        globalThis.PLATFORM_LABEL = {};
        globalThis.PLATFORM_ICON = {};
        globalThis.extractTopics = () => [];
        globalThis.extractTodo = () => '';
        globalThis.avatarGradient = () => ['#aaaaaa', '#bbbbbb'];
        globalThis.avatarLetter = (n) => (n && n[0]) || '?';
        globalThis.formatTsLocal = () => '—';
        // formatSummaryBody 依赖 app.js 提供的全局（jsdom 下 stub）
        globalThis.renderDigestBlocks = (md) => `<div class="db-card">${md}</div>`;
        await import('../pages/summaries.js');
    });

    // 复刻 build_digest 的输出形态：条目间空行，单条摘要内部为多行
    const DIGEST = [
        '📋 今日对话摘要（共 2 段）',
        '',
        '• **张保丁**：2026-09-05',
        '张保丁发送了权限报表截图，请求我开通缺失权限。',
        '',
        '• **程海珍**：2026-08-18',
        '程海珍申请CRM账号。',
        '【对话摘要】2026-09-05',
        '程海珍请我帮忙开通VPN权限。',
    ].join('\n');

    it('多段摘要的续行必须回接到上一条目，不能只留首行', () => {
        const out = window.renderDigestCards(DIGEST, []);
        expect(out.cards).toContain('张保丁发送了权限报表截图');
        expect(out.cards).toContain('程海珍申请CRM账号');
        expect(out.cards).toContain('程海珍请我帮忙开通VPN权限');
    });

    it('日期小标渲染为 digest-sub-date 标签，且剥离首行冗余前缀', () => {
        const out = window.renderDigestCards(DIGEST, []);
        expect(out.cards).toContain('digest-sub-date');
        // 首行被 build_digest 剥掉前缀后残留的裸日期也要变成日期标签
        expect(out.cards).toContain('<span class="digest-sub-date">2026-09-05</span>');
        // 原样泄漏的「【对话摘要】」标记不应留在卡片里
        expect(out.cards).not.toContain('【对话摘要】');
    });

    it('每条摘要只生成一个卡片（续行不得被误当成新条目）', () => {
        const out = window.renderDigestCards(DIGEST, []);
        const cardCount = (out.cards.match(/class="digest-card"/g) || []).length;
        expect(cardCount).toBe(2);
    });

    it('未改动源文件仍是可被构建的源码（与 dist 同源）', () => {
        const src = readFileSync(join(here, '../pages/summaries.js'), 'utf8');
        expect(src).toContain('formatSummaryBody');
        expect(src).toContain('digest-sub-date');
        expect(src).toContain('window.renderDigestCards = renderDigestCards');
    });
});
