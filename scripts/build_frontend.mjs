// scripts/build_frontend.mjs
// 前端构建：将 40+ CSS 与 30+ 经典 <script> 合并为单 bundle，减少首屏请求数（~70 → 2）。
//
// 设计要点（与现有「经典 script 共享全局作用域」加载方式 100% 行为等价）：
//  - 经典 <script> 各自顶层作用域本就共享全局；直接按模板顺序拼接，除顶层同名
//    const/let/class 会冲突外完全等价。已审计确认无真实冲突（DOMAIN 等均在 IIFE 内）。
//  - 逐文件剥离顶层 'use strict' 指令，使整 bundle 统一为 sloppy（避免严格模式污染
//    导致的块级函数声明语义变化），与各 page 经典脚本当前默认行为一致。
//  - esbuild 仅作「压缩」（minify，非 bundle），不重命名顶层 function/var（外部引用未知），
//    故 window.switchPage 等全局桥接与 onclick="switchPage()" 内联处理器不受影响。
//  - 内容哈希文件名 → 长效缓存；api.py 读取 dist/manifest.json 注入 ?v= 版本号。
//  - drafts.js 为 type=module，不参与合并，仍单独加载（见 index.html）。
//
// 运行：npm run build:frontend  （需在项目根目录执行）

import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, unlinkSync, watch } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { dirname, join, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const STATIC = join(ROOT, 'web', 'static');
const OUT = join(STATIC, 'dist');
const ESBUILD = join(ROOT, 'node_modules', '.bin', 'esbuild');

// ---- 加载顺序严格对齐 web/templates/index.html（head CSS + 底部脚本）----
const CSS_ORDER = [
  'css/icons.css',
  'css/bootstrap-override.css',
  'css/theme.css',
  'css/pages/persona.css',
  'css/base/variables.css',
  'css/base/reset.css',
  'css/base/utilities.css',
  'css/layout/app-shell.css',
  'css/layout/dashboard.css',
  'css/components/panel.css',
  'css/components/table.css',
  'css/components/toast.css',
  'css/components/button.css',
  'css/components/form.css',
  'css/components/kpi.css',
  'css/components/chart.css',
  'css/components/badge.css',
  'css/pages/dashboard.css',
  'css/pages/messages.css',
  'css/pages/rag.css',
  'css/pages/rules.css',
  'css/pages/settings.css',
  'css/pages/keywords.css',
  'css/pages/metrics.css',
  'css/pages/simulate.css',
  'css/pages/dead_letters.css',
  'css/pages/logs.css',
  'css/pages/drafts.css',
  'css/pages/tools.css',
  'css/pages/intent.css',
  'css/motion.css',
];

// 经典脚本（非 module）顺序；app.js 必须最后（window.X=X 桥接引用各 page 全局函数）。
const JS_ORDER = [
  'js/icons.js',
  'js/theme.js',
  'js/core/api.js',
  'js/core/store.js',
  'js/core/util.js',
  'js/components/kpiCard.js',
  'js/components/chartCard.js',
  'js/components/dataTable.js',
  'js/components/decisionTable.js',
  'js/components/panel.js',
  'js/components/stateBadge.js',
  'js/components/cardValidator.js',
  'js/services/observabilityService.js',
  'js/services/routingQualityService.js',
  'js/services/dashboardReliabilityService.js',
  'js/pages/config.js',
  'js/pages/dead_letters.js',
  'js/pages/dashboard.js',
  'js/pages/departments.js',
  'js/pages/intent.js',
  'js/pages/keywords.js',
  'js/pages/messages.js',
  'js/pages/rag.js',
  'js/pages/skills.js',
  'js/pages/vector_status.js',
  'js/pages/routetrace.js',
  'js/pages/tools.js',
  'js/pages/persona.js',
  'js/pages/metrics.js',
  'js/pages/cost_quality.js',
  'js/pages/logs.js',
  'js/core/app.js',
  'js/core/onboarding.js',
  'js/pages/simulate.js',
];

function read(rel) {
  const p = join(STATIC, rel);
  if (!existsSync(p)) throw new Error(`构建缺失源文件: ${rel}`);
  return readFileSync(p, 'utf8');
}

function stripUseStrict(src) {
  // 仅剥离文件最开头的 'use strict' 指令（拼接后统一 sloppy，避免严格模式污染）
  return src.replace(/^\s*['"]use strict['"]\s*;?\s*/, '');
}

function concat(order, sep) {
  return order
    .map((rel) => `/* === ${rel} === */\n` + stripUseStrict(read(rel)))
    .join(sep);
}

function minify(code, loader) {
  if (!existsSync(ESBUILD)) {
    console.warn('[build] 未找到 esbuild，跳过压缩（仍输出未压缩 bundle）');
    return code;
  }
  try {
    return execFileSync(ESBUILD, ['--minify', `--loader=${loader}`], {
      input: code,
      maxBuffer: 1 << 28,
    }).toString();
  } catch (e) {
    console.warn('[build] esbuild 压缩失败，回退未压缩：', e.message);
    return code;
  }
}

function hashOf(s) {
  return createHash('sha256').update(s).digest('hex').slice(0, 12);
}

// ---- 单次构建：清理旧 bundle → 合并压缩 → 写 manifest ----
function build() {
  if (existsSync(OUT)) {
    for (const f of readdirSync(OUT)) {
      if (f.startsWith('bundle.')) unlinkSync(join(OUT, f));
    }
  } else {
    mkdirSync(OUT, { recursive: true });
  }

  const cssMin = minify(concat(CSS_ORDER, '\n'), 'css');
  const cssName = `bundle.${hashOf(cssMin)}.css`;
  writeFileSync(join(OUT, cssName), cssMin);

  const jsMin = minify(concat(JS_ORDER, '\n;\n'), 'js');
  const jsName = `bundle.${hashOf(jsMin)}.js`;
  writeFileSync(join(OUT, jsName), jsMin);

  const manifest = { css: cssName, js: jsName };
  writeFileSync(join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 2));

  console.log(
    `[build] 完成：CSS ${CSS_ORDER.length} 文件 → ${cssName} (${cssMin.length} B)；` +
      `JS ${JS_ORDER.length} 文件 → ${jsName} (${jsMin.length} B)`
  );
}

const WATCH = process.argv.includes('--watch');
if (WATCH) {
  // 监听模式：监视 web/static 下任意 css/js 源码变更，防抖后自动重建 dist，
  // 让你或我本地改动前端后无需手动跑构建即可拿到最新编译产物（api.py 读 manifest 注入版本）。
  console.log('[build] 监听模式：监视 web/static 变动，自动重新编译前端…  (Ctrl+C 退出)');
  let timer = null;
  const trigger = () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      try {
        build();
      } catch (e) {
        console.error('[build] 重建失败：', e.message);
      }
    }, 120);
  };
  try {
    watch(
      STATIC,
      { recursive: true },
      (_event, filename) => {
        if (!filename) return;
        if (filename.startsWith('dist' + sep)) return; // 忽略产物自身写入
        if (!/\.(css|js)$/.test(filename)) return;
        trigger();
      }
    );
  } catch (e) {
    console.error('[build] 无法启动监听（当前平台不支持 recursive watch）：', e.message);
    process.exit(1);
  }
} else {
  build();
}
