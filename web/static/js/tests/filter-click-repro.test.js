// 回归测试：过滤器 tab 的 data-args 在**构建产物（minify 后）**仍正确绑定变量。
//
// 背景（真实故障）：草稿审阅页与死信队列页的 tab 曾用
//   '<button data-args=\'["' + s + '"]\'>' + ...
// 拼接生成 data-args。构建时 esbuild --minify 把拼接重排成
//   `... data-args='["` + H + `"]' ...`
// ——`["` 与 `"]` 变成模板字面量、`+H+` 夹在中间，属性值被截断成 `["`，
// JSON.parse 失败 → runAction 传 undefined → 请求 status=undefined
// → 后端 WHERE status='undefined' → 列表被清空（"默认能加载、点过滤器就空"）。
//
// 修复：改用模板字符串（${} 作用域绑定，minify 安全）。
//
// 断言方式：对 minify 产物做文本检查 —— tab 的 data-args 必须形如
//   data-args='["${...}"]'   （变量在 ${} 内）
// 而不能是 data-args='["` 后接反引号/加号拼接（minify 错绑的标志）。
// 该断言直接命中根因，且不依赖 eval（eval 需还原 minified 片段的变量作用域，脆弱）。
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, writeFileSync, mkdtempSync, rmSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';
import { execFileSync } from 'child_process';

const ROOT = process.cwd();
const DIST = join(ROOT, 'web/static/dist');
const PAGES = join(ROOT, 'web/static/js/pages');
const ESBUILD_BIN = join(ROOT, 'node_modules/.bin/esbuild');

/** CLI 版 esbuild minify（jsdom 环境里 esbuild 的 JS API 不可用：TextEncoder 不继承 Uint8Array） */
function minify(src) {
  const dir = mkdtempSync(join(tmpdir(), 'esb-'));
  try {
    const inFile = join(dir, 'in.js');
    const outFile = join(dir, 'out.js');
    writeFileSync(inFile, src, 'utf8');
    execFileSync(ESBUILD_BIN, ['--minify', `--outfile=${outFile}`, inFile], { stdio: ['ignore', 'ignore', 'pipe'] });
    return readFileSync(outFile, 'utf8');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * 校验「tab 按钮」生成的 data-args 形态正确。
 *
 * 只针对 tab（筛选器按钮）——即同一段模板里同时出现 data-status 与 data-args 的位置。
 * 其他 data-args（列表行里的 item.id / "@el" 等）minify 处理正确、无错绑风险，不在检查范围。
 *
 * 合法形态：
 *   data-args='["${X}"]'  模板插值（安全）
 *   data-args='["all"]'   纯字面量（安全）
 * 非法形态（minify 错绑）：
 *   data-args='["` + X + `"]'  → data-args='[" 之后紧跟反引号
 */
function assertTabsUseTemplateInterpolation(code, label, minCount) {
  // 定位 tab 按钮：data-status= 与 data-args= 出现在同一段模板里
  const found = [];
  const segRe = /data-status=[\s\S]{0,160}?data-args='\["([^\s]{0,4})/g;
  let m;
  while ((m = segRe.exec(code)) !== null) {
    found.push({ after: m[1], ctx: m[0].slice(0, 90) });
  }

  expect(found.length, label + ': 应找到 tab 的 data-status+data-args 组合')
    .toBeGreaterThanOrEqual(minCount || 1);

  const DOLLAR_BRACE = '$' + '{';
  const bad = found.filter((f) => {
    const a = f.after;
    if (a.startsWith(DOLLAR_BRACE)) return false;           // 模板插值：安全
    if (a.startsWith('`')) return true;                      // minify 错绑：反引号拼接
    return !/^[A-Za-z_@][\w.@-]*["\]]/.test(a);             // 纯字面量：all / @el
  });

  expect(
    bad.map((f) => label + ': after=' + JSON.stringify(f.after) + ' ctx=' + JSON.stringify(f.ctx)),
    label + ': tab 的 data-args 形态异常。必须是模板插值 data-args=\'["${X}"]]\' '
    + '或纯字面量 data-args=\'["all"]\'。若 data-args=\'[" 之后紧跟反引号（形如 \'["` + X + `"]\'），'
    + '说明构建 minify 错绑了变量 → 属性值被截断 → JSON.parse 失败 → status=undefined → 后端返回空列表。',
  ).toEqual([]);
  return found.length;
}

/** 源码层：不得用字符串拼接生成 data-args（须为模板字符串插值） */
function assertSourceUsesTemplate(code, label) {
  const lines = code.split('\n');
  const offenders = [];
  lines.forEach((raw, i) => {
    // 跳过注释行（注释里会写反例说明，如「不可写成 data-args='["' + s + '"]'」）
    const trimmed = raw.trim();
    if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) return;

    const line = raw.split('//')[0]; // 行尾注释
    if (!line.includes('data-args')) return;
    if (line.includes('${')) return; // 模板插值：安全
    const seg = line.slice(line.indexOf('data-args'), line.indexOf('data-args') + 120);
    if (/\+\s*[A-Za-z_$][\w$]*\s*\+/.test(seg)) {
      offenders.push(`${i + 1}: ${line.trim().slice(0, 110)}`);
    }
  });
  expect(offenders, `${label}: 不应再用字符串拼接生成 data-args（须用模板字符串插值）`).toEqual([]);
}

describe('死信队列页：tab data-args 在构建产物中正确绑定（回归 status=undefined 清空列表）', () => {
  it('dist bundle 里 dlq tab 的 data-args 用模板插值', () => {
    const bundleName = readdirSync(DIST).find((f) => f.endsWith('.js'));
    const code = readFileSync(join(DIST, bundleName), 'utf8');
    // 3 个动态 tab 合并在一处模板字面量 + allBtn 一处字面量 → ≥1 即可
    const n = assertTabsUseTemplateInterpolation(code, '死信/bundle', 1);
    expect(n).toBeGreaterThanOrEqual(1);
  });

  it('源码里 dlq tab 的 data-args 不再用拼接', () => {
    assertSourceUsesTemplate(readFileSync(join(PAGES, 'dead_letters.js'), 'utf8'), '死信/源码');
  });
});

describe('草稿审阅页：tab data-args 在 minify 产物中正确绑定（回归 status=undefined 清空列表）', () => {
  it('esbuild minify 后 draft tab 的 data-args 用模板插值', () => {
    const code = minify(readFileSync(join(PAGES, 'drafts.js'), 'utf8'));
    // 4 个 tab 在 minify 后位于同一处模板字面量内（循环里），故只需 ≥1
    const n = assertTabsUseTemplateInterpolation(code, '草稿/minify', 1);
    expect(n).toBeGreaterThanOrEqual(1);
  });

  it('源码里 draft tab 的 data-args 不再用拼接', () => {
    assertSourceUsesTemplate(readFileSync(join(PAGES, 'drafts.js'), 'utf8'), '草稿/源码');
  });
});
