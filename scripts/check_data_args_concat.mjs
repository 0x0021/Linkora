#!/usr/bin/env node
// 前端门禁：禁止用「字符串拼接」生成 data-args / data-* 属性值。
//
// 背景（真实故障）：草稿审阅页与死信队列页的过滤器 tab 用
//   '<button data-args=\'["' + s + '"]\'>'
// 拼接生成属性值。构建时 esbuild --minify 会重命名函数作用域内的变量
// （回调参数 s → M），却漏改字符串拼接里的 s，使其指向外层同名变量
// （allBtn，var 顺序求值时为 undefined），最终生成 data-args='["undefined"]'。
// 点击 tab → _xxxSwitchStatus("undefined") → 请求 status=undefined
// → 后端 WHERE status='undefined' → 列表被清空。
//
// 规则：data-* 属性值必须用模板字符串 ${} 插值（作用域绑定，minify 安全），
// 不得用 '+ 变量 +' 拼接。
//
// 用法：node scripts/check_data_args_concat.mjs
// 退出码 0 = 通过；1 = 存在隐患。

import { readdirSync, statSync, readFileSync } from 'fs';
import { join, relative, dirname } from 'path';
import { fileURLToPath } from 'url';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const JS_DIR = join(ROOT, 'web/static/js');

// 匹配：在「HTML 字符串生成」中用 '+ 变量 +' 拼接 data-* 属性值。
// 模板字符串 ${...} 不匹配（minify 安全），静态字面量也不匹配。
//
// 仅匹配 data-args / data-action / data-id 等常见 HTML 属性，且要求该
// data-* 出现在一个字符串字面量（引号）内部——即真正在拼 HTML。
// 排除 querySelector('.xxx[data-id="' + id + '"]') 这类运行时选择器拼接：
// 那类不进 HTML、不经 minify 重命名，无此风险。
const ATTR_NAMES = 'args|action|id|status|key|value|role|target|slug|type|name|platform|config|page|tab|attr';
// 形态：data-xxx=  '...' + VAR + '...'  （引号紧跟 = 后，说明在拼 HTML 字符串）
const CONCAT_RE = new RegExp(
  String.raw`data-(?:${ATTR_NAMES})\s*=\s*\\?['"][^'"]*\\?['"]?\s*\+\s*[A-Za-z_$][\w$]*\s*\+`,
  'g',
);
// 形态：data-xxx= '...' + VAR + '...'  （' 与 " 混用）
const CONCAT_RE2 = new RegExp(
  String.raw`data-(?:${ATTR_NAMES})\s*=\\?'[^']*'\s*\+\s*[A-Za-z_$][\w$]*\s*\+\s*\\?["']`,
  'g',
);
// 排除：位于 querySelector / querySelectorAll / closest / matches 调用内的拼接
const SAFE_CALL_RE = /(?:querySelectorAll|querySelector|closest|matches)\s*\(\s*[^)]*$/;

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'node_modules' || name === 'tests') continue;
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

const files = walk(JS_DIR);
const findings = [];

for (const file of files) {
  const src = readFileSync(file, 'utf8');
  const lines = src.split('\n');
  lines.forEach((line, i) => {
    const code = line.split('//')[0];
    if (!code.includes('data-')) return;
    // 模板字符串插值是安全写法，直接跳过
    if (code.includes('${')) return;
    const hits = new Set();
    for (const re of [CONCAT_RE, CONCAT_RE2]) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(code))) hits.add(m[0]);
    }
    if (hits.size > 0) {
      // 排除运行时选择器拼接（不进 HTML，无 minify 重命名风险）
      const isSelector = SAFE_CALL_RE.test(code.slice(0, code.indexOf('data-')));
      if (isSelector) return;
      findings.push({
        file: relative(ROOT, file),
        line: i + 1,
        text: line.trim().slice(0, 120),
      });
    }
  });
}

if (findings.length === 0) {
  console.log(`[check_data_args_concat] 通过：扫描 ${files.length} 个前端 JS 文件，未发现用字符串拼接生成 data-* 属性值。`);
  process.exit(0);
}

console.error(`[check_data_args_concat] 失败：发现 ${findings.length} 处用字符串拼接生成 data-* 属性值。`);
console.error('  风险：构建时 esbuild --minify 会重命名函数作用域变量，却可能漏改拼接中的引用，');
console.error('  使其指向外层同名变量（常为 undefined），导致点击后参数错误、列表被清空。');
console.error('  修法：改用模板字符串 ${} 插值（作用域绑定，minify 安全）。\n');
for (const f of findings) {
  console.error(`  ${f.file}:${f.line}`);
  console.error(`      ${f.text}`);
}
process.exit(1);
