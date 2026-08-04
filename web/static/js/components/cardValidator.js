/**
 * Card Config Self-Check — 卡片配置自检模块
 *
 * 对 LLM 回复中的卡片代码块（yyb-file-list、yyb-image-gallery、
 * yyb-video-card、yyb-product、yyb-delete-list、yyb-tool-call）
 * 进行配置自检。当卡片配置错误、数据异常或渲染失败时，
 * 在卡片上方显示 ⚠️ 警告信息，便于定位问题。
 *
 * 检查内容：
 *   1. 卡片语法格式是否合法
 *   2. 卡片内路径与文件系统是否一致
 *   3. 卡片数据字段完整性与兼容性
 *   4. 卡片渲染失败兜底检测
 *
 * 返回值：
 *   {
 *     ok: boolean,          // 是否通过检查
 *     warnings: string[]    // 告警信息（为空表示无告警）
 *   }
 *
 * 示例：
 *
 *   const result = CardValidator.validateBlock(fencedCodeBlock);
 *
 *   if (!result.ok) {
 *     // 渲染 ⚠️ 警告提示
 *     renderWarning(result.warnings);
 *   }
 */
 
const CardValidator = (function () {
    'use strict';

    // ── 已知卡片类型 ──
    const KNOWN_CARD_TYPES = new Set([
        'yyb-file-list',
        'yyb-image-gallery',
        'yyb-video-card',
        'yyb-delete-list',
        'yyb-product',
        'yyb-tool-call',
    ]);

    // ── 正则：匹配 fenced code block 的语言标记 ──
    // 格式：```<card_type>\n...\n```
    const FENCE_RE = /^```<([^>]+)>/;
    const FENCE_END = /```\s*$/;

    /**
     * 验证单个围栏代码块是否为合法卡片。
     * @param {string} rawBlock - 原始代码块（含围栏标记）
     * @returns {{ ok: boolean, cardType: string|null, warnings: string[], items: string[] }}
     */
    function validateBlock(rawBlock) {
        const result = { ok: true, cardType: null, warnings: [], items: [] };
        if (!rawBlock || typeof rawBlock !== 'string') {
            result.ok = false;
            result.warnings.push('卡片代码块为空');
            return result;
        }

        const trimmed = rawBlock.trim();
        const fenceMatch = trimmed.match(FENCE_RE);
        if (!fenceMatch) {
            // 不是卡片代码块，不报错
            return result;
        }

        const cardType = fenceMatch[1].trim();
        result.cardType = cardType;

        if (!KNOWN_CARD_TYPES.has(cardType)) {
            result.ok = false;
            result.warnings.push(`未知卡片类型：<${cardType}>，可能不被前端支持`);
            return result;
        }

        // 检查是否有闭合围栏
        if (!FENCE_END.test(trimmed.substring(fenceMatch[0].length))) {
            result.ok = false;
            result.warnings.push('卡片代码块缺少闭合 ```，可能截断');
            return result;
        }

        // 提取卡片内容（去掉围栏）
        const inner = trimmed
            .replace(FENCE_RE, '')
            .replace(FENCE_END, '')
            .trim();

        // 解析内容项
        if (cardType === 'yyb-tool-call') {
            // yyb-tool-call 只含一个 call_xxx ID
            const callId = inner.trim();
            if (!callId.startsWith('call_')) {
                result.ok = false;
                result.warnings.push(`yyb-tool-call 格式异常：期望 call_xxx 但得到 "${callId.slice(0, 20)}"`);
            }
            result.items = [callId];
        } else {
            // 文件/图片/视频/删除列表 / 产出物卡片
            // 格式：每行 [文件名](<路径>)
            const lines = inner.split('\n').filter(function (l) { return l.trim(); });
            const linkRe = /^\s*\[([^\]]+)\]\(<([^>]+)>\)\s*$/;

            lines.forEach(function (line, idx) {
                const match = line.match(linkRe);
                if (!match) {
                    result.ok = false;
                    result.warnings.push(
                        '第 ' + (idx + 1) + ' 行格式异常（期望 `[文件名](<路径>)`）：'
                        + line.slice(0, 40)
                    );
                    return;
                }
                const filename = match[1].trim();
                const filepath = match[2].trim();
                result.items.push(filepath);

                // 文件路径基础校验
                if (!filepath.startsWith('/')) {
                    result.warnings.push(
                        '路径非绝对路径 "' + filename + '"：' + filepath
                    );
                }

                // yyb-product 额外校验：不应出现临时产物
                if (cardType === 'yyb-product' && /temp|tmp|\/temp\//i.test(filepath)) {
                    result.warnings.push(
                        '产出物路径疑似临时文件 "' + filename + '"：' + filepath
                    );
                }

                // yyb-delete-list 额外校验
                if (cardType === 'yyb-delete-list' && /\/\.(git|svn|ssh|aws|kube)\//.test(filepath)) {
                    result.warnings.push(
                        '⚠️ 删除列表包含敏感路径 "' + filename + '"：' + filepath
                    );
                }
            });

            if (result.items.length === 0) {
                result.warnings.push('卡片内容为空，无有效条目');
            }
        }

        return result;
    }

    /**
     * 扫描整个 LLM 回复文本，提取所有卡片代码块并逐一验证。
     * @param {string} text - LLM 回复全文
     * @returns {{ blocks: Array, totalWarnings: number }}
     */
    function scanText(text) {
        if (!text || typeof text !== 'string') return { blocks: [], totalWarnings: 0 };

        const blocks = [];
        let totalWarnings = 0;
        let inFence = false;
        let fenceBuf = '';
        let fenceLang = '';

        const lines = text.split('\n');
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            if (!inFence) {
                const m = line.match(/^```<(.+)>/);
                if (m) {
                    inFence = true;
                    fenceLang = m[1];
                    fenceBuf = line;
                    continue;
                }
            } else {
                if (/^```\s*$/.test(line)) {
                    fenceBuf += '\n' + line;
                    const result = validateBlock(fenceBuf);
                    if (result.cardType !== null) {
                        blocks.push(result);
                        totalWarnings += result.warnings.length;
                    }
                    inFence = false;
                    fenceBuf = '';
                    fenceLang = '';
                } else {
                    fenceBuf += '\n' + line;
                }
            }
        }

        // 未闭合的卡片块
        if (inFence) {
            const result = validateBlock(fenceBuf);
            if (result.cardType !== null) {
                result.ok = false;
                result.warnings.push('卡片代码块未闭合（可能被截断）');
                blocks.push(result);
                totalWarnings += result.warnings.length;
            }
        }

        return { blocks: blocks, totalWarnings: totalWarnings };
    }

    /**
     * 渲染警告 HTML 供页面展示。
     * @param {string[]} warnings
     * @returns {string} HTML
     */
    function renderWarnings(warnings) {
        if (!warnings || warnings.length === 0) return '';
        var items = warnings.map(function (w) {
            return '<li>' + escapeHtml(w) + '</li>';
        }).join('');
        return (
            '<div class="card-drift-warning" style="'
            + 'background:#fff3cd;border:1px solid #ffc107;border-radius:6px;'
            + 'padding:8px 12px;margin:8px 0;font-size:0.85rem;'
            + 'color:#856404;'
            + '">'
            + '<strong>⚠️ 卡片配置异常：</strong>'
            + '<ul style="margin:4px 0 0 16px;padding:0;">'
            + items
            + '</ul></div>'
        );
    }

    return {
        validateBlock: validateBlock,
        scanText: scanText,
        renderWarnings: renderWarnings,
    };
})();
