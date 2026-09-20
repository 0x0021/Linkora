// scripts/migrate_inline_onclick.mjs
// Phase 0 机械化迁移：将 index.html 模板里内联 onclick="fn(args)" 转为
//   data-action="fn" data-args='[...]'  ，由 app.js 的统一事件委托分发。
//
// 设计约束（与现有事件委托契约一致）：
//  - 仅迁移「单标识符函数名 + 字面量参数」形态；含 this/event/; 或背景点击特例予以保留。
//  - 参数 this → "@el"（运行期由委托替换为被点击元素，等价于旧 onclick 的 this）。
//  - data-args 用单引号 HTML 属性包裹 JSON（内部双引号），并对 &<> 做属性转义。
//  - 若参数含单引号（会破坏 HTML 属性）则保留原内联 onclick，不强行迁移。
//  - 不触碰 onsubmit / onchange / oninput 等其他内联事件处理器。
//
// 运行：node scripts/migrate_inline_onclick.mjs   （项目根目录）

import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const HTML = join(ROOT, 'web', 'templates', 'index.html');

let html = readFileSync(HTML, 'utf8');

// 轻量参数解析：尊重引号与转义，按顶层逗号切分；this→@el，纯数字→number，
// true/false/null 原样，引号包裹→去引号字符串。
function parseArgs(argStr) {
  const out = [];
  let i = 0;
  const n = argStr.length;
  let cur = '';
  let inQ = null;
  while (i < n) {
    const c = argStr[i];
    if (inQ) {
      if (c === '\\') {
        cur += c + (argStr[i + 1] || '');
        i += 2;
        continue;
      }
      if (c === inQ) {
        inQ = null;
        i++;
        continue;
      }
      cur += c;
      i++;
      continue;
    }
    if (c === "'" || c === '"') {
      inQ = c;
      i++;
      continue;
    }
    if (c === ',') {
      out.push(cur.trim());
      cur = '';
      i++;
      continue;
    }
    cur += c;
    i++;
  }
  if (cur.trim() !== '' || out.length === 0) out.push(cur.trim());

  return out.map((t) => {
    if (t === 'this') return '@el';
    if (t === 'true') return true;
    if (t === 'false') return false;
    if (t === 'null') return null;
    if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
    if ((t[0] === "'" || t[0] === '"') && t[t.length - 1] === t[0]) return t.slice(1, -1);
    return t; // 裸标识符（理论上不应出现）
  });
}

// 属性转义：防止 data-args 内合法字符破坏 HTML 属性解析
function escAttr(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

let migrated = 0;
let skipped = 0;
const kept = [];

// 仅匹配 HTML 属性形态（前导空白），避免命中 <script> 内的 'onclick="..."' 字符串
const re = /(\s)onclick="([^"]*)"/g;

html = html.replace(re, (full, lead, body) => {
  // 背景点击关闭灯箱：依赖 event.target===this，保留内联
  if (body === 'if(event.target===this)closeImageLightbox()') {
    skipped++;
    kept.push(body);
    return full;
  }
  // 多语句（含 return false）：保留内联，等待人工评估
  if (/[;{}]|=>|&&|\|\||\?/.test(body)) {
    skipped++;
    kept.push(body);
    return full;
  }
  // 单标识符函数名(参数) 形态
  const m = body.match(/^([A-Za-z_$][\w$]*)\((.*)\)$/);
  if (!m) {
    skipped++;
    kept.push(body);
    return full;
  }
  const fn = m[1];
  let args;
  try {
    args = parseArgs(m[2]);
  } catch (e) {
    skipped++;
    kept.push(body);
    return full;
  }
  // 参数含单引号会破坏 HTML 属性 → 保留内联
  const json = JSON.stringify(args);
  if (json.includes("'")) {
    skipped++;
    kept.push(body);
    return full;
  }
  migrated++;
  return `${lead}data-action="${fn}" data-args='${escAttr(json)}'`;
});

writeFileSync(HTML, html);

console.log(`[migrate] 内联 onclick → data-action：迁移 ${migrated} 处，保留 ${skipped} 处`);
if (kept.length) {
  console.log('[migrate] 保留的内联 onclick：');
  kept.forEach((k) => console.log('   - ' + k));
}
